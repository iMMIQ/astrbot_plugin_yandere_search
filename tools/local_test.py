# SPDX-License-Identifier: LGPL-3.0-or-later
#!/usr/bin/env python3
"""本地假事件栈 E2E：不安装 AstrBot 也能驱动插件全部能力。

用法: python3 tools/local_test.py [--quick]
--quick 跳过 live 网络项（booru 实网/真实反搜），只跑离线矩阵。

覆盖矩阵：
  A. 单元: _parse 全语法 / _rating_cap 群私聊矩阵 / 词表翻译 / LLM 校验
  B. 离线命令流(StubBooru): 连写重试 / 空结果双诊断 / 热门跨天回补 /
     下一张游标推进与耗尽 / 收藏越界 / 订阅推送去重·空推·失败
  C. 治理: 白名单 e / 冷却·配额管理员豁免 / 总开关 / 历史 TTL
  D. 反搜(MockTransport): SauceNAO 解析 / iqdb multipart+解析 / 去重合并 /
     CF 退避 / 每日配额门 / 命中本站自动补图(分级门)
  E. 搜图模式: 触发 / 30s 节流 / "/"跳过 / 关闭
  F. live 网络: booru 全方法 / 全命令真实流 / iqdb 真实反搜
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))
TMP = Path(tempfile.mkdtemp(prefix="yandere_test_"))
QUICK = "--quick" in sys.argv


# ---------------- astrbot 桩 ----------------

def _setup_stubs() -> None:
    class _Log:
        def _p(self, tag, *a):
            print(f"  [stub-log.{tag}]", *a)

        def info(self, *a):
            self._p("info", *a)

        def warning(self, *a):
            self._p("warn", *a)

        def error(self, *a):
            self._p("err", *a)

    logger = _Log()

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logger

    class AstrMessageEvent:
        pass

    class MessageChain:
        def __init__(self):
            self.chain = []

        def message(self, m):
            self.chain.append(("plain", m))
            return self

        def file_image(self, p):
            self.chain.append(("img", p))
            return self

        def url_image(self, u):
            self.chain.append(("img", u))
            return self

    class PermissionType:
        ADMIN = "ADMIN"
        MEMBER = "MEMBER"

    class EventMessageType:
        ALL = "ALL"
        GROUP_MESSAGE = "GROUP_MESSAGE"

    class _Filter:
        @staticmethod
        def command(name, alias=None, **kw):
            return lambda fn: fn

        @staticmethod
        def permission_type(t, **kw):
            return lambda fn: fn

        @staticmethod
        def event_message_type(t, **kw):
            return lambda fn: fn

    # 类体内访问不到外层函数局部名，类属性在类外补挂
    _Filter.PermissionType = PermissionType
    _Filter.EventMessageType = EventMessageType

    event_pkg = types.ModuleType("astrbot.api.event")
    event_pkg.AstrMessageEvent = AstrMessageEvent
    event_pkg.MessageChain = MessageChain
    event_pkg.filter = _Filter()

    class Star:
        def __init__(self, context):
            self.context = context

    class Context:
        pass

    star = types.ModuleType("astrbot.api.star")
    star.Star = Star
    star.Context = Context

    core = types.ModuleType("astrbot.core")
    cstar = types.ModuleType("astrbot.core.star")
    cfilter = types.ModuleType("astrbot.core.star.filter")
    cmd_mod = types.ModuleType("astrbot.core.star.filter.command")
    cmd_mod.GreedyStr = str

    class StarTools:
        _dir = TMP

        @classmethod
        def get_data_dir(cls, name=None):
            d = cls._dir / ("data_" + (name or "x"))
            d.mkdir(parents=True, exist_ok=True)
            return d

    st_mod = types.ModuleType("astrbot.core.star.star_tools")
    st_mod.StarTools = StarTools

    class Image:
        def __init__(self, file=None, url=""):
            self.file = file
            self.url = url

        @staticmethod
        def fromFileSystem(p):
            return Image(file=str(p))

    class Node:
        def __init__(self, content=None, uin="0", name=""):
            self.content = content or []

    class Nodes:
        def __init__(self, nodes=None):
            self.nodes = nodes or []

    class Reply:
        def __init__(self, chain=None):
            self.chain = chain or []

    all_mod = types.ModuleType("astrbot.api.all")
    all_mod.Image = Image
    all_mod.Node = Node
    all_mod.Nodes = Nodes
    all_mod.Reply = Reply
    all_mod.MessageChain = MessageChain

    mods = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_pkg,
        "astrbot.api.star": star,
        "astrbot.api.all": all_mod,
        "astrbot.core": core,
        "astrbot.core.star": cstar,
        "astrbot.core.star.filter": cfilter,
        "astrbot.core.star.filter.command": cmd_mod,
        "astrbot.core.star.star_tools": st_mod,
    }
    astrbot.api = api
    astrbot.core = core
    api.event = event_pkg
    api.star = star
    api.all = all_mod
    core.star = cstar
    cstar.filter = cfilter
    cstar.star_tools = st_mod
    for k, v in mods.items():
        sys.modules[k] = v


_setup_stubs()

import httpx  # noqa: E402

import astrbot_plugin_yandere_search.main as m  # noqa: E402
from astrbot_plugin_yandere_search.booru import MoebooruClient, BooruError  # noqa: E402

ALL = sys.modules["astrbot.api.all"]

# 1x1 PNG（供 StubBooru.fetch / 转码链路使用）
_buf = io.BytesIO()
try:
    from PIL import Image as PILImage

    PILImage.new("RGB", (8, 8), (200, 30, 30)).save(_buf, "PNG")
    PNG_BYTES = _buf.getvalue()
except Exception:
    PNG_BYTES = b"\x89PNG\r\n\x1a\n"  # PIL 缺失时退化为裸字节（convert 关闭时可用）


def mk_post(pid, rating="s", tags="catgirl nekomimi", md5=None):
    return {
        "id": pid, "md5": md5 or f"{pid:032x}", "rating": rating, "score": 100 + pid,
        "tags": tags, "status": "active",
        "file_url": f"https://files.yande.re/image/{pid}/test%20file.jpg",
        "jpeg_url": f"https://files.yande.re/jpeg/{pid}/test.jpg",
        "preview_url": f"https://files.yande.re/preview/{pid}/test.jpg",
    }


class FakeContext:
    def __init__(self, provider=None, send_ok=True):
        self.sent = []
        self.provider = provider
        self.send_ok = send_ok

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return self.send_ok

    async def get_using_provider_async(self, umo=None):
        return self.provider


class FakeProvider:
    def __init__(self, text):
        self.text = text
        self.prompts = []

    async def text_chat(self, prompt=None, system_prompt=None, **kw):
        self.prompts.append(prompt)
        return types.SimpleNamespace(completion_text=self.text, result_chain=None)


class FakeMessageType:
    def __init__(self, group=True):
        self._group = group

    def __str__(self):
        return "MessageType.GROUP_MESSAGE" if self._group else "MessageType.PRIVATE_MESSAGE"


class FakeMessageObj:
    def __init__(self, comps=None):
        self.message = list(comps or [])
        self.self_id = "10000"


class FakeBot:
    def __init__(self, ret=None):
        self.ret = ret or {}
        self.calls = []

    async def call_action(self, action, **kw):
        self.calls.append((action, kw))
        return self.ret


class FakeEvent:
    def __init__(self, text="", comps=None, admin=True, uid="10001", umo=None, group=True, bot=None):
        self.unified_msg_origin = umo or "aiocqhttp:GroupMessage:123456"
        self.message_str = text
        self.message_obj = FakeMessageObj(comps)
        self._admin = admin
        self._uid = uid
        self._group = group
        self.bot = bot
        self.results = []

    def get_sender_id(self):
        return self._uid

    def get_group_id(self):
        tail = self.unified_msg_origin.rpartition(":")[2]
        return tail.split("_")[-1] if "_" in tail else tail

    def get_sender_name(self):
        return "tester"

    def is_admin(self):
        return self._admin

    def get_message_type(self):
        return FakeMessageType(self._group)

    def plain_result(self, text):
        self.results.append(("plain", text))
        return text

    def chain_result(self, chain):
        self.results.append(("chain", chain))
        return chain

    def stop_event(self):
        self.results.append(("stop",))

    async def send(self, mc):
        self.results.append(("send", mc))


CFG = {
    "api_url": "https://yande.re",
    "proxy_url": "",
    "default_rating": "s",
    "group_rating_cap": "s",
    "r18_whitelist": "aiocqhttp:GroupMessage:123456",
    "max_images": 3,
    "image_quality": "preview",
    "convert_enabled": False,
    "order": "score",
    "request_timeout": 20,
    "cooldown_seconds": 8,
    "daily_quota": 80,
    "forward_threshold": 3,
    "extra_sites": "konachan",
    "fallback_sites": "",
    "fallback_headstart": 8,
    "deadline_seconds": 24,
    "reverse_enabled": True,
    "saucenao_api_key": "",
    "saucenao_daily_limit": 30,
    "iqdb_enabled": True,
    "llm_fallback": False,
}

UMO = "aiocqhttp:GroupMessage:123456"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  | {detail}" if detail and not cond else ""))


def plains(ev):
    return [r[1] for r in ev.results if r[0] == "plain"]


def chains(ev):
    return [r[1] for r in ev.results if r[0] == "chain"]


def sends(ev):
    return [r[1] for r in ev.results if r[0] == "send"]


async def drive(gen):
    async for _ in gen:
        pass


def s_search(plugin, ev, q):
    """旧 /搜图 语义：/p + 精选（质量优先路径）"""
    return plugin.cmd_p(ev, f"{q} 精选")


def s_random(plugin, ev, q):
    """旧 /随机 语义：/p 默认随机路径"""
    return plugin.cmd_p(ev, q)


def fresh_umo(tag):
    return f"aiocqhttp:GroupMessage:{abs(hash(tag)) % 10**8}"


class StubBooru:
    """离线桩：可编程 canned 响应，供命令流矩阵测试。"""

    site_key, label = "yandere", "yandere.re"

    def __init__(self):
        self.page_results = []       # page_posts 调用依次弹出
        self.popular_map = {}        # (period, days_back) -> posts
        self.search_results = []     # search() 调用依次弹出
        self.count_value = 0
        self.suggest_value = []
        self.get_post_map = {}
        self.page_calls = []
        self.search_calls = []
        self.popular_calls = []
        self.fetch_calls = []        # (post_id, quality)
        self.fetch_delay = 0.0
        self.fetch_hook = None       # async fn(post_id, quality) 模拟按档位限速

    async def close(self):
        pass

    async def search(self, tags, limit=3, order="score", rating="", exclude=None):
        self.search_calls.append((tuple(tags), limit, order, rating, tuple(sorted(exclude or ()))))
        return self.search_results.pop(0) if self.search_results else []

    async def page_posts(self, tags, page=1, rating="", order="score", exclude=None):
        self.page_calls.append((tuple(tags), page, rating, order))
        return self.page_results.pop(0) if self.page_results else []

    async def popular(self, period="day", days_back=0):
        self.popular_calls.append((period, days_back))
        return self.popular_map.get((period, days_back), [])

    async def get_post(self, post_id):
        return self.get_post_map.get(post_id)

    async def count(self, tags):
        return self.count_value

    async def suggest_tags(self, word, limit=5):
        return self.suggest_value

    async def fetch(self, post, quality="original"):
        self.fetch_calls.append((post["id"], quality))
        if self.fetch_hook:
            await self.fetch_hook(post["id"], quality)
        elif self.fetch_delay:
            await asyncio.sleep(self.fetch_delay)
        return ".png", PNG_BYTES

    @staticmethod
    def pick_url(post, quality):
        return post.get("preview_url") or post["file_url"]


class StubSearcher:
    def __init__(self, results=None, notes=None, remaining=None):
        self.results = results or []
        self.notes = notes or []
        self.remaining = remaining
        self.calls = []

    async def close(self):
        pass

    async def search(self, image_url=None, file_path=None):
        self.calls.append((image_url, file_path))
        return {"results": self.results, "engines": ["iqdb"], "notes": self.notes,
                "saucenao_remaining": self.remaining}


# ---------------- A. 单元矩阵 ----------------

async def section_a(plugin):
    print("\n== A. 单元矩阵 ==")
    # _parse 全语法
    cases = [
        ("巨乳 3", (["巨乳"], 3, None, None)),
        ("5", ([], 3, None, None)),                      # 纯数字=数量，clamp 到 max_images
        ("catgirl rating:e site:konachan 2", (["catgirl"], 2, "konachan", "e")),
        ("rating:q", ([], 1, None, "q")),
        ("rating:g 2", ([], 2, None, "s")),              # g 归一化为 s
        ("雷姆 帕秋莉", (["雷姆", "帕秋莉"], 1, None, None)),
        ("绝区零 10", (["绝区零"], 3, None, None)),        # 超过 max_images 被 clamp
        ("旗袍3", (["旗袍"], 3, None, None)),             # 中文标签与数字连写
        ("萝莉 2", (["萝莉"], 2, None, None)),
        ("旗袍 r18 2", (["旗袍"], 2, None, "e")),          # r18=分级词，结尾 2 仍是数量
        ("旗袍 r18", (["旗袍"], 1, None, "e")),            # r18 的 18 不被当数量
        ("r18", ([], 1, None, "e")),
        ("旗袍 擦边", (["旗袍"], 1, None, "q")),
        ("全年龄 3", ([], 3, None, "s")),
        ("雷姆 e", (["雷姆"], 1, None, "e")),              # 裸单字母分级
    ]
    for q, want in cases:
        got = plugin._parse(q)
        check(f"parse: {q!r}", got == want, f"got {got}")

    # 翻译 + Danbooru 写法纠正
    tr = plugin._translate(["巨乳", "cat_girl", "Catgirl", "unknown_xyz"])
    check("translate: 词表+纠正", tr[0] == "large_breasts" and tr[1] == "catgirl" and tr[3] == "unknown_xyz", str(tr))

    # 分级上限矩阵：群/私聊 × row 覆盖 × 全局配置
    ge, pe = FakeEvent(group=True), FakeEvent(group=False)
    check("ratingcap: 群默认=group_rating_cap", plugin._rating_cap(ge, plugin.gov.get_chat(UMO)) == "s")
    check("ratingcap: 私聊默认=default_rating", plugin._rating_cap(pe, plugin.gov.get_chat(UMO)) == "s")
    plugin.gov.set_chat(UMO, rating_cap="q")
    check("ratingcap: row 覆盖优先", plugin._rating_cap(ge, plugin.gov.get_chat(UMO)) == "q")
    pu = "aiocqhttp:PrivateMessage:777"
    plugin.gov.set_chat(pu, rating_cap="e")
    check("ratingcap: 私聊 row 覆盖", plugin._rating_cap(FakeEvent(group=False, umo=pu), plugin.gov.get_chat(pu)) == "e")
    plugin.gov.set_chat(UMO, rating_cap="s")

    # LLM 兜底校验（tagger 单元）
    from astrbot_plugin_yandere_search.tagger import LLMTagger

    t = LLMTagger(FakeContext(FakeProvider("silver_hair, catgirl, 银发, fake_tag!! , rain")),
                  {"silver_hair", "catgirl", "rain"})
    got, dropped = await t.extract("x", "银发猫娘")
    check("tagger: 候选校验过滤", got == ["silver_hair", "catgirl", "rain"] and dropped == 2, f"{got}/{dropped}")
    t2 = LLMTagger(FakeContext(None), {"catgirl"})
    got2, _ = await t2.extract("x", "任意")
    check("tagger: 无 provider 返回空", got2 == [])
    check("tagger: has_cjk", LLMTagger.has_cjk("猫") and not LLMTagger.has_cjk("cat"))

    # 随机兜底（mock _get）：quirk 空轮短路 / 随机页取样 / 排除 / 看遍重置
    c = MoebooruClient(timeout=5)
    api_calls = []

    def mk_ids(a, b):
        return [{"id": i, "rating": "s", "score": 10,
                 "jpeg_url": f"https://x/{i}.jpg"} for i in range(a, b)]

    class FakeResp:
        def __init__(self, payload, text=""):
            self._p, self._t = payload, text

        def json(self):
            return self._p

        @property
        def text(self):
            return self._t

    async def fake_get(path, params=None):
        params = dict(params or {})
        api_calls.append((path, params))
        if path == "/post.xml":
            return FakeResp(None, '<posts count="250"></posts>')
        q = params.get("tags", "")
        if "order:random" in q:
            return FakeResp([])  # quirk：稳定空
        base = 1000 + params.get("page", 1) * 100
        return FakeResp(mk_ids(base, base + 100))

    c._get = fake_get
    with mock.patch("astrbot_plugin_yandere_search.booru.random.randint", return_value=2):
        got = await c.search(["loli"], limit=3, order="random", rating="s")
    ids = [p["id"] for p in got]
    check("booru: 兜底取随机页(page2)", len(ids) == 3 and all(1200 <= i < 1300 for i in ids), str(ids))
    rnd_calls = [a for a in api_calls if "order:random" in a[1].get("tags", "")]
    check("booru: random 空轮短路", len(rnd_calls) == 1, str(rnd_calls))

    api_calls.clear()
    with mock.patch("astrbot_plugin_yandere_search.booru.random.randint", return_value=2):
        got = await c.search(["loli"], limit=3, order="random", rating="s",
                             exclude=set(range(1200, 1230)))
    ids = [p["id"] for p in got]
    check("booru: 兜底尊重排除", len(ids) == 3 and all(1230 <= i < 1300 for i in ids), str(ids))

    with mock.patch("astrbot_plugin_yandere_search.booru.random.randint", return_value=2):
        got = await c.search(["loli"], limit=2, order="random", rating="s",
                             exclude=set(range(1200, 1300)))  # 只排除第2页
    ids = [p["id"] for p in got]
    check("booru: 随机页全排除回退前页", len(ids) == 2 and all(1000 <= i < 1200 for i in ids), str(ids))

    with mock.patch("astrbot_plugin_yandere_search.booru.random.randint", return_value=2):
        got = await c.search(["loli"], limit=2, order="random", rating="s",
                             exclude=set(range(1000, 1250)))  # 250 张全看过
    ids = [p["id"] for p in got]
    check("booru: 看遍整池自动重置", len(ids) == 2 and all(1000 <= i < 1300 for i in ids), str(ids))
    await c.close()


# ---------------- B. 离线命令流（StubBooru） ----------------

async def section_b(plugin, ctx):
    print("\n== B. 离线命令流 ==")
    stub = StubBooru()
    real = plugin._clients.get("yandere")
    plugin._clients["yandere"] = stub
    try:
        # B1 连写重试：两词 AND 空 -> _ 连写重试命中
        stub.page_results = [[], [mk_post(1), mk_post(2)]]
        ev = FakeEvent(umo=fresh_umo("join"))
        await drive(s_search(plugin, ev, "genshin impact 2"))
        check("join-retry: 二次调用用连写标签",
              stub.page_calls[0][0] == ("genshin", "impact") and stub.page_calls[1][0] == ("genshin_impact",),
              str(stub.page_calls))
        check("join-retry: 出图", len(chains(ev)) >= 1)

        # B2 空结果诊断-分级过滤：search 空 + count>0
        stub.page_results = [[]]
        stub.count_value = 500
        ev = FakeEvent(umo=fresh_umo("diag1"))
        await drive(s_search(plugin, ev, "某词 1"))
        check("diagnose: 提示被分级过滤", any("共有 500 张" in t for t in plains(ev)), str(plains(ev)))

        # B3 空结果诊断-标签不存在（显式 rating 时跳过 count）+ 相似标签
        stub.page_results = [[]]
        stub.count_value = 0
        stub.suggest_value = [("catgirls", 123)]
        ev = FakeEvent(umo=fresh_umo("diag2"))
        await drive(s_search(plugin, ev, "catgirlss rating:e 1"))
        check("diagnose: 显式rating跳过计数", not any("共有" in t for t in plains(ev)))
        check("diagnose: 相似标签建议", any("catgirls(123)" in t for t in plains(ev)), str(plains(ev)))

        # B4 热门跨天回补：day0 只有 e 级 -> cap s 下回补 day1 的 3 张 s
        stub.popular_map = {
            ("day", 0): [mk_post(10, rating="e")],
            ("day", 1): [mk_post(11), mk_post(12), mk_post(13)],
            ("week", 0): [mk_post(20, rating="s"), mk_post(21, rating="q")],
        }
        umo = fresh_umo("hot")
        ev = FakeEvent(umo=umo)
        await drive(plugin.cmd_hot(ev, "3"))
        h = plugin.gov.get_history(umo, "10001")
        check("hot: 跨天回补调用链", ("day", 0) in stub.popular_calls and ("day", 1) in stub.popular_calls,
              str(stub.popular_calls))
        check("hot: 回补池全过分级门", h and all(p["rating"] == "s" for p in h["pool"]) and len(h["pool"]) >= 3,
              str([(p["id"], p["rating"]) for p in (h or {}).get("pool", [])]))

        # B5 热门全被分级滤空
        stub.popular_map = {("day", 0): [mk_post(30, rating="e")], ("day", 1): [], ("day", 2): []}
        ev = FakeEvent(umo=fresh_umo("hotempty"))
        await drive(plugin.cmd_hot(ev, ""))
        check("hot: 滤空提示", any("没有可发的图" in t for t in plains(ev)), str(plains(ev)))

        # B6 下一张游标推进与耗尽（score pool=3, 首次取2）
        umo = fresh_umo("next")
        stub.page_results = [[mk_post(41), mk_post(42), mk_post(43)]]
        ev = FakeEvent(umo=umo)
        await drive(s_search(plugin, ev, "x 2"))
        h1 = plugin.gov.get_history(umo, "10001")
        calls_before = len(stub.page_calls)
        ev2 = FakeEvent(umo=umo)
        await drive(plugin.cmd_next(ev2, "2"))
        h2 = plugin.gov.get_history(umo, "10001")
        check("next: 走缓存池不再请求", len(stub.page_calls) == calls_before)
        check("next: 游标推进取到剩余", h1["offset"] == 2 and h2["offset"] == 3 and h2["posts"][0]["id"] == 43,
              f"{h1.get('offset')}->{h2.get('offset')}")
        ev3 = FakeEvent(umo=umo)
        await drive(plugin.cmd_next(ev3, "1"))
        check("next: 耗尽提示", any("看完" in t for t in plains(ev3)), str(plains(ev3)))

        # B7 随机续看去重：exclude 传入上次 shown
        umo = fresh_umo("nextr")
        stub.search_results = [[mk_post(51), mk_post(52)]]
        await drive(s_random(plugin, FakeEvent(umo=umo), "词 2"))
        stub.search_results = [[mk_post(51), mk_post(53)]]  # 51 重复出现
        await drive(plugin.cmd_next(FakeEvent(umo=umo), "2"))
        check("next-random: exclude 生效", stub.search_calls[-1][4] == (51, 52), str(stub.search_calls[-1]))

        # B7b 连续两轮 /随机 命令：第二轮带 seen 排除（新一轮命令而非续看）
        umo = fresh_umo("rand2")
        stub.search_results = [[mk_post(55), mk_post(56)]]
        await drive(s_random(plugin, FakeEvent(umo=umo), "词 2"))
        stub.search_results = [[mk_post(56), mk_post(57)]]  # 56 重复出现
        await drive(s_random(plugin, FakeEvent(umo=umo), "词 2"))
        check("random: 连续两轮 seen 排除", stub.search_calls[-1][4] == (55, 56), str(stub.search_calls[-1]))

        # B9 下载优化：auto_sample 取样图 / gif 透传不转码 / 下载总时长硬顶
        plugin.config.update(image_quality="original", convert_enabled=True,
                             convert_max_px=1080, convert_quality=95,
                             auto_sample=True, download_timeout=10)
        try:
            umo = fresh_umo("mat")
            stub.page_results = [[mk_post(71)]]
            await drive(s_search(plugin, FakeEvent(umo=umo), "样图 1"))
            check("mat: original+转码自动取样图", stub.fetch_calls and stub.fetch_calls[-1] == (71, "jpeg"),
                  str(stub.fetch_calls))

            gp = mk_post(72)
            gp["jpeg_url"] = "https://files.yande.re/jpeg/72/test.gif"
            gp["file_url"] = "https://files.yande.re/image/72/test.gif"
            stub.page_results = [[gp]]
            await drive(s_search(plugin, FakeEvent(umo=umo), "动图 1"))
            gif_dest = plugin.cache_dir / f"{gp['md5']}_jpeg.gif"
            check("mat: gif 透传不转码", gif_dest.exists() and gif_dest.read_bytes() == PNG_BYTES,
                  str(list(plugin.cache_dir.glob('*72*')) if plugin.cache_dir.exists() else 'no cache dir'))

            plugin.config["download_timeout"] = 0.2

            async def always_slow(pid, q):
                await asyncio.sleep(0.6)

            stub.fetch_hook = always_slow
            stub.page_results = [[mk_post(73)]]
            ev = FakeEvent(umo=umo)
            await drive(s_search(plugin, ev, "超时 1"))
            check("mat: 下载全超时兜底提示", any("下载全部失败" in t for t in plains(ev)), str(plains(ev)))
            check("mat: 超时前先降级 preview", stub.fetch_calls[-2:] == [(73, "jpeg"), (73, "preview")],
                  str(stub.fetch_calls))

            async def slow_non_preview(pid, q):
                if q != "preview":
                    await asyncio.sleep(0.6)

            stub.fetch_hook = slow_non_preview
            stub.page_results = [[mk_post(74)]]
            ev = FakeEvent(umo=umo)
            await drive(s_search(plugin, ev, "降级 1"))
            check("mat: 超时降级 preview 出图", bool(chains(ev)) and stub.fetch_calls[-2:] == [(74, "jpeg"), (74, "preview")],
                  str(stub.fetch_calls[-2:]))
        finally:
            stub.fetch_hook = None
            stub.fetch_delay = 0.0
            plugin.config.update(image_quality="preview", convert_enabled=False, download_timeout=30)

        # B10 数量参数：要 10 张被 clamp 到 3，回显注明
        umo = fresh_umo("cap")
        stub.page_results = [[mk_post(80), mk_post(81), mk_post(82)]]
        ev = FakeEvent(umo=umo)
        await drive(s_search(plugin, ev, "词 10"))
        check("count: 超限截断提示", any("一次最多 3 张" in t for t in plains(ev)), str(plains(ev)))
        check("count: 实发 clamp 3 张", plains(ev) and plains(ev)[0].count("#") == 3, str(plains(ev)[:1]))

        # B11 分级直觉词：r18 识别为分级（不是标签/数量），被上限拦时提示解锁方法
        umo = fresh_umo("r18tok")
        stub.search_results = [[mk_post(85)]]
        ev = FakeEvent(umo=umo)  # 群聊，上限 s
        await drive(s_random(plugin, ev, "词 r18 2"))
        call = stub.search_calls[-1]
        check("rating: r18 直觉词识别", call[0] == ("词",) and call[1] == 4 and call[3] == "s", str(call))
        check("rating: 降级提示含解锁方法", any("/搜图设置 r18 on" in t for t in plains(ev)), str(plains(ev)))

        # B12 /p 合一：默认随机；精选切质量优先
        umo = fresh_umo("p1")
        stub.search_results = [[mk_post(86)]]
        ev = FakeEvent(umo=umo)
        await drive(plugin.cmd_p(ev, "词 2"))
        check("p: 默认随机", stub.search_calls[-1][2] == "random" and stub.search_calls[-1][1] == 4,
              str(stub.search_calls[-1]))
        stub.page_results = [[mk_post(87), mk_post(88)]]
        ev = FakeEvent(umo=fresh_umo("p2"))
        await drive(plugin.cmd_p(ev, "词 精选 2"))
        check("p: 精选=质量优先", stub.page_calls[-1][3] == "score" and stub.page_calls[-1][0] == ("词",),
              str(stub.page_calls[-1]))

        # B13 下载质量保障：超时优先池内备胎；无备胎才预览档且回显标注
        plugin.config.update(image_quality="original", convert_enabled=True,
                             auto_sample=False, download_timeout=0.2)
        try:
            umo = fresh_umo("spare")
            stub.page_results = [[mk_post(95), mk_post(96), mk_post(97), mk_post(98)]]

            async def fail_big(pid, q):
                if pid in (95, 96) and q != "preview":
                    await asyncio.sleep(0.6)

            stub.fetch_hook = fail_big
            ev = FakeEvent(umo=umo)
            await drive(s_search(plugin, ev, "备胎 2"))
            txt = plains(ev)[0] if plains(ev) else ""
            check("spare: 超时图用备胎替换", "#97" in txt and "#98" in txt and "#95" not in txt, txt[:90])
            check("spare: 备胎保持原档位", stub.fetch_calls[-2:] == [(97, "original"), (98, "original")],
                  str(stub.fetch_calls))
            check("spare: 替换有提示", "备胎" in txt, txt[-60:])

            stub.page_results = [[mk_post(99)]]

            async def fail_99(pid, q):
                if pid == 99 and q != "preview":
                    await asyncio.sleep(0.6)

            stub.fetch_hook = fail_99
            ev = FakeEvent(umo=fresh_umo("pv"))
            await drive(s_search(plugin, ev, "兜底 1"))
            txt = plains(ev)[0] if plains(ev) else ""
            check("spare: 无备胎预览兜底+标注", "(预览)" in txt
                  and stub.fetch_calls[-2:] == [(99, "original"), (99, "preview")],
                  f"{txt[:60]} | {stub.fetch_calls[-2:]}")
        finally:
            stub.fetch_hook = None
            plugin.config.update(image_quality="preview", convert_enabled=False,
                                 auto_sample=True, download_timeout=30)

        # B14 AstrBot 参数截断回归：GreedyStr 带默认值时 AstrBot 4.28 只绑定首词，
        # 命令参数必须以 event.message_str 为准（_raw_args 兜底绑定值仅本地直呼用）
        umo = fresh_umo("raw1")
        stub.search_results = [[mk_post(101)]]
        ev = FakeEvent(text="p 猫娘 r18", umo=umo)
        await drive(plugin.cmd_p(ev, "猫娘"))  # 第二参模拟被截断的绑定值
        check("raw: message_str 里的 r18 被识别(降级提示)",
              any("R18 需管理员" in t for t in plains(ev)), str(plains(ev))[:90])
        check("raw: r18 的 18 不当成数量(无误报提示)",
              not any("一次最多" in t for t in plains(ev)), str(plains(ev))[:90])

        stub.search_results = [[mk_post(102)]]
        ev = FakeEvent(text="p 猫娘 3", umo=fresh_umo("raw2"))
        await drive(plugin.cmd_p(ev, "猫娘"))
        check("raw: message_str 里的数量生效(3+2)", stub.search_calls[-1][1] == 5,
              str(stub.search_calls[-1]))

        stub.search_results = [[mk_post(103)]]
        ev = FakeEvent(text="p 猫娘 AND 萝莉", umo=fresh_umo("raw3"))
        await drive(plugin.cmd_p(ev, "猫娘"))
        check("raw: AND 连接词丢弃", stub.search_calls[-1][0] == ("catgirl", "loli"),
              str(stub.search_calls[-1]))

        ev = FakeEvent(text="搜图设置 r18 on", umo=fresh_umo("raw4"))
        await drive(plugin.cmd_set(ev, "r18"))
        check("raw: dbset 完整参数 r18 on", plugin.gov.get_chat(ev.unified_msg_origin).get("r18_ok") == 1,
              "r18_ok 未置位")
        stub.search_results = [[mk_post(104)]]
        ev2 = FakeEvent(text="p 萝莉 r18", umo=ev.unified_msg_origin)
        await drive(plugin.cmd_p(ev2, "萝莉"))
        check("raw: r18 解锁后直达 rating:e", stub.search_calls[-1][3] == "e"
              and "rating:e" in plains(ev2)[0], f"{stub.search_calls[-1]} | {plains(ev2)[:60]}")

        # B15 插件管理员：admin_users 独立于 AstrBot admins_id；拒绝要有提示而非静默
        plugin.config.update(admin_users="")
        ev = FakeEvent(umo=fresh_umo("adm1"), admin=False)
        await drive(plugin.cmd_set(ev, ""))
        check("admin: 非管理员拒绝且有提示", any("插件管理员" in t for t in plains(ev)),
              str(plains(ev))[:80])
        ev = FakeEvent(umo=fresh_umo("admx"), admin=False)
        await drive(plugin.cmd_searchmode(ev, "on"))
        check("admin: 搜图模式同样拦截", any("插件管理员" in t for t in plains(ev)),
              str(plains(ev))[:80])

        plugin.config.update(admin_users="10001")  # FakeEvent 默认 uid
        ev = FakeEvent(umo=fresh_umo("adm2"), admin=False)
        await drive(plugin.cmd_set(ev, ""))
        check("admin: admin_users 命中放行", any("本会话搜图设置" in t for t in plains(ev)),
              str(plains(ev))[:80])

        plugin.config.update(admin_users="")
        ev = FakeEvent(umo=fresh_umo("adm3"), admin=True)
        await drive(plugin.cmd_set(ev, ""))
        check("admin: AstrBot 管理员兜底仍有效", any("本会话搜图设置" in t for t in plains(ev)),
              str(plains(ev))[:80])

        # B17 群级会话键：本部署群聊 umo 是 {sender}_{group}，同群不同人必须共享设置
        g = "aiocqhttp:GroupMessage:777888"
        plugin.config.update(admin_users="10001")
        ev = FakeEvent(umo=f"aiocqhttp:GroupMessage:10001_777888", uid="10001")
        await drive(plugin.cmd_set(ev, "r18 on"))
        check("chatkey: /dbset 写在群级键上",
              plugin.gov.get_chat(g).get("r18_ok") == 1,
              str(plugin.gov.get_chat(g)))
        stub.search_results = [[mk_post(105)]]
        ev2 = FakeEvent(umo=f"aiocqhttp:GroupMessage:99999_777888", uid="99999")
        await drive(plugin.cmd_p(ev2, "词 r18"))
        check("chatkey: 同群他人请求继承解锁(e)",
              stub.search_calls[-1][3] == "e" and "rating:e" in plains(ev2)[0],
              f"{stub.search_calls[-1]} | {plains(ev2)[:60]}")

        # B8 订阅推送：popular 推送 / tags 两轮去重 / 当日不重推 / 空推跳过
        ctx.sent.clear()
        umo = fresh_umo("sub")
        await drive(plugin.cmd_sub(FakeEvent(umo=umo), "热门 08:00"))
        hhmm = (datetime.now() - timedelta(minutes=1)).strftime("%H:%M")
        plugin.gov._db.execute("UPDATE subs SET hh_mm=?", (hhmm,))
        plugin.gov._db.commit()
        stub.popular_map = {("day", 0): [mk_post(61), mk_post(62)]}
        await plugin._scheduler.tick()
        check("sub: popular 推送", any(u == umo for u, _ in ctx.sent), "no send")

        sid = plugin.gov.list_subs(umo)[0]["id"]
        ev = FakeEvent(umo=umo)
        await drive(plugin.cmd_unsub(ev, str(sid)))
        check("sub: 退订", any("已退订" in t for t in plains(ev)) and not plugin.gov.list_subs(umo))

        # tags 订阅：两轮推送验证 last_ids 去重
        ctx.sent.clear()
        umo2 = fresh_umo("sub2")
        await drive(plugin.cmd_sub(FakeEvent(umo=umo2), "猫耳 08:00 2"))
        sub2 = plugin.gov.list_subs(umo2)[0]
        plugin.gov._db.execute("UPDATE subs SET hh_mm=?", (hhmm,))
        plugin.gov._db.commit()
        stub.search_results = [[mk_post(71), mk_post(72)], [mk_post(71), mk_post(73), mk_post(74)], []]
        await plugin._scheduler.tick()
        payload1 = json.loads(plugin.gov.list_subs(umo2)[0]["payload"])
        check("sub-tags: 首轮 last_ids", set(payload1.get("last_ids", [])) == {71, 72}, str(payload1))
        # 重置 last_run_day 触发第二轮：已推的 71 不再出现
        plugin.gov._db.execute("UPDATE subs SET last_run_day='' WHERE id=?", (sub2["id"],))
        plugin.gov._db.commit()
        ctx.sent.clear()
        await plugin._scheduler.tick()
        payload2 = json.loads(plugin.gov.list_subs(umo2)[0]["payload"])
        check("sub-tags: 二轮排除已推", set(payload2.get("last_ids", [])) >= {73, 74}, str(payload2))
        sent_text2 = "\n".join(str(c.chain) for u, c in ctx.sent if u == umo2)
        check("sub-tags: 推送内容不含已推 id", "#73" in sent_text2 and "#71(" not in sent_text2, sent_text2[:200])

        # 当日已推不重复
        ctx.sent.clear()
        await plugin._scheduler.tick()
        check("sub: 当日已推不重复", not [u for u, _ in ctx.sent if u == umo2])

        # 空推跳过
        umo3 = fresh_umo("sub3")
        await drive(plugin.cmd_sub(FakeEvent(umo=umo3), "词 07:00"))
        plugin.gov._db.execute("UPDATE subs SET hh_mm=? WHERE id=?", (hhmm, plugin.gov.list_subs(umo3)[0]["id"]))
        plugin.gov._db.commit()
        await plugin._scheduler.tick()
        check("sub: 空推静默跳过", not [u for u, _ in ctx.sent if u == umo3])

        # B9 收藏越界 / 我的收藏翻页
        umo = fresh_umo("fav")
        stub.page_results = [[mk_post(81), mk_post(82)]]
        await drive(s_search(plugin, FakeEvent(umo=umo), "x 2"))
        ev = FakeEvent(umo=umo)
        await drive(plugin.cmd_fav(ev, "9"))
        check("fav: 序号越界提示", any("取值 1-2" in t for t in plains(ev)), str(plains(ev)))
        await drive(plugin.cmd_fav(FakeEvent(umo=umo), "1"))
        await drive(plugin.cmd_fav(FakeEvent(umo=umo), "1"))
        ev = FakeEvent(umo=umo)
        await drive(plugin.cmd_favlist(ev, "5"))
        check("favlist: 越界页提示", any("没有第 5 页" in t for t in plains(ev)), str(plains(ev)))
        ev = FakeEvent(umo=umo)
        await drive(plugin.cmd_favlist(ev, ""))
        check("favlist: 首页含链接", any("yande.re/post/show/81" in t for t in plains(ev)), str(plains(ev)))
    finally:
        if real:
            plugin._clients["yandere"] = real
        else:
            plugin._clients.pop("yandere", None)


# ---------------- C. 治理矩阵 ----------------

async def section_c(plugin):
    print("\n== C. 治理矩阵 ==")
    # 白名单外的会话开 e 被拒
    umo_out = fresh_umo("wl")
    ev = FakeEvent(umo=umo_out)
    await drive(plugin.cmd_set(ev, "rating e"))
    check("gov: 未解锁拒开 e", any("未解锁 R18" in t for t in plains(ev)), str(plains(ev)))
    # 白名单内允许（UMO 在 CFG.r18_whitelist）
    ev = FakeEvent(umo=UMO)
    await drive(plugin.cmd_set(ev, "rating e"))
    check("gov: 白名单内允许 e", any("→ rating:e" in t for t in plains(ev)))
    await drive(plugin.cmd_set(FakeEvent(umo=UMO), "rating s"))

    # r18 一键解锁/关闭周期 + 分级子命令别名
    umo = fresh_umo("r18cmd")
    ev = FakeEvent(umo=umo)
    await drive(plugin.cmd_set(ev, "r18 on"))
    chat = plugin.gov.get_chat(umo)
    check("r18: 一键解锁并开 e", chat["rating_cap"] == "e" and chat["r18_ok"] == 1)
    await drive(plugin.cmd_set(FakeEvent(umo=umo), "分级 涩图"))
    check("r18: 分级接受直觉词", any("→ rating:e" in t for t in plains(ev)))
    await drive(plugin.cmd_set(FakeEvent(umo=umo), "r18 off"))
    chat = plugin.gov.get_chat(umo)
    check("r18: 关闭恢复群默认", chat["rating_cap"] == "s" and chat["r18_ok"] == 0)

    # 冷却：管理员豁免（先 touch 模拟刚执行过命令）
    plugin.gov.set_chat(UMO, cooldown_s=60)
    plugin.gov.touch_command(UMO)
    check("gov: 管理员豁免冷却", plugin.gov.check(UMO, "1", True) == (True, ""))
    ok, why = plugin.gov.check(UMO, "2", False)
    check("gov: 非管理员被冷却", not ok and "冷却" in why, why)
    plugin.gov.set_chat(UMO, cooldown_s=0)

    # 配额：管理员豁免 + 计数
    plugin.gov.set_chat(UMO, quota=1)
    plugin.gov.record_usage(UMO, "u9", 1)
    ok, why = plugin.gov.check(UMO, "u9", False)
    check("gov: 配额用尽被拦", not ok and "配额" in why, why)
    check("gov: 管理员豁免配额", plugin.gov.check(UMO, "u9", True) == (True, ""))
    plugin.gov.set_chat(UMO, quota=80)

    # 总开关
    umo = fresh_umo("off")
    await drive(plugin.cmd_set(FakeEvent(umo=umo), "开关 off"))
    ev = FakeEvent(umo=umo)
    await drive(s_search(plugin, ev, "catgirl 1"))
    check("gov: 关闭后命令被拦", any("已被管理员关闭" in t for t in plains(ev)), str(plains(ev)))

    # 历史 TTL 过期
    umo = fresh_umo("ttl")
    plugin.gov.set_history(umo, "10001", "score", {"site": "yandere", "tags": ["x"], "rating": "s",
                                                   "offset": 1, "pool": [mk_post(1)], "posts": [mk_post(1)]})
    plugin.gov._db.execute("UPDATE history SET ts=0")
    plugin.gov._db.commit()
    assert plugin.gov.get_history(umo, "10001") is None
    ev = FakeEvent(umo=umo)
    await drive(plugin.cmd_next(ev, ""))
    check("gov: TTL 过期后无续看", any("没有可续看" in t for t in plains(ev)), str(plains(ev)))

    # 下一张无历史（不同用户）
    ev = FakeEvent(umo=fresh_umo("nohist"), uid="88888")
    await drive(plugin.cmd_next(ev, ""))
    check("next: 无历史提示", any("没有可续看" in t for t in plains(ev)))

    # seen：随机去重持久化（写入/淘汰/隔离/ts 刷新）
    gov = plugin.gov
    gov.add_seen("su", "yandere", "词|s", [1, 2, 3])
    check("seen: 写入读取", gov.get_seen("su", "yandere", "词|s") == {1, 2, 3})
    gov.add_seen("su", "yandere", "词|s", [4], keep=3)
    check("seen: keep 淘汰最旧", gov.get_seen("su", "yandere", "词|s") == {2, 3, 4})
    check("seen: qkey 隔离", gov.get_seen("su", "yandere", "k2") == set()
          and gov.get_seen("su", "konachan", "词|s") == set())
    gov.add_seen("su", "yandere", "词|s", [2], keep=3)
    gov.add_seen("su", "yandere", "词|s", [5], keep=3)
    check("seen: 刷新 ts 免淘汰", gov.get_seen("su", "yandere", "词|s") == {2, 4, 5})


# ---------------- D. 反搜矩阵（MockTransport） ----------------

SAUCENAO_FIXTURE = {
    "header": {"status": 0, "long_remaining": 95},
    "results": [
        {"header": {"similarity": "93.42", "thumbnail": "https://img.saucenao.com/x.jpg",
                    "index_name": "Index #5: Pixiv Images"},
         "data": {"ext_urls": ["https://www.pixiv.net/artworks/12345678"], "pixiv_id": 12345678,
                  "title": "ねこみみ", "member_name": "authorA"}},
        {"header": {"similarity": "88.10", "thumbnail": "https://img3.saucenao.com/y.jpg",
                    "index_name": "Index #13: yande.re"},
         "data": {"ext_urls": ["https://yande.re/post/show/999888"], "yandere_id": 999888,
                  "material": "catgirl"}},
    ],
}

IQDB_FIXTURE = """<html><body>
<table><tr><td class="image"><a href="https://yande.re/post/show/999888"><img src="https://iqdb.org/thu/a.jpg"></a></td></tr>
<tr><td>1500x2000</td><td class="similarity">98% similarity</td></tr></table>
<table><tr><td class="image"><a href="//danbooru.donmai.us/posts/11938661"><img src="https://iqdb.org/thu/b.jpg"></a></td></tr>
<tr><td>1500x2000</td><td class="similarity">91% similarity</td></tr></table>
<table><tr><td class="image"><a href="//gelbooru.com/index.php?page=post&s=view&id=3518901"></a></td></tr>
<tr><td>800x1200</td><td class="similarity">85% similarity</td></tr></table>
<table><tr><th>Your image</th></tr><tr><td><img src="https://iqdb.org/thu/thu_q.jpg"></td></tr></table>
</body></html>"""

CF_FIXTURE = "<html><head><title>Just a moment...</title></head><body>cf challenge</body></html>"


async def section_d(plugin):
    print("\n== D. 反搜矩阵 ==")
    from astrbot_plugin_yandere_search.reverse import ReverseSearcher

    calls = {"saucenao": 0, "iqdb": 0}
    iqdb_multipart = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "saucenao" in url:
            calls["saucenao"] += 1
            if calls["saucenao"] == 1 and handler.cf_first:
                return httpx.Response(403, text=CF_FIXTURE)
            return httpx.Response(200, json=SAUCENAO_FIXTURE)
        calls["iqdb"] += 1
        iqdb_multipart.append(request.headers.get("content-type", ""))
        return httpx.Response(200, text=IQDB_FIXTURE)

    handler.cf_first = False

    def make(gate=None):
        s = ReverseSearcher(iqdb_enabled=True, daily_gate=gate)
        s._client.aclose()
        s._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return s

    # D1 SauceNAO 正常解析（高相似 → 不打 iqdb）
    s = make()
    res = await s.search(image_url="https://x.example/a.jpg")
    top = res["results"][0]
    check("rev: saucenao pixiv 解析", top["site"] == "pixiv" and top["post_id"] == 12345678
          and abs(top["similarity"] - 93.42) < 0.01 and top["author"] == "authorA", str(top))
    check("rev: saucenao yande 命中", any(r["site"] == "yandere" and r["post_id"] == 999888 for r in res["results"]))
    check("rev: 高相似不查 iqdb", calls["iqdb"] == 0 and res["saucenao_remaining"] == 95)
    await s.close()

    # D2 iqdb 兜底解析（saucenao 低相似触发）+ multipart 断言 + 去重合并
    low = {"header": {"status": 0, "long_remaining": 90},
           "results": [{"header": {"similarity": "40.00", "thumbnail": "https://t/x.jpg",
                                   "index_name": "Index #5"},
                        "data": {"ext_urls": ["https://yande.re/post/show/999888"]}}]}
    orig = dict(SAUCENAO_FIXTURE)
    handler.cf_first = False
    s = make()
    # 用 monkeypatch 换 fixture: 先记 handler 引用不便，改用闭包变量
    fixture_box = {"sn": SAUCENAO_FIXTURE}

    def handler2(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "saucenao" in url:
            return httpx.Response(200, json=fixture_box["sn"])
        iqdb_multipart.append(request.headers.get("content-type", ""))
        return httpx.Response(200, text=IQDB_FIXTURE)

    s._client.aclose()
    s._client = httpx.AsyncClient(transport=httpx.MockTransport(handler2))
    fixture_box["sn"] = low
    res = await s.search(image_url="https://x.example/a.jpg")
    check("rev: iqdb multipart 表单", iqdb_multipart and iqdb_multipart[-1].startswith("multipart/form-data"),
          str(iqdb_multipart))
    check("rev: iqdb 解析三源", {r["site"] for r in res["results"]} == {"yandere", "danbooru", "gelbooru"},
          str([(r["site"], r["post_id"]) for r in res["results"]]))
    check("rev: (site,id) 去重保留高相似", len([r for r in res["results"] if r["site"] == "yandere"]) == 1)
    check("rev: iqdb 协议相对链接补 https", all(r["url"].startswith("https://") for r in res["results"]))
    await s.close()

    # D3 CF 拦截 → 退避 10 分钟（第二次直接跳过 saucenao）
    state = {"sn_403": True}

    def handler3(request: httpx.Request) -> httpx.Response:
        if "saucenao" in str(request.url):
            if state["sn_403"]:
                return httpx.Response(403, text=CF_FIXTURE)
            return httpx.Response(200, json=SAUCENAO_FIXTURE)
        return httpx.Response(200, text=IQDB_FIXTURE)

    s = ReverseSearcher(iqdb_enabled=True)
    s._client.aclose()
    s._client = httpx.AsyncClient(transport=httpx.MockTransport(handler3))
    res1 = await s.search(image_url="https://x/b.jpg")
    check("rev: CF 提示+iqdb 兜底", any("Cloudflare" in n for n in res1["notes"])
          and any(r["engine"] == "iqdb" for r in res1["results"]), str(res1["notes"]))
    state["sn_403"] = False
    res2 = await s.search(image_url="https://x/b.jpg")
    check("rev: CF 退避期内不再打 saucenao", any("临时不可用" in n for n in res2["notes"]), str(res2["notes"]))
    await s.close()

    # D4 每日配额门
    s = ReverseSearcher(iqdb_enabled=True, daily_gate=lambda key: False)
    s._client.aclose()
    s._client = httpx.AsyncClient(transport=httpx.MockTransport(handler3))
    res = await s.search(image_url="https://x/c.jpg")
    check("rev: 配额用尽只用 iqdb", any("额度已用完" in n for n in res["notes"])
          and all(r["engine"] == "iqdb" for r in res["results"]))
    await s.close()

    # D5 /搜原图：命中本站图源自动补图（过分级门）+ Reply 引用图提取
    stub_booru = StubBooru()
    stub_booru.get_post_map[999888] = mk_post(999888, rating="s")
    real_client = plugin._clients.get("yandere")
    plugin._clients["yandere"] = stub_booru
    real_searcher = plugin.searcher
    plugin.searcher = StubSearcher(results=[
        {"engine": "iqdb", "similarity": 98.0, "index": "iqdb", "thumbnail": "",
         "site": "yandere", "post_id": 999888, "url": "https://yande.re/post/show/999888",
         "title": "", "author": ""},
        {"engine": "iqdb", "similarity": 91.0, "index": "iqdb", "thumbnail": "",
         "site": "danbooru", "post_id": 11938661, "url": "https://danbooru.donmai.us/posts/11938661",
         "title": "", "author": ""},
    ], remaining=7)
    try:
        reply_comp = ALL.Reply(chain=[ALL.Image(url="https://multimedia.nt.qq.com/x.jpg")])
        ev = FakeEvent(comps=[reply_comp], umo=UMO)
        await drive(plugin.cmd_sauce(ev, ""))
        check("sauce: Reply 内嵌图提取", plugin.searcher.calls and plugin.searcher.calls[0][0] == "https://multimedia.nt.qq.com/x.jpg",
              str(plugin.searcher.calls))
        check("sauce: 结果文本", any("以图搜图" in t and "98%" in t for t in plains(ev)), str(plains(ev)))
        check("sauce: 命中本站自动补图", any(isinstance(c[0], ALL.Image) for c in chains(ev)), str(len(chains(ev))))
        # 超分级省略图
        stub_booru.get_post_map[999888] = mk_post(999888, rating="e")
        ev = FakeEvent(comps=[ALL.Image(url="https://x/1.jpg")], umo=UMO)
        await drive(plugin.cmd_sauce(ev, ""))
        check("sauce: 超分级省略图片", any("已省略图片" in t for t in plains(ev)), str(plains(ev)))
        # 无图用法
        ev = FakeEvent(umo=UMO)
        await drive(plugin.cmd_sauce(ev, ""))
        check("sauce: 无图提示用法", any("回复一张图片" in t for t in plains(ev)))
        # Reply 引用的历史图无直链（只有文件名）→ get_image 换 URL
        fakebot = FakeBot({"url": "https://multimedia.nt.qq.com/download/resolved.jpg"})
        ev = FakeEvent(comps=[ALL.Reply(chain=[ALL.Image(file="ABCD1234.jpg")])], umo=UMO, bot=fakebot)
        await drive(plugin.cmd_sauce(ev, ""))
        check("sauce: get_image 恢复直链",
              plugin.searcher.calls and plugin.searcher.calls[-1][0] == "https://multimedia.nt.qq.com/download/resolved.jpg"
              and fakebot.calls and fakebot.calls[0][0] == "get_image",
              f"calls={plugin.searcher.calls} bot={fakebot.calls}")
    finally:
        plugin.searcher = real_searcher
        if real_client:
            plugin._clients["yandere"] = real_client
        else:
            plugin._clients.pop("yandere", None)


# ---------------- E. 搜图模式 ----------------

async def section_e(plugin):
    print("\n== E. 搜图模式 ==")
    umo = fresh_umo("mode")
    plugin.gov.set_chat(umo, searchmode=1)
    real = plugin.searcher
    plugin.searcher = StubSearcher(results=[
        {"engine": "iqdb", "similarity": 88.0, "index": "iqdb", "thumbnail": "",
         "site": "yandere", "post_id": 1, "url": "https://yande.re/post/show/1", "title": "", "author": ""}])
    try:
        ev = FakeEvent("看看这张", comps=[ALL.Image(url="https://x/1.jpg")], umo=umo)
        await plugin.on_any_message(ev)
        check("mode: 图片自动反搜回复", any("以图搜图" in str(c.chain) for c in sends(ev)), "no send")
        ev2 = FakeEvent("又一张", comps=[ALL.Image(url="https://x/2.jpg")], umo=umo)
        await plugin.on_any_message(ev2)
        check("mode: 30s 节流", not sends(ev2) and len(plugin.searcher.calls) == 1)
        ev3 = FakeEvent("/搜图 x", comps=[ALL.Image(url="https://x/3.jpg")], umo=umo)
        await plugin.on_any_message(ev3)
        check("mode: '/' 命令跳过", not sends(ev3))
        plugin.gov.set_chat(umo, searchmode=0)
        plugin._rev_ts.clear()
        ev4 = FakeEvent("关闭后", comps=[ALL.Image(url="https://x/4.jpg")], umo=umo)
        await plugin.on_any_message(ev4)
        check("mode: 关闭后不反搜", not sends(ev4))
    finally:
        plugin.searcher = real
        plugin.gov.set_chat(umo, searchmode=0)


# ---------------- F. live 网络 ----------------

async def _retry(fn, n=3, wait=2.0):
    """live 请求带重试；全部失败抛最后一个异常。"""
    last = None
    for i in range(n):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < n - 1:
                await asyncio.sleep(wait)
    raise last


async def section_f(plugin, ctx):
    print("\n== F. live 网络 ==")
    client = plugin.client()
    if "--debug-live" in sys.argv:
        try:
            r = await client._client.get("/post.json", params={"tags": "catgirl rating:s", "limit": 3, "page": 1})
            print("DEBUG RAW:", r.status_code, r.headers.get("content-type"), "|", r.text[:120].replace("\n", " "))
        except Exception as exc:
            print("DEBUG RAW EXC:", exc.__class__.__name__, str(exc)[:120])
        print("DEBUG clients:", list(plugin._clients))

    try:
        pool = await _retry(lambda: client.page_posts(["catgirl"], page=1, rating="s"))
    except Exception as exc:  # noqa: BLE001
        print(f"  ↳ page_posts 网络失败: {exc.__class__.__name__}: {str(exc)[:80]}")
        pool = []
    check("live: page_posts", len(pool) > 0, f"{len(pool)} posts")

    if pool:
        try:
            page2 = await _retry(lambda: client.page_posts(["catgirl"], page=2, rating="s"))
        except Exception:
            page2 = []
        if page2:
            check("live: page2 无重叠", not {p["id"] for p in pool} & {p["id"] for p in page2})
        try:
            newest = await _retry(lambda: client.search(["catgirl"], limit=3, order="none", rating="s"))
        except Exception:
            newest = []
        check("live: order=none 最新序", len(newest) == 3 and newest[0]["id"] > newest[-1]["id"],
              str([p["id"] for p in newest]))
        try:
            rnd = await _retry(lambda: client.search(["catgirl"], limit=3, order="random",
                                                     rating="s", exclude={pool[0]["id"]}))
        except Exception:
            rnd = []
        check("live: random+exclude", 1 <= len(rnd) <= 3 and all(p["id"] != pool[0]["id"] for p in rnd),
              str(len(rnd)))
        # yande.re order:random 对部分标签稳定返回空（服务端 quirk）→ 退化为高分池抽样
        try:
            qg = await _retry(lambda: client.search(["china_dress"], limit=2, order="random", rating="s"))
        except Exception:
            qg = []
        check("live: random 退化兜底", len(qg) >= 1, str([p["id"] for p in qg]))
        # 回归：quirk 标签连续两轮随机（模拟用户连发 /dbr）→ 第二轮排除第一轮已发图
        seen_ids = set()
        try:
            r1 = await _retry(lambda: client.search(["loli"], limit=3, order="random", rating="s"))
            seen_ids = {p["id"] for p in r1}
            r2 = await _retry(lambda: client.search(["loli"], limit=3, order="random", rating="s",
                                                    exclude=seen_ids))
        except Exception:
            r1, r2 = [], []
        check("live: quirk 连续两轮不重复",
              len(r1) >= 1 and len(r2) >= 1 and not seen_ids & {p["id"] for p in r2},
              f"{sorted(seen_ids)} / {[p['id'] for p in r2]}")
    for period in ("day", "week", "month"):
        try:
            pop = await _retry(lambda p=period: client.popular(p))
        except Exception:
            pop = []
        check(f"live: popular {period}", len(pop) > 0)
    try:
        pop1 = await _retry(lambda: client.popular("day", days_back=1))
    except Exception:
        pop1 = []
    check("live: popular days_back", len(pop1) > 0)
    if pool:
        try:
            got = await _retry(lambda: client.get_post(pool[0]["id"]))
        except Exception:
            got = None
        check("live: get_post", got and got["id"] == pool[0]["id"])
    try:
        cnt = await _retry(lambda: client.count(["catgirl"]))
    except Exception:
        cnt = 0
    check("live: count", cnt > 10000, str(cnt))
    try:
        sug = await _retry(lambda: client.suggest_tags("nekomi"))
    except Exception:
        sug = []
    check("live: suggest", any(n == "nekomimi" for n, _ in sug), str(sug))

    # 命令真实流
    ev = FakeEvent(umo=fresh_umo("live1"))
    await drive(s_search(plugin, ev, "巨乳 3"))
    check("live: 搜图3图合并转发", bool(chains(ev)) and isinstance(chains(ev)[0][0], ALL.Nodes),
          str(plains(ev)[:1]))
    ev = FakeEvent(umo=ev.unified_msg_origin)
    await drive(s_random(plugin, ev, "旗袍 2"))
    got_imgs = [c for c in chains(ev) if isinstance(c[0], ALL.Image)]
    check("live: 随机2图逐张", 1 <= len(got_imgs) <= 2 and len(chains(ev)) == len(got_imgs),
          f"{len(got_imgs)} imgs")
    # 回归：同一标签连续两次 /随机 命令，第二轮不得与第一轮重复
    ids1 = set(re.findall(r"#(\d+)", " ".join(plains(ev))))
    ev = FakeEvent(umo=ev.unified_msg_origin)
    await drive(s_random(plugin, ev, "旗袍 2"))
    ids2 = set(re.findall(r"#(\d+)", " ".join(plains(ev))))
    check("live: 连续随机命令不重复", ids1 and ids2 and not ids1 & ids2,
          f"{sorted(ids1)} vs {sorted(ids2)}")
    ev = FakeEvent(umo=fresh_umo("live2"))
    await drive(plugin.cmd_hot(ev, "本周 2"))
    check("live: 热门本周", bool(chains(ev)) or any("热门" in t for t in plains(ev)))
    ev = FakeEvent(umo=fresh_umo("live3"))
    await drive(plugin.cmd_tag(ev, "nekomimi"))
    check("live: 标签en→zh", any("→" in t for t in plains(ev)))

    # 真实反搜（iqdb；SauceNAO 本网络被 CF 拦为预期降级）
    url = (pool[0].get("preview_url") or pool[0]["jpeg_url"]) if pool else None
    if url:
        try:
            res = await _retry(lambda: plugin.searcher.search(image_url=url))
        except Exception as exc:
            res = {"results": [], "notes": [str(exc)]}
        check("live: 反搜有结果", bool(res["results"]),
              f"engines={res.get('engines')} notes={res.get('notes')}")
    else:
        check("live: 反搜有结果", False, "取样池为空")
    # site 前缀（konachan DC-IP 403 视为环境跳过）
    ev = FakeEvent(umo=fresh_umo("live4"))
    await drive(s_search(plugin, ev, "wallpaper site:konachan 1"))
    check("live: site 切换反馈", any("konachan" in t or "403" in t for t in plains(ev)), str(plains(ev)))


# ---------------- B18. 语义标签匹配（嵌入召回+重排，离线 stub） ----------------

async def embed_tests(plugin):
    print("\n== B18. 语义标签匹配 ==")
    import numpy as np
    from astrbot_plugin_yandere_search.embedder import EmbedTagger

    idx_dir = TMP / "embed_idx"
    idx_dir.mkdir(parents=True, exist_ok=True)
    tags = ["pantyhose", "nekomimi", "maid"]
    texts = ["pantyhose 黑丝", "nekomimi 兽耳", "maid 女仆"]
    np.savez_compressed(idx_dir / "tag_index.npz", emb=np.eye(4, dtype=np.float16)[:3])
    (idx_dir / "tag_index_meta.json").write_text(json.dumps(
        {"model": "bge-m3", "dim": 4, "n": 3, "built": 0, "tags": tags, "texts": texts,
         "counts": [9000, 4000, 3000], "aliases": [False, False, False]},
        ensure_ascii=False), encoding="utf-8")

    class CtxCfg:
        """4.28 插件 Context 的配置入口：get_config()（无 umo 返回默认配置）"""
        def __init__(self, cfg):
            self._cfg = cfg

        def get_config(self, umo=None):
            return self._cfg

    prov_list = [
        {"type": "openai_embedding", "enable": True, "embedding_api_base": "https://emb.example/v1",
         "embedding_api_key": "k", "embedding_model": "bge-m3"},
        {"type": "vllm_rerank", "enable": True, "rerank_api_base": "https://rr.example",
         "rerank_api_suffix": "/v1/rerank", "rerank_api_key": "", "rerank_model": "rrm"},
    ]
    t1 = EmbedTagger.create(CtxCfg({"provider": prov_list}), idx_dir, {"embed_enabled": True}, plugin.gov)
    check("embed: get_config 自动发现",
          t1 is not None and t1.emb_url == "https://emb.example/v1"
          and t1.rr_url == "https://rr.example/v1/rerank" and t1.emb_model == "bge-m3",
          "discovery failed")
    t_leg = EmbedTagger.create(
        types.SimpleNamespace(astrbot_config={"provider": prov_list}),
        idx_dir, {"embed_enabled": True}, plugin.gov)
    check("embed: 旧 astrbot_config 属性兜底", t_leg is not None)
    check("embed: 索引缺失关闭",
          EmbedTagger.create(CtxCfg({"provider": prov_list}), TMP, {"embed_enabled": True}, plugin.gov) is None)
    check("embed: 模型不一致关闭",
          EmbedTagger.create(CtxCfg({"provider": prov_list}), idx_dir,
                             {"embed_enabled": True, "embed_api_url": "https://x/v1",
                              "embed_model": "other-model"}, plugin.gov) is None)

    calls = {"embed": 0, "rerank": 0}

    async def fake_embed(texts_):
        calls["embed"] += 1
        table = {"黑丝": 0, "布偶": 1, "噼里啪啦": 0, "鬼画符": 3}
        mat = np.zeros((len(texts_), 4), dtype=np.float32)
        for i, t in enumerate(texts_):
            mat[i, table.get(t, 3)] = 1.0
        return mat

    async def fake_rerank(query, docs):
        calls["rerank"] += 1
        return [{"index": 0, "relevance_score": 0.92 if query in ("黑丝", "噼里啪啦") else 0.05}]

    assert t1 is not None
    t1._api_embed = fake_embed
    t1._api_rerank = fake_rerank

    got, rej = await t1.resolve(["黑丝", "catgirl"])
    check("embed: 命中+非CJK忽略",
          got == {"黑丝": "pantyhose"} and rej == [] and calls["embed"] == 1 and calls["rerank"] == 1,
          f"{got} {rej} {calls}")
    got, rej = await t1.resolve(["黑丝"])
    check("embed: 正样本缓存", calls["embed"] == 1, str(calls))
    got2, rej2 = await t1.resolve(["布偶"])
    check("embed: 低分定性拒绝", got2 == {} and rej2 == ["布偶"], f"{got2} {rej2}")
    got2b, rej2b = await t1.resolve(["布偶"])
    check("embed: 负样本缓存(拒绝也缓存)", calls["embed"] == 2 and rej2b == ["布偶"], str(calls))
    got3, rej3 = await t1.resolve(["随便啥"])  # 查询向量与全索引正交 → 余弦预筛拦截，不进重排
    check("embed: 余弦预筛拦截", got3 == {} and rej3 == ["随便啥"] and calls["rerank"] == 2,
          f"{got3} {rej3} {calls}")
    plugin.gov._db.execute("UPDATE tagmap SET ts=0 WHERE token='布偶'")
    plugin.gov._db.commit()
    await t1.resolve(["布偶"])
    check("embed: 负样本过期重算", calls["embed"] == 4, str(calls))
    await t1.close()

    # 集成：/p 未收录词 → 搜索用映射后标签，回显带「语义」注记；
    # 定性拒绝的词从 AND 搜索剔除（否则一个 CJK 裸词让整条查询空结果）
    stub = StubBooru()
    real = plugin._clients.get("yandere")
    plugin._clients["yandere"] = stub
    old_emb = plugin.embedder
    plugin.embedder = t1
    try:
        calls["embed"] = 0
        stub.search_results = [[mk_post(120)]]
        ev = FakeEvent(umo=fresh_umo("emb1"))
        await drive(s_random(plugin, ev, "噼里啪啦 1"))
        check("embed: /p 搜索用映射标签", stub.search_calls[-1][0] == ("pantyhose",),
              str(stub.search_calls[-1]))
        check("embed: 回显语义注记", any("语义 噼里啪啦→pantyhose" in t for t in plains(ev)),
              str(plains(ev)))
        stub.search_results = [[mk_post(121)]]
        ev = FakeEvent(umo=fresh_umo("emb2"))
        await drive(s_random(plugin, ev, "鬼画符 噼里啪啦 1"))
        check("embed: 定性拒绝词从AND剔除", stub.search_calls[-1][0] == ("pantyhose",),
              str(stub.search_calls[-1]))
        check("embed: 回显忽略注记", any("忽略未识别: 鬼画符" in t for t in plains(ev)),
              str(plains(ev)))
    finally:
        plugin.embedder = old_emb
        plugin._clients["yandere"] = real


# ---------------- B19. 连写自动拆词 ----------------

def segment_unit(plugin):
    print("\n== B19. 连写自动拆词 ==")
    check("seg: 词表锚点切分", plugin._segment("白发女仆猫娘") == ["白发", "女仆", "猫娘"],
          str(plugin._segment("白发女仆猫娘")))
    check("seg: 词表整词不拆", plugin._expand_compound(["猫娘"]) == (["猫娘"], []))
    out, src = plugin._expand_compound(["黑丝猫娘"])
    check("seg: 连写词表词拆开", out == ["黑丝", "猫娘"] and src == ["黑丝猫娘"], str((out, src)))
    check("seg: 尾部残段保留", plugin._segment("女仆装") == ["女仆", "装"])
    check("seg: 中英边界切开", plugin._segment("catgirl猫娘") == ["catgirl", "猫娘"])
    check("seg: 词表角色名直达不拆", plugin._expand_compound(["时崎狂三"])[0] == ["时崎狂三"])
    # 站内无此概念的预设剔除：白发这类发色词绝不进嵌入层（曾命中音译陷阱 blanc）
    check("seg: 发色词预设剔除", all(plugin._unsearchable(w) for w in
                                     ["白发", "黑发", "金毛", "银发", "长发", "呆毛", "魅魔"]))
    check("seg: 正常词不误杀", not any(plugin._unsearchable(w) for w in
                                       ["白发女仆", "女仆", "雷姆", "黑丝", "白发女仆猫娘"]))


async def segment_flow(plugin):
    import numpy as np
    from astrbot_plugin_yandere_search.embedder import EmbedTagger

    idx_dir = TMP / "embed_idx"  # B18 已建索引
    calls = {"embed": 0}

    async def fake_embed(texts_):
        calls["embed"] += 1
        mat = np.zeros((len(texts_), 4), dtype=np.float32)
        for i in range(len(texts_)):
            mat[i, 3] = 1.0  # 全部给与索引正交的向量 → 余弦预筛拒绝
        return mat

    t = EmbedTagger("https://e/v1", "k", "bge-m3", "https://r/rr", "", "rrm",
                    idx_dir, plugin.gov, topk=200, min_score=0.7)
    t._api_embed = fake_embed
    stub = StubBooru()
    real = plugin._clients.get("yandere")
    plugin._clients["yandere"] = stub
    old_emb = plugin.embedder
    plugin.embedder = t
    try:
        stub.search_results = [[mk_post(130)]]
        calls["embed"] = 0
        ev = FakeEvent(umo=fresh_umo("seg1"))
        await drive(s_random(plugin, ev, "白发女仆猫娘 1"))
        check("seg: AND 三词=词表两词+残段剔除",
              stub.search_calls[-1][0] == ("maid", "catgirl"), str(stub.search_calls[-1]))
        txt = " ".join(plains(ev))
        check("seg: 回显拆词注记", "拆词 白发女仆猫娘" in txt, txt[:120])
        check("seg: 回显忽略注记", "忽略未识别: 白发" in txt, txt[:120])
        check("seg: 发色词不进嵌入层", calls["embed"] == 0, str(calls))

        stub.search_results = [[mk_post(131)]]
        umo_e = fresh_umo("seg_r18")
        plugin.gov.set_chat(umo_e, r18_ok=1, rating_cap="e")
        ev = FakeEvent(umo=umo_e)
        await drive(s_random(plugin, ev, "猫娘r18 1"))
        check("seg: 拆出分级词归位(rating:e)",
              stub.search_calls[-1][0] == ("catgirl",) and stub.search_calls[-1][3] == "e",
              str(stub.search_calls[-1]))
    finally:
        plugin.embedder = old_emb
        plugin._clients["yandere"] = real
        await t.close()



# ---------------- B20. 国内源回退竞速 ----------------

class StubLolicon:
    """离线桩：Lolicon 回退源（与真实客户端同接口）。"""

    site_key, label = "lolicon", "Lolicon·pixiv"

    def __init__(self):
        self.calls = []        # (tags, limit, rating)
        self.scripted = {}     # tuple(tags) -> posts
        self.fetch_fail = set()

    @staticmethod
    def mk_loli(pid):
        return {"id": pid, "score": 0, "rating": "s", "tags": "猫娘",
                "urls": {"regular": f"https://i.pixiv.re/fake_{pid}_m1200.jpg",
                         "original": f"https://i.pximg.net/fake_{pid}.png"},
                "_site": "lolicon"}

    async def search(self, tags, limit=1, order="", rating="s", exclude=None):
        self.calls.append((tuple(tags), limit, rating))
        return list(self.scripted.get(tuple(tags), []))

    async def fetch(self, post, quality):
        if post["id"] in self.fetch_fail:
            raise BooruError("stub 下载失败")
        return f"{post['id']}.jpg", b"FAKEJPG" + str(post["id"]).encode()

    async def close(self):
        pass


async def fallback_tests(plugin):
    print("\n== B20. 国内源回退竞速 ==")
    stub = StubBooru()
    real = plugin._clients.get("yandere")
    plugin._clients["yandere"] = stub
    loli = StubLolicon()
    plugin._clients["lolicon"] = loli
    try:
        # 1) 主源快 → 不触发回退
        plugin.config.update(fallback_sites="lolicon", fallback_headstart=8, deadline_seconds=24)
        stub.search_results = [[mk_post(140)]]
        ev = FakeEvent(umo=fresh_umo("fb0"))
        await drive(s_random(plugin, ev, "猫娘 1"))
        check("fb: 主源快不回退", loli.calls == [] and any("#140" in t for t in plains(ev)),
              f"{loli.calls} {plains(ev)[:1]}")

        # 2) 主源下载慢(超 headstart) → 回退源立即交付，不等截止
        plugin.config.update(fallback_headstart=0.4, deadline_seconds=6)

        async def slow_hi(pid, q):
            await asyncio.sleep(8)  # 超过 deadline 预算，主源全档位必失败

        stub.fetch_hook = slow_hi
        loli.scripted[("猫娘",)] = [loli.mk_loli(9001)]
        stub.search_results = [[mk_post(141)]]
        t0 = time.time()
        ev = FakeEvent(umo=fresh_umo("fb1"))
        await drive(s_random(plugin, ev, "猫娘 1"))
        dt = time.time() - t0
        txt = " ".join(plains(ev))
        check("fb: 主源慢回退国内源", "Lolicon" in txt and "#9001" in txt, txt[:160])
        check("fb: 回退即时交付不等截止", dt < 3.0, f"{dt:.1f}s")
        check("fb: 回退吃原始中文词", loli.calls and loli.calls[0][0] == ("猫娘",), str(loli.calls))
        check("fb: 回退注记", "已回退国内源" in txt, txt[:160])
        stub.fetch_hook = None

        # 3) 标签阶梯：多词 OR 全空 → 退化首标签
        loli.calls.clear()
        loli.scripted[("女仆",)] = [loli.mk_loli(9002)]
        stub.search_results = [[]]
        ev = FakeEvent(umo=fresh_umo("fb2"))
        await drive(s_random(plugin, ev, "女仆 猫娘 1"))
        check("fb: 空结果退化首标签",
              len(loli.calls) == 2 and loli.calls[1][0] == ("女仆",) and "#9002" in " ".join(plains(ev)),
              str(loli.calls))

        # 4) rating e 解锁会话 → 回退源收到 e
        loli.calls.clear()
        loli.scripted[("女仆",)] = [loli.mk_loli(9003)]
        stub.search_results = [[]]
        umo_e = fresh_umo("fb3")
        plugin.gov.set_chat(umo_e, r18_ok=1, rating_cap="e")
        ev = FakeEvent(umo=umo_e)
        await drive(s_random(plugin, ev, "女仆 r18 1"))
        check("fb: rating e 传递回退源", loli.calls and loli.calls[-1][2] == "e", str(loli.calls))
    finally:
        plugin.config.update(fallback_sites="", fallback_headstart=8, deadline_seconds=24)
        plugin._clients["yandere"] = real
        plugin._clients.pop("lolicon", None)


async def main() -> None:
    ctx = FakeContext()
    plugin = m.YandereSearchPlugin(ctx, dict(CFG))
    stub_mod = sys.modules["astrbot.core.star.star_tools"]

    # 独立数据目录的第二实例：site 未启用矩阵
    orig_dir = stub_mod.StarTools._dir
    stub_mod.StarTools._dir = TMP / "alt"
    try:
        cfg2 = dict(CFG, extra_sites="")
        plugin2 = m.YandereSearchPlugin(FakeContext(), cfg2)
        ev = FakeEvent(umo=fresh_umo("gate2"))
        await drive(s_search(plugin2, ev, "catgirl site:konachan 1"))
        check("site-gate: 未启用图源拒绝", any("未启用" in t for t in plains(ev)), str(plains(ev)))
        await plugin2.terminate()
    finally:
        stub_mod.StarTools._dir = orig_dir

    dbg = "--debug-live" in sys.argv

    def snap(tag):
        if dbg:
            print(f"CLIENTS-after-{tag}:", {k: type(v).__name__ for k, v in plugin._clients.items()})

    await section_a(plugin)
    snap("A")
    await section_b(plugin, ctx)
    snap("B")
    await embed_tests(plugin)
    snap("B18")
    segment_unit(plugin)
    await segment_flow(plugin)
    await fallback_tests(plugin)
    await section_c(plugin)
    snap("C")
    await section_d(plugin)
    snap("D")
    await section_e(plugin)
    snap("E")
    if not QUICK:
        await section_f(plugin, ctx)
    else:
        print("\n== F. live 网络（--quick 跳过）==")

    await plugin.terminate()
    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
