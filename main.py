# SPDX-License-Identifier: LGPL-3.0-or-later
import asyncio
import json
import re
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.star_tools import StarTools

from .booru import MoebooruClient, BooruError, SITES
from .embedder import EmbedTagger
from .governance import Governance, RATING_ORDER
from .reverse import ReverseSearcher
from .subscribe import SubScheduler
from .tagger import LLMTagger

PLUGIN_NAME = "astrbot_plugin_yandere_search"

HELP_TEXT = """📖 yande.re 找图 · 命令一览
搜图: /p 猫娘 3        (默认随机；加 精选 走质量优先)
分级: 词尾直接加 r18 / 涩图 / 擦边 / 全年龄 (或 rating:e，受会话上限约束)；
      不写分级默认搜上限内所有等级（r18 群默认混出 s/q/e）
热门: /热门 本周 5      (别名 /hot；今日/本周/本月)
续看: /下一张 2         (别名 /next，上次搜索后 10 分钟内有效)
查词: /标签 巨乳        (别名 /tag，中英双向 + 站内数量)
反搜: 回复一张图片发 /搜原图 (别名 /sn /sauce)
收藏: /收藏 1 · /我的收藏
管理: /搜图设置 查看 | r18 on | 分级 q | 冷却 8 | 配额 80 | 转发 on | 开关 on (别名 /dbset，仅插件管理员——WebUI 本插件配置里设置)
订阅: /订阅 热门 08:00 · /订阅 猫娘 08:00 3 · /退订 1 (别名 /sub)
模式: /搜图模式 on      (开启后本会话所有图片自动反搜)
数量后缀 1-5；多词 AND；连写自动拆词（白发女仆猫娘=3词）；词表未命中自动语义匹配站内标签"""

NUM_TAIL_RE = re.compile(r"^(.*?)\s*(\d+)\s*$", re.S)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
RATING_TOKEN_RE = re.compile(r"^rating:([gsqe])$", re.IGNORECASE)
SITE_TOKEN_RE = re.compile(r"^site:(\w+)$", re.IGNORECASE)
# Moebooru 支持的元标签：不参与中文翻译和空格连写重试
META_PREFIXES = ("rating:", "order:", "id:", "status:", "user:", "date:", "parent:", "pool:", "site:")
PERIOD_MAP = {
    "今日": "day", "今天": "day", "day": "day",
    "本周": "week", "周": "week", "week": "week",
    "本月": "month", "月": "month", "month": "month",
}
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 常用中文 -> yande.re 英文标签（覆盖自动词表，优先级更高）。
# 词表已于 2026-09 逐个对 yande.re 实测(post.xml count>0)校准——
# yande.re 与 Danbooru 词表差异很大（兽耳是 nekomimi/inumimi/kemonomimi 系，
# 无发型标签 twintails/long_hair，furry/succubus/樱花/夜景等概念不存在）。
ZH_TAG_MAP = {
    "萝莉": "loli", "正太": "shota", "巨乳": "large_breasts", "贫乳": "small_breasts",
    "猫娘": "catgirl", "猫耳": "cat_ears", "猫尾": "cat_tail", "狗娘": "inumimi",
    "狐娘": "fox_girl", "兔娘": "rabbit_ears",
    "触手": "tentacles", "精灵": "elf", "天使": "angel",
    "恶魔": "demon", "女巫": "witch", "修女": "nun",
    "白丝": "pantyhose", "黑丝": "pantyhose", "束缚": "bondage",
    "过膝袜": "thighhighs", "膝盖袜": "kneesocks",
    "女仆": "maid", "护士": "nurse",
    "兔女郎": "bunny_girl", "巫女": "miko", "泳装": "swimsuit", "比基尼": "bikini",
    "旗袍": "china_dress", "和服": "kimono", "婚纱": "wedding_dress", "眼镜": "glasses",
    "马尾": "ponytail", "水手服": "serafuku",
    "校服": "school_uniform", "制服": "uniform", "军装": "military_uniform",
    "原神": "genshin_impact", "明日方舟": "arknights", "碧蓝航线": "azur_lane",
    "星穹铁道": "honkai:_star_rail", "蔚蓝档案": "blue_archive", "少女前线": "girls_frontline",
    "崩坏三": "honkai_impact", "fgo": "fate/grand_order",
    "初音未来": "hatsune_miku", "雷姆": "rem_(re_zero)", "帕秋莉": "patchouli_knowledge",
    "猫": "cat", "星空": "starry_sky", "雨": "rain", "风景": "landscape", "壁纸": "wallpaper",
    # Danbooru 习惯写法纠正
    "cat_girl": "catgirl", "foxgirl": "fox_girl", "doggirl": "inumimi",
}

RATING_HINT = {"s": "全年龄", "q": "擦边", "e": "R18"}
# 未显式指定分级时的默认范围：搜本会话上限内「所有」等级，而不是只搜上限那一级
# （否则 r18 解锁的群默认 /p 只出 e 图）。booru: all=不加分级标签、-e=-rating:e(s+q)；
# lolicon: all→r18=2 混合、-e→r18=0
RATING_DEFAULT_RANGE = {"s": "s", "q": "-e", "e": "all"}
# 多词搜索的口语连接词：yande.re 多标签本身即 AND 交集，连接词直接丢弃
TAG_CONNECTORS = {"and", "与", "和", "还有", "plus"}
# yande.re 站内确定不存在的概念（2026-09 词表校准实测：发色/发型标签基本无人打，
# 魅魔/dog 等 0 帖）。概念整体缺失时语义匹配只会召回音译碰瓷的角色名
# （白发→blanc、白丝→white_len），必须确定性剔除、不进嵌入层。
HAIR_COLOR_RE = re.compile(r"^[白黑金银红蓝紫粉绿棕栗茶灰][发毛]$")
IGNORE_CONCEPTS = {
    "长发", "短发", "中长发", "卷发", "直发", "头发", "发色", "发型",
    "呆毛", "刘海", "魅魔", "狗", "犬",
}
# 直觉化分级词：命令里直接写即等价 rating:*（大小写不敏感）。
# 带数字的词（r18）不能被结尾数字当数量的逻辑吃掉，_parse 里有对应保护
RATING_ALIAS = {
    "r18": "e", "nsfw": "e", "涩图": "e", "瑟图": "e", "色图": "e",
    "e": "e",
    "擦边": "q", "questionable": "q", "q": "q",
    "全年龄": "s", "sfw": "s", "健康": "s", "s": "s",
}


class YandereSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.cache_dir = data_dir / "cache"
        # 中文词表 = 自动词表打底 + 手工词典优先覆盖。
        # 自动词表由 tools/build_tags.py 生成: 多个开源中文词表对 yande.re
        # 真实标签库(canonical+别名)全量校准过滤, 保证每条都能搜到图
        try:
            with open(Path(__file__).parent / "tags_zh.json", encoding="utf-8") as fp:
                auto_map = json.load(fp)
        except Exception:
            auto_map = {}
        self.zh_map = {**auto_map, **ZH_TAG_MAP}
        self.en_map: dict[str, str] = {}
        logger.info(f"[yandere] 中文标签词表: 自动 {len(auto_map)} 条 + 手工 {len(ZH_TAG_MAP)} 条")
        # 连写自动拆词：词表即领域词典，做最大匹配（白发女仆猫娘 → 白发+女仆+猫娘）
        self._seg_max = max((len(k) for k in self.zh_map), default=2)

        self.gov = Governance(
            data_dir / "state.db",
            defaults={
                "rating_cap": str(config.get("group_rating_cap", "s")),
                "cooldown_s": int(config.get("cooldown_seconds", 8)),
                "quota": int(config.get("daily_quota", 80)),
                "forward": 1,
                "enabled": 1,
                "searchmode": 0,
            },
        )

        self.default_site = "yandere"
        extra = {
            s.strip().lower()
            for s in re.split(r"[,，\s]+", str(config.get("extra_sites", "") or ""))
            if s.strip()
        }
        # 回退源（国内直连）默认启用，可用 fallback_sites 配置关闭
        fallback = {
            s.strip().lower()
            for s in re.split(r"[,，\s]+", str(config.get("fallback_sites", "lolicon") or ""))
            if s.strip()
        }
        self._allowed_sites = {"yandere"} | ((extra | fallback) & set(SITES))
        self._clients: dict[str, MoebooruClient] = {}

        if config.get("reverse_enabled", True):
            limit = int(config.get("saucenao_daily_limit", 30))
            self.searcher = ReverseSearcher(
                proxy_url=config.get("proxy_url", ""),
                timeout=float(config.get("request_timeout", 15)),
                api_key=str(config.get("saucenao_api_key", "") or ""),
                iqdb_enabled=bool(config.get("iqdb_enabled", True)),
                daily_gate=lambda key: self.gov.api_count(key) < limit,
                daily_bump=lambda key, n: self.gov.bump_api(key, n),
            )
        else:
            self.searcher = None

        self.tagger = (
            LLMTagger(context, set(self.zh_map.values()))
            if config.get("llm_fallback", False)
            else None
        )
        # 语义标签匹配：词表未命中时的嵌入召回层（索引/端点缺失自动整层关闭）
        self.embedder = EmbedTagger.create(context, Path(__file__).parent, config, self.gov)
        self._rev_ts: dict[str, float] = {}
        self._sem = asyncio.Semaphore(3)

        self._scheduler = SubScheduler(self)
        self._sub_task = asyncio.create_task(self._scheduler.run())
        # 语义索引预热：消掉重载后首个查询 4-10s 的加载成本
        self._warm_task = asyncio.create_task(self._prewarm())

    async def _prewarm(self) -> None:
        """后台预热语义索引 + 一次嵌入探测（失败静默，首查再懒加载）。"""
        try:
            if self.embedder is None:
                return
            await self.embedder._ensure_ready()
            await self.embedder._api_embed(["预热"])
            logger.info("[yandere] 语义索引预热完成")
        except Exception:
            pass

    async def terminate(self) -> None:
        self._scheduler.stop()
        self._sub_task.cancel()
        self._warm_task.cancel()
        for c in self._clients.values():
            await c.close()
        if self.searcher:
            await self.searcher.close()
        if self.embedder:
            await self.embedder.close()
        self.gov.close()

    # ---------- 基础设施 ----------

    def client(self, site: str | None = None):
        site = (site or self.default_site).lower()
        if site == "lolicon":
            # 内部回退机制：不受 site 白名单影响（白名单管用户 site:xxx 切换）
            if "lolicon" not in self._clients:
                from .lolicon import LoliconClient
                self._clients["lolicon"] = LoliconClient(
                    api_key=str(self.config.get("lolicon_api_key", "") or ""),
                    timeout=float(self.config.get("request_timeout", 15)),
                    proxy_url=str(self.config.get("proxy_url", "") or ""),
                )
            return self._clients["lolicon"]
        if site not in self._allowed_sites:
            site = self.default_site
        if site not in self._clients:
            api = self.config.get("api_url", "https://yande.re") if site == "yandere" else None
            self._clients[site] = MoebooruClient(
                api_url=api or SITES[site]["api"],
                site_key=site,
                proxy_url=self.config.get("proxy_url", ""),
                timeout=float(self.config.get("request_timeout", 15)),
            )
        return self._clients[site]

    def _is_group(self, event: AstrMessageEvent) -> bool:
        return "GROUP" in str(event.get_message_type()).upper()

    def _chat_key(self, event: AstrMessageEvent) -> str:
        """治理存储用的会话键：群聊归一到群级。
        本部署 napcat 适配器的群聊 umo 是 {sender}_{group}——同群每个用户
        各有一个 origin，直接用会把分级上限/r18 解锁/冷却等本应群级生效的
        设置碎成按人。群聊一律归一到 {platform}:GroupMessage:{group_id}
        （也是 send_message 订阅推送能识别的标准格式），私聊保持原样。"""
        umo = event.unified_msg_origin
        if not self._is_group(event):
            return umo
        head, _, tail = umo.rpartition(":")
        gid = str(event.get_group_id() or "").strip() or (
            tail.split("_")[-1] if "_" in tail else tail)
        return f"{head}:{gid}"

    def _raw_args(self, event: AstrMessageEvent, bound: str = "") -> str:
        """从原始消息自取命令参数（去掉命令词本身，返回剩余文本）。

        AstrBot 4.28 的 GreedyStr 参数只有「无默认值」写法才会被识别为贪婪参数；
        带默认值（`query: GreedyStr = ""`）时被当成普通 str，只绑定第一个词，
        r18 / 数量 / AND 多词全在进插件前就被丢弃。因此命令参数一律以
        event.get_message_str() 为准，绑定值仅在取不到（本地直呼测试）时兜底。"""
        text = re.sub(r"\s+", " ", str(getattr(event, "message_str", "") or "").strip())
        if text.startswith("/"):
            text = text[1:]
        if " " in text:
            return text.split(" ", 1)[1].strip()
        return (bound or "").strip()

    def _rating_cap(self, event: AstrMessageEvent, chat: dict) -> str:
        """会话分级上限：群=row覆盖>group_rating_cap；私聊=row覆盖>default_rating。"""
        if "rating_cap" in chat.get("_row_keys", set()):
            return chat["rating_cap"]
        if self._is_group(event):
            return str(self.config.get("group_rating_cap", "s"))
        return str(self.config.get("default_rating", "s"))

    @staticmethod
    def _rating_disp(eff: str) -> str:
        """回显里的分级展示：范围值 all/-e 也转成可读形式。"""
        if eff == "all":
            return "rating:all"
        if eff == "-e":
            return "rating:s+q"
        return f"rating:{eff}" + (f"·{RATING_HINT[eff]}" if eff in ("q", "e") else "")

    def _r18_allowed(self, umo: str) -> bool:
        """e 级解锁 = 配置 r18_whitelist ∪ 管理员 /搜图设置 r18 on 的会话标记。"""
        raw = str(self.config.get("r18_whitelist", "") or "")
        if umo in {s.strip() for s in re.split(r"[,，\s]+", raw) if s.strip()}:
            return True
        return bool(self.gov.get_chat(umo).get("r18_ok"))

    def _is_plugin_admin(self, event: AstrMessageEvent) -> bool:
        """插件管理员 = 配置 admin_users 列表 ∪ AstrBot 管理员。
        不直接依赖 AstrBot 的 admins_id（那套全局配置默认是 bot QQ，
        且 permission_type 装饰器对非管理员静默吞命令毫无反馈）。"""
        raw = str(self.config.get("admin_users", "") or "")
        ids = {s.strip() for s in re.split(r"[,，\s]+", raw) if s.strip()}
        return str(event.get_sender_id()) in ids or event.is_admin()

    def _admin_denied(self, event: AstrMessageEvent) -> str | None:
        """管理命令的权限闸：通过返回 None，否则返回拒绝文案（handler 内自查，
        取代 AstrBot permission_type 装饰器的静默丢弃）。"""
        if self._is_plugin_admin(event):
            return None
        return (
            "🚫 本命令仅插件管理员可用。"
            f"请 bot 主人在 WebUI 本插件配置「插件管理员」里加上 {event.get_sender_id()}"
        )

    def _gate(self, event: AstrMessageEvent) -> tuple[bool, str]:
        ok, reason = self.gov.check(
            self._chat_key(event), event.get_sender_id(), self._is_plugin_admin(event)
        )
        if ok:
            self.gov.touch_command(self._chat_key(event))
        return ok, reason

    async def _materialize(
        self, post: dict, allow_preview: bool = True,
        deadline: float | None = None, force_quality: str | None = None,
    ):
        """取图 -> (可选)转 webp -> 落盘缓存，返回本地文件路径。命中缓存不发起网络请求。
        单图调用方（反搜补图等）默认允许超时降级 preview；批量发送走 _finalize_posts
        自己控制降级顺序（备胎优先于预览档），传 allow_preview=False。
        deadline=全局截止时钟(monotonic)，下载预算被钳制在其内。"""
        quality = force_quality or str(self.config.get("image_quality", "original"))
        convert = bool(self.config.get("convert_enabled", True))
        max_px = int(self.config.get("convert_max_px", 1080))
        cq = int(self.config.get("convert_quality", 95))
        # 最终要缩到 ≤1600px 时直接取 jpeg 样本：下载量小一个数量级，1080p 画质无可感差异
        if (
            not force_quality
            and self.config.get("auto_sample", True)
            and convert and max_px <= 1600 and quality == "original"
        ):
            quality = "jpeg"
        try:
            return await self._fetch_one(post, quality, convert, max_px, cq, deadline)
        except BooruError as exc:
            if quality == "preview" or not allow_preview:
                raise
            logger.warning(f"[yandere] #{post.get('id')} {exc}，降级 preview 重试")
            return await self._fetch_one(post, "preview", convert, max_px, cq, deadline)

    def _convert_args(self) -> tuple[bool, int, int]:
        return (
            bool(self.config.get("convert_enabled", True)),
            int(self.config.get("convert_max_px", 1080)),
            int(self.config.get("convert_quality", 95)),
        )

    async def _materialize_preview(self, post: dict, deadline: float | None = None):
        """直接取预览档（缩略图恒存在，几百 KB），批量流程的最后兜底。"""
        convert, max_px, cq = self._convert_args()
        return await self._fetch_one(post, "preview", convert, max_px, cq, deadline)

    async def _fetch_one(
        self, post: dict, quality: str, convert: bool, max_px: int, cq: int,
        deadline: float | None = None,
    ):
        from .imaging import convert_to_webp

        loli = post.get("_site") == "lolicon"
        if loli:
            # pixiv：regular 镜像(国内直连)为主，original(pximg 被墙)仅显式 original 档
            url = (post.get("urls") or {}).get(
                "original" if quality == "original" else "regular", "")
            if not url:
                raise BooruError("lolicon: 该帖无可用图链")
        else:
            url = MoebooruClient.pick_url(post, quality)
        ext = Path(url.split("?")[0]).suffix.lower()
        # 动图逐帧重编码在弱 CPU 上是分钟级开销，直接透传原文件
        animated = ext == ".gif"
        key = post.get("md5") or str(post["id"])
        if convert and not animated:
            dest = self.cache_dir / f"{key}_{quality}_w{max_px}.webp"
        else:
            dest = self.cache_dir / f"{key}_{quality}{ext}"
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        dl_budget = float(self.config.get("download_timeout", 30))
        # 按已知文件大小自适应预算：大图允许最多 90s（代理出口慢时 30s 会误杀）
        expected = int(post.get("file_size") or 0)
        if "/jpeg/" in url:
            expected = int(post.get("jpeg_file_size") or 0) or expected
        if quality != "preview" and expected > 0:
            dl_budget = max(dl_budget, min(90.0, expected / 40_000))
        if deadline is not None:
            # 全局截止：下载预算不得超过剩余时间（保 30s 内交付的最后闸门）
            dl_budget = min(dl_budget, max(3.0, deadline - time.monotonic()))
        t0 = time.perf_counter()
        async with self._sem:
            try:
                fetcher = self.client("lolicon" if loli else None).fetch(post, quality)
                _, data = await asyncio.wait_for(fetcher, timeout=dl_budget)
            except asyncio.TimeoutError as exc:
                # wait_for 超时是"总时长"预算：httpx 的 read 超时只约束相邻数据块间隔，
                # 代理出口慢时大图涓流下载几分钟都不会触发，必须外加硬顶
                raise BooruError(
                    f"{quality} 档下载超时（>{dl_budget:.0f}s）"
                ) from exc
        t_dl = time.perf_counter() - t0
        raw_len = len(data)
        t_cv = 0.0
        if convert and not animated:
            t0 = time.perf_counter()
            data = await asyncio.to_thread(convert_to_webp, data, max_px, cq)
            t_cv = time.perf_counter() - t0
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        logger.info(
            f"[yandere] #{post.get('id')} {quality} 下载 {t_dl:.1f}s {raw_len/1e6:.1f}MB"
            + (f" 转码 {t_cv:.1f}s -> {len(data)/1e6:.1f}MB" if t_cv else "")
        )
        return dest

    async def _emit_images(self, event: AstrMessageEvent, paths: list):
        """发送图片：达到阈值且会话允许时合并转发（仅 aiocqhttp），失败回退逐张。"""
        from astrbot.api.all import Image

        chat = self.gov.get_chat(self._chat_key(event))
        thr = int(self.config.get("forward_threshold", 3))
        if chat["forward"] and thr > 0 and len(paths) >= thr:
            try:
                from astrbot.api.all import Node, Nodes

                uin = str(getattr(event.message_obj, "self_id", "") or "10000")
                yield event.chain_result(
                    [
                        Nodes(
                            nodes=[
                                Node(uin=uin, name="yande.re 找图", content=[Image.fromFileSystem(str(p))])
                                for p in paths
                            ]
                        )
                    ]
                )
                return
            except Exception as exc:
                logger.warning(f"[yandere] 合并转发失败，回退逐张发送: {exc}")
        for p in paths:
            yield event.chain_result([Image.fromFileSystem(str(p))])

    async def _finalize_posts(
        self, posts: list, spare: list | None = None, deadline: float | None = None,
    ) -> tuple[list, list[str], list[str]]:
        """并发下载一组帖子；失败的先依次用备胎补位（保原档位画质），
        备胎也拿不到才对缺口用预览档兜底。返回 (最终帖子, 路径, 回显提示)。
        deadline 传入时下载预算被钳制在剩余时间内（30s 交付保证的闸门）。"""
        spare = list(spare or [])
        results = await asyncio.gather(
            *(self._materialize(p, allow_preview=False, deadline=deadline) for p in posts),
            return_exceptions=True,
        )
        pairs: list[tuple[dict, str]] = []
        failed: list[dict] = []
        for p, r in zip(posts, results):
            if isinstance(r, Exception):
                failed.append(p)
                logger.warning(f"[yandere] #{p.get('id')} 下载失败: {r}")
            else:
                pairs.append((p, r))
        notes: list[str] = []
        replaced = 0
        # 换备胎要重新下一张完整图，剩余预算不足时直接走预览兜底更快
        while failed and spare and (deadline is None or deadline - time.monotonic() > 12):
            s = spare.pop(0)
            try:
                pairs.append((s, await self._materialize(s, allow_preview=False, deadline=deadline)))
                failed.pop(0)
                replaced += 1
            except Exception as exc:
                logger.warning(f"[yandere] #{s.get('id')} 备胎下载失败: {exc}")
        pv = 0
        skipped = 0
        while failed:
            p = failed.pop(0)
            try:
                pairs.append((p, await self._materialize_preview(p, deadline=deadline)))
                pv += 1
            except Exception as exc:
                skipped += 1
                logger.warning(f"[yandere] #{p.get('id')} 预览档兜底失败: {exc}")
        if replaced:
            notes.append(f"{replaced} 张超时已换池内备胎")
        if pv:
            notes.append(f"{pv} 张为预览图·网络慢")
        if skipped:
            notes.append(f"{skipped} 张下载失败跳过")
        return [p for p, _ in pairs], [str(x) for _, x in pairs], notes

    @staticmethod
    def _fmt_ids(posts: list, paths: list[str]) -> str:
        # pixiv 源（Lolicon）没有评分概念，score=0 时不显示 ⭐
        return " / ".join(
            f"#{p['id']}"
            + (f"(⭐{p.get('score', 0)})" if p.get("score") else "")
            + (("(预览)" if "_preview" in Path(x).name else ""))
            for p, x in zip(posts, paths)
        )

    async def _deliver(self, event: AstrMessageEvent, paths: list[str], echo: str):
        """发送回显与图片并记账（echo 由调用方用最终帖子列表构建）。"""
        yield event.plain_result(echo)
        async for msg in self._emit_images(event, paths):
            yield msg
        self.gov.record_usage(self._chat_key(event), event.get_sender_id(), len(paths))

    def _parse(self, query: str) -> tuple[list[str], int, str | None, str | None]:
        """拆出 (非元标签, 数量, site, 显式rating)。数量/site/rating 均可省略。
        分级可写 rating:e，也可直接写直觉词（r18/涩图/擦边/全年龄/s/q/e）。"""
        max_n = int(self.config.get("max_images", 3))
        qs = query.strip()
        toks = qs.split()
        # "r18" 这类自带数字的分级词在结尾时，结尾数字是词的一部分、不是数量
        last = toks[-1] if toks else ""
        ends_with_digit_alias = (
            last.lower() in RATING_ALIAS and any(c.isdigit() for c in last)
        )
        count = 1
        m = None if ends_with_digit_alias else NUM_TAIL_RE.match(qs)
        if m and (m.group(1).strip() or qs.isdigit()):
            query = m.group(1).strip()
            count = max(1, min(int(m.group(2)), max_n))
        elif qs.isdigit():
            count = max(1, min(int(qs), max_n))
            query = ""
        else:
            query = qs
        site = rating = None
        tags = []
        for t in query.split():
            ms = SITE_TOKEN_RE.match(t)
            mr = RATING_TOKEN_RE.match(t)
            if ms:
                site = ms.group(1).lower()
            elif mr:
                rating = {"g": "s", "s": "s", "q": "q", "e": "e"}[mr.group(1).lower()]
            elif t.lower() in RATING_ALIAS:
                rating = RATING_ALIAS[t.lower()]
            elif t.lower() in TAG_CONNECTORS:
                continue
            else:
                tags.append(t)
        return tags, count, site, rating

    def _translate(self, raw_tags: list[str]) -> list[str]:
        return [self.zh_map.get(t.lower(), t) for t in raw_tags]

    def _segment(self, text: str) -> list[str]:
        """词表最大匹配分词：命中处切开，未命中的连续字符聚成残段；ASCII 段独立成片。"""
        pieces: list[str] = []
        buf = ""
        i, n = 0, len(text)

        def flush():
            nonlocal buf
            if buf:
                pieces.append(buf)
                buf = ""

        while i < n:
            if not CJK_RE.search(text[i]):
                flush()
                j = i
                while j < n and not CJK_RE.search(text[j]):
                    j += 1
                pieces.append(text[i:j])
                i = j
                continue
            matched = ""
            for ln in range(min(self._seg_max, n - i), 0, -1):
                seg = text[i:i + ln]
                if seg in self.zh_map:
                    matched = seg
                    break
            if matched:
                flush()
                pieces.append(matched)
                i += len(matched)
            else:
                buf += text[i]
                i += 1
        flush()
        return pieces

    def _unsearchable(self, token: str) -> bool:
        """yande.re 站内确定无此概念（发色/发型/魅魔等）：搜索恒空或只剩
        音译碰瓷的角色名（白发→blanc），确定性剔除、不进嵌入层。"""
        return token in IGNORE_CONCEPTS or bool(HAIR_COLOR_RE.fullmatch(token))

    def _expand_compound(self, raw_tags: list[str]) -> tuple[list[str], list[str]]:
        """连写 CJK 词按词表锚点拆分。整词已在词表的不拆（时崎狂三 这类角色名）。
        返回 (新 token 列表, 被拆过的原文列表)。"""
        out: list[str] = []
        split_src: list[str] = []
        for t in raw_tags:
            if not CJK_RE.search(t) or t.lower() in self.zh_map:
                out.append(t)
                continue
            pieces = self._segment(t)
            if len(pieces) >= 2:
                out.extend(pieces)
                split_src.append(t)
            else:
                out.append(t)
        return out, split_src

    # ---------- 搜图 / 随机 ----------

    @filter.command("p")
    async def cmd_p(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """搜图（默认随机；加「精选」走质量优先）。"""
        query = self._raw_args(event, query)
        order = "random"
        words = []
        for w in (query or "").split():
            if w.lower() in ("精选", "score", "order:score"):
                order = "score"
            else:
                words.append(w)
        async for r in self._run(event, " ".join(words), order=order):
            yield r

    async def _run(self, event: AstrMessageEvent, query: str, order: str):
        query = (query or "").strip()
        if not query:
            yield event.plain_result(HELP_TEXT)
            return
        ok, reason = self._gate(event)
        if not ok:
            yield event.plain_result(f"🚫 {reason}")
            return
        umo = self._chat_key(event)
        chat = self.gov.get_chat(umo)
        raw_tags, count, site, rating_req = self._parse(query)
        # 连写自动拆词：白发女仆猫娘 → 白发+女仆+猫娘（词表锚点切分，无锚点不拆）
        raw_tags, split_src = self._expand_compound(raw_tags)
        kept: list[str] = []
        preset_drop: list[str] = []
        for t in raw_tags:
            low = t.lower()
            if low in RATING_ALIAS:
                # 拆出来的分级词归位（猫娘r18 / 猫娘涩图 的尾段）
                rating_req = rating_req or RATING_ALIAS[low]
            elif low in TAG_CONNECTORS:
                continue
            elif self._unsearchable(t):
                # 站内确定无此概念（发色/发型等），确定性剔除
                preset_drop.append(t)
            else:
                kept.append(t)
        raw_tags = kept
        split_note = f"（拆词 {'、'.join(split_src)}）" if split_src else ""
        # 记录用户原始要求数量，超上限截断时在回显中说明。
        # "r18"尾部的 18 是分级词的一部分，不是要 18 张，同样要保护
        toks = query.strip().split()
        protected = bool(toks) and toks[-1].lower() in RATING_ALIAS and any(
            c.isdigit() for c in toks[-1])
        m_num = None if protected else NUM_TAIL_RE.match(query.strip())
        asked_n = int(m_num.group(2)) if m_num else None
        if site and site not in self._allowed_sites:
            yield event.plain_result(
                f"🚫 图源 {site} 未启用（可用: {', '.join(sorted(self._allowed_sites))}）"
            )
            return
        site = site or self.default_site
        cap = self._rating_cap(event, chat)
        if rating_req:
            requested = rating_req
            eff = min([requested, cap], key=lambda r: RATING_ORDER.get(r, 0))
        else:
            # 不写分级 = 搜上限内所有等级（r18 群默认混出 s/q/e，不再只搜 e）
            requested = eff = RATING_DEFAULT_RANGE.get(cap, "s")
        degraded = eff != requested
        effective_order = order

        # 翻译：词表优先 → 嵌入语义匹配（召回+重排，候选天然在有效集值域）→ LLM 兜底
        tags = self._translate(raw_tags)
        llm_note = embed_note = ""
        got: dict[str, str] = {}
        rejected: list[str] = []
        unknown = [t for t in raw_tags if t.lower() not in self.zh_map and CJK_RE.search(t)]
        if self.embedder and unknown:
            got, rejected = await self.embedder.resolve(unknown)
            if got:
                tags = [got.get(t.lower(), tt) for t, tt in zip(raw_tags, tags)]
                embed_note = "（语义 " + "、".join(f"{k}→{v}" for k, v in got.items()) + "）"
        if self.tagger:
            # LLM 只在「非元标签全部未知且嵌入也没救回来」时整句兜底
            unknown = [t for t in unknown if t.lower() not in got]
            if unknown and len(unknown) == len([t for t in raw_tags if not t.lower().startswith(META_PREFIXES)]):
                got2, dropped = await self.tagger.extract(umo, " ".join(unknown))
                if got2:
                    tags = got2
                    llm_note = f"（LLM 翻译{'; 丢弃 ' + str(dropped) + ' 个无效标签' if dropped else ''}）"
        if rejected or preset_drop:
            # 嵌入层定性拒绝 + 预设剔除（发色词等站内无此概念）的词不能留在 AND 搜索里
            # ——一个 CJK 裸词会让整条查询空结果。只在还有别的标签兜底时剔除；
            # 全部被剔则保留原词走空结果诊断。
            drop = {t.lower() for t in rejected} | {t.lower() for t in preset_drop}
            keep_nonmeta = [t for t in raw_tags if not t.lower().startswith(META_PREFIXES)]
            if keep_nonmeta:
                tags = [tt for t, tt in zip(raw_tags, tags) if t.lower() not in drop]
                embed_note += f"（忽略未识别: {' '.join(drop)}）"

        # ---------- 搜索+取图：主源先跑，超 headstart 未交付则起国内回退源竞速 ----------
        deadline_s = float(self.config.get("deadline_seconds", 24))
        deadline = time.monotonic() + deadline_s
        headstart = float(self.config.get("fallback_headstart", 8))
        use_fb = (
            site == self.default_site
            and "lolicon" in {
                s.strip().lower()
                for s in re.split(r"[,，\s]+", str(self.config.get("fallback_sites", "lolicon") or ""))
                if s.strip()
            }
        )
        # 回退源吃 pixiv 生态的中日文标签：用翻译前的用户原始词
        fb_tags = [t for t in raw_tags if not t.lower().startswith(META_PREFIXES)]

        prim = asyncio.create_task(self._search_and_fetch(
            site, tags, count, effective_order, eff, umo, deadline))
        res: dict | None = None
        fb: asyncio.Task | None = None
        try:
            res = await asyncio.wait_for(asyncio.shield(prim), timeout=max(0.2, headstart))
        except (asyncio.TimeoutError, TimeoutError):
            pass
        if not (res and res.get("ok")) and use_fb:
            fb = asyncio.create_task(self._fallback_fetch(fb_tags, count, eff, deadline))
        if not (res and res.get("ok")):
            # 谁 ok 谁赢、立即交付；同时完成时主源优先（标签精度/画质）
            while True:
                pending = [t for t in (prim, fb) if t is not None and not t.done()]
                if not pending or deadline - time.monotonic() <= 0:
                    break
                done, _ = await asyncio.wait(
                    pending, timeout=deadline - time.monotonic(),
                    return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break  # 截止到点
                for t in (prim, fb):
                    if (t in done and t.exception() is None and t.result().get("ok")):
                        res = t.result()
                        break
                if res and res.get("ok"):
                    break
            if not (res and res.get("ok")):
                for t in (prim, fb):
                    if t is not None and t.done() and not t.cancelled() and t.exception() is None:
                        r = t.result()
                        if r.get("ok"):
                            res = r
                            break
        for t in (prim, fb):
            if t is not None and not t.done():
                t.cancel()

        if not (res and res.get("ok")):
            prim_res = (prim.result()
                        if prim.done() and not prim.cancelled() and prim.exception() is None
                        else {})
            if prim_res.get("kind") == "error":
                yield event.plain_result(f"⚠️ {prim_res.get('msg', '搜索失败，请稍后再试')}")
            else:
                async for msg in self._diagnose_empty(event, tags, eff, site):
                    yield msg
            return

        site_tag = f"[{SITES[res['site']]['label']}] " if res["site"] != self.default_site else ""
        if degraded:
            degrade_note = (
                f"（本会话分级上限 {eff}，R18 需管理员 /搜图设置 r18 on）"
                if requested == "e" and eff != "e"
                else f"（已按会话上限降级 rating:{eff}）"
            )
        else:
            degrade_note = ""
        cap_note = f"（一次最多 {count} 张）" if asked_n and asked_n > count else ""
        rating_disp = self._rating_disp(eff)
        logger.info(
            f"[yandere] 取图就绪[{res['site']}]: 搜索 {res['search_s']:.1f}s "
            f"下载+转码 {res['mat_s']:.1f}s（{len(res['posts'])} 张）"
        )
        query_echo = (
            f"{site_tag}[{' AND '.join(res['tags'])} {rating_disp} order:{res.get('order', effective_order)}]"
            f"{split_note}{embed_note}{llm_note}{degrade_note}{cap_note} "
            + self._fmt_ids(res["posts"], res["paths"])
            + (f"（{'；'.join(res['notes'])}）" if res["notes"] else "")
        )

        if res.get("kind") == "random":
            if res.get("qkey"):
                self.gov.add_seen(umo, res["site"], res["qkey"], [p["id"] for p in res["posts"]])
            self.gov.set_history(
                umo, event.get_sender_id(), "random",
                {"site": res["site"], "tags": res["tags"], "rating": eff, "posts": res["posts"]},
            )
        else:
            self.gov.set_history(
                umo, event.get_sender_id(), res.get("kind", "score"),
                {"site": res["site"], "tags": res["tags"], "rating": eff, "offset": len(res["posts"]),
                 "pool": res.get("pool") or res["posts"], "posts": res["posts"]},
            )
        async for msg in self._deliver(event, res["paths"], query_echo):
            yield msg
        # 注意：stop_event 必须放在所有 yield 之后——
        # AstrBot 调度器在每次取到结果后会检查事件是否已停止，提前停止会丢弃后续消息
        event.stop_event()

    async def _search_and_fetch(
        self, site: str, tags: list[str], count: int, order: str,
        eff: str, umo: str, deadline: float,
    ) -> dict:
        """主源（Moebooru 系）搜索+取图，永不抛异常。ok 结构字段与 _fallback_fetch 一致。"""
        client = self.client(site)

        async def _once(tg: list[str]) -> tuple[list, list, list, str]:
            if order == "random":
                # 随机去重：排除本会话该标签组合最近发过的图；多取 2 张做备胎
                qk = f"{' '.join(tg)}|{eff}"
                full = await client.search(
                    tg, limit=count + 2, order="random", rating=eff,
                    exclude=self.gov.get_seen(umo, site, qk),
                )
                return full[:count], full[count:], full[:count], qk
            pl = await client.page_posts(tg, page=1, rating=eff)
            if len(pl) < count and len(pl) >= 100:
                pl = pl + await client.page_posts(tg, page=2, rating=eff)
            return pl[:count], pl[count:count + 2], pl, ""

        qkey = ""
        spare: list = []
        pool: list = []
        posts: list = []
        t_search = time.perf_counter()
        try:
            # 搜索硬帽：慢窗下 random 多轮探测会吃掉全部预算，宁可提前交给回退源
            cap = max(3.0, min(10.0, deadline - time.monotonic() - 8.0))
            posts, spare, pool, qkey = await asyncio.wait_for(_once(tags), timeout=cap)
            if not posts:
                # 空结果可能是把多词标签拆开了（"genshin impact" -> 两个标签），连写重试一次
                non_meta = [t for t in tags if not t.lower().startswith(META_PREFIXES)]
                if len(non_meta) > 1:
                    retry = ["_".join(non_meta)] + [t for t in tags if t.lower().startswith(META_PREFIXES)]
                    posts, spare, pool, qkey = await asyncio.wait_for(
                        _once(retry), timeout=cap)
                    if posts:
                        tags = retry
        except (asyncio.TimeoutError, TimeoutError):
            return {"ok": False, "kind": "error",
                    "msg": f"{SITES.get(site, {}).get('label', site)} 搜索超时（出口可能处于慢窗）"}
        except BooruError as exc:
            logger.warning(f"[yandere] search failed: {exc}")
            return {"ok": False, "kind": "error", "msg": str(exc)}
        t_search = time.perf_counter() - t_search
        if not posts:
            return {"ok": False, "kind": "empty"}

        t0 = time.perf_counter()
        final, paths, notes = await self._finalize_posts(posts, spare, deadline=deadline)
        t_mat = time.perf_counter() - t0
        if not final:
            return {"ok": False, "kind": "error", "msg": "图片下载全部失败"}
        return {
            "ok": True, "site": site, "tags": tags, "kind": order, "order": order,
            "qkey": qkey, "pool": pool or posts, "posts": final, "paths": paths,
            "notes": notes, "search_s": t_search, "mat_s": t_mat,
        }

    async def _fallback_fetch(self, raw_tags: list[str], count: int, eff: str, deadline: float) -> dict:
        """国内回退源（Lolicon/pixiv，直连无需代理）：多 tag 为 OR 宽松语义，
        空结果时退化到首标签再试。永不抛异常。"""
        t_search = time.perf_counter()
        try:
            cl = self.client("lolicon")
            posts: list = []
            tried: list[list[str]] = []
            for try_tags in (raw_tags, raw_tags[:1]):
                if not try_tags or try_tags in tried:
                    continue
                tried.append(try_tags)
                posts = await asyncio.wait_for(
                    cl.search(try_tags, limit=count, rating=eff), timeout=8.0)
                if posts:
                    break
            if not posts:
                return {"ok": False, "kind": "empty"}
            t_search = time.perf_counter() - t_search
            t0 = time.perf_counter()
            results = await asyncio.gather(
                *(self._materialize(p, allow_preview=False, deadline=deadline,
                                    force_quality="jpeg")
                  for p in posts),
                return_exceptions=True,
            )
            pairs = [(p, r) for p, r in zip(posts, results) if not isinstance(r, Exception)]
            for p, r in zip(posts, results):
                if isinstance(r, Exception):
                    logger.warning(f"[yandere] lolicon #{p.get('id')} 下载失败: {r}")
            if not pairs:
                return {"ok": False, "kind": "error", "msg": "回退源图片下载失败"}
            notes = ["已回退国内源 Lolicon(pixiv)"]
            if len(pairs) < len(posts):
                notes.append(f"{len(posts) - len(pairs)} 张下载失败跳过")
            return {
                "ok": True, "site": "lolicon", "tags": raw_tags, "kind": "random",
                "order": "random", "qkey": "", "pool": [p for p, _ in pairs],
                "posts": [p for p, _ in pairs], "paths": [str(x) for _, x in pairs],
                "notes": notes, "search_s": t_search, "mat_s": time.perf_counter() - t0,
            }
        except Exception as exc:
            logger.warning(f"[yandere] lolicon 回退失败: {exc}")
            return {"ok": False, "kind": "error", "msg": str(exc)}

    async def _diagnose_empty(self, event: AstrMessageEvent, tags: list[str], rating: str, site: str):
        """空结果时区分『被分级过滤』『标签不存在』，给出可操作提示。"""
        client = self.client(site)
        user_set_rating = any(t.lower().startswith("rating:") for t in tags)
        query_echo = " AND ".join(tags)
        if not user_set_rating and rating != "all":
            # all 范围已含所有等级，"add r18 to adjust" no longer makes sense, skip straight to tag suggestion
            try:
                total = await client.count(tags)
            except BooruError:
                total = -1
            if total > 0:
                yield event.plain_result(
                    f"『{query_echo}』在分级 {self._rating_disp(rating)} 下没有图，"
                    f"但该标签共有 {total} 张——内容基本都不在这个分级里。\n"
                    f"显式加 r18 / 擦边 / rating:e 可调整（如 /p {query_echo} r18）"
                )
                return
        tips = f"『{query_echo}』没有找到图片。yande.re 用英文蛇形标签（如 catgirl、large_breasts，兽耳系是 nekomimi/inumimi），内置近2万条 yande.re 校准中文词表自动翻译。"
        suggestions = []
        non_meta = [t for t in tags if not t.lower().startswith(META_PREFIXES)]
        if non_meta:
            suggestions = await client.suggest_tags(non_meta[0])
        if suggestions:
            tips += "\n相似标签: " + "、".join(f"{n}({c})" for n, c in suggestions[:5])
        yield event.plain_result(tips)

    # ---------- 热门 ----------

    @filter.command("热门", alias={"hot"})
    async def cmd_hot(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """查看热门榜（今日/本周/本月）。"""
        async for r in self._run_hot(event, self._raw_args(event, query)):
            yield r

    async def _run_hot(self, event: AstrMessageEvent, query: str):
        ok, reason = self._gate(event)
        if not ok:
            yield event.plain_result(f"🚫 {reason}")
            return
        umo = self._chat_key(event)
        chat = self.gov.get_chat(umo)
        parts = (query or "").split()
        period = "day"
        for t in parts:
            if t in PERIOD_MAP or t.lower() in PERIOD_MAP:
                period = PERIOD_MAP.get(t, PERIOD_MAP.get(t.lower()))
                break
        nums = [t for t in parts if t.isdigit()]
        max_n = int(self.config.get("max_images", 3))
        count = max(1, min(int(nums[0]), max_n)) if nums else min(3, max_n)
        site = self.default_site
        cap = self._rating_cap(event, chat)
        client = self.client(site)

        try:
            pool = await client.popular(period)
        except BooruError as exc:
            yield event.plain_result(f"⚠️ {exc}")
            return
        pool = [p for p in pool if RATING_ORDER.get(p.get("rating"), 0) <= RATING_ORDER.get(cap, 0)]
        # 今日榜被分级过滤后不够时，往前补一天/两天（周月榜本身量足够）
        if len(pool) < count and period == "day":
            seen = {p["id"] for p in pool}
            for back in (1, 2):
                try:
                    extra = await client.popular("day", days_back=back)
                except BooruError:
                    break
                pool += [p for p in extra if p["id"] not in seen
                         and RATING_ORDER.get(p.get("rating"), 0) <= RATING_ORDER.get(cap, 0)]
                if len(pool) >= count:
                    break
        if not pool:
            yield event.plain_result(
                f"热门榜（{'今日' if period == 'day' else period}）在分级 rating:{cap} 下没有可发的图"
            )
            return
        posts = pool[:count]
        spare = pool[count:count + 2]
        cn = {"day": "今日", "week": "本周", "month": "本月"}[period]
        final, paths, notes = await self._finalize_posts(posts, spare)
        if not final:
            yield event.plain_result("⚠️ 图片下载全部失败，请检查代理或稍后再试")
            return
        echo = (
            f"[热门·{cn} rating:{cap}] " + self._fmt_ids(final, paths)
            + (f"（{'；'.join(notes)}）" if notes else "")
        )
        self.gov.set_history(
            umo, event.get_sender_id(), "popular",
            {"site": site, "period": period, "offset": len(final), "pool": pool, "posts": final},
        )
        async for msg in self._deliver(event, paths, echo):
            yield msg
        event.stop_event()

    # ---------- 下一张 ----------

    @filter.command("下一张", alias={"next"})
    async def cmd_next(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """继续上一次搜索的结果。"""
        async for r in self._run_next(event, self._raw_args(event, query)):
            yield r

    async def _run_next(self, event: AstrMessageEvent, query: str):
        umo = self._chat_key(event)
        uid = event.get_sender_id()
        h = self.gov.get_history(umo, uid)
        if not h:
            yield event.plain_result("没有可续看的搜索（10 分钟内有效）。先 /p 或 /热门 吧")
            return
        ok, reason = self._gate(event)
        if not ok:
            yield event.plain_result(f"🚫 {reason}")
            return
        max_n = int(self.config.get("max_images", 3))
        nums = [t for t in (query or "").split() if t.isdigit()]
        count = max(1, min(int(nums[0]), max_n)) if nums else min(3, max_n)
        site = h.get("site", self.default_site)
        kind = h["kind"]

        if kind == "random":
            tags = h.get("tags", [])
            rating = h.get("rating", "s")
            qkey = f"{' '.join(tags)}|{rating}"
            seen = self.gov.get_seen(umo, site, qkey)
            try:
                full = await self.client(site).search(
                    tags, limit=count + 2, order="random", rating=rating, exclude=seen
                )
            except BooruError as exc:
                yield event.plain_result(f"⚠️ {exc}")
                return
            posts, spare = full[:count], full[count:]
            if not posts:
                yield event.plain_result("这个标签的随机池差不多翻完了，换个词试试～")
                return
            final, paths, notes = await self._finalize_posts(posts, spare)
            if not final:
                yield event.plain_result("⚠️ 图片下载全部失败，请检查代理或稍后再试")
                return
            self.gov.add_seen(umo, site, qkey, [p["id"] for p in final])
            self.gov.set_history(umo, uid, "random",
                                 {"site": site, "tags": tags, "rating": rating, "posts": final})
            echo = (
                f"[随机·续 {' AND '.join(tags)}] " + self._fmt_ids(final, paths)
                + (f"（{'；'.join(notes)}）" if notes else "")
            )
        else:
            pool = h.get("pool", [])
            offset = int(h.get("offset", 0))
            posts = pool[offset: offset + count]
            if not posts:
                yield event.plain_result("上一批结果已经看完了，换 /p 或 /热门 开新一轮吧")
                return
            final, paths, notes = await self._finalize_posts(
                posts, pool[offset + count: offset + count + 2]
            )
            if not final:
                yield event.plain_result("⚠️ 图片下载全部失败，请检查代理或稍后再试")
                return
            cn = {"score": "搜图", "popular": "热门"}[kind]
            echo = (
                f"[{cn}·续] " + self._fmt_ids(final, paths)
                + (f"（{'；'.join(notes)}）" if notes else "")
            )
            h["offset"] = offset + len(final)
            h["posts"] = final
            self.gov.set_history(umo, uid, kind, h)
        async for msg in self._deliver(event, paths, echo):
            yield msg
        event.stop_event()

    # ---------- 标签工具 ----------

    @filter.command("标签", alias={"tag"})
    async def cmd_tag(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """查标签：中文↔英文 + 站内数量 + 相似标签。"""
        word = self._raw_args(event, query).strip()
        if not word:
            yield event.plain_result("用法: /标签 巨乳 ｜ /标签 nekomimi")
            return
        ok, reason = self._gate(event)
        if not ok:
            yield event.plain_result(f"🚫 {reason}")
            return
        if not self.en_map:
            for zh, en in self.zh_map.items():
                self.en_map.setdefault(en, zh)
        client = self.client()
        lw = word.lower()
        en = self.zh_map.get(word) or self.zh_map.get(lw)
        lines = []
        try:
            if en:
                total = await client.count([en])
                lines.append(f"{word} → {en}｜约 {total:,} 张")
                sugg = [s for s in await client.suggest_tags(en.split("(")[0]) if s[0] != en]
                if sugg:
                    lines.append("相关: " + "、".join(f"{n}({c})" for n, c in sugg[:5]))
            elif lw in self.en_map:
                total = await client.count([lw])
                lines.append(f"{lw} → {self.en_map[lw]}｜约 {total:,} 张")
            else:
                hits: list[tuple[str, str]] = []
                if CJK_RE.search(word):
                    hits = [(zh, v) for zh, v in self.zh_map.items() if zh.startswith(word)][:8]
                else:
                    sugg = await client.suggest_tags(lw.replace(" ", "_"))
                    hits = [(n, f"{c} 张") for n, c in sugg]
                if hits:
                    lines.append(f"词典里没有『{word}』，相近的有:")
                    lines.extend(f"· {zh} → {v}" for zh, v in hits)
                else:
                    lines.append(f"『{word}』没收录也没相近标签。yande.re 用英文蛇形标签（如 catgirl）")
        except BooruError as exc:
            lines.append(f"⚠️ {exc}")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    # ---------- 以图搜图 ----------

    @filter.command("搜原图", alias={"sn", "sauce"})
    async def cmd_sauce(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """反搜图片来源：回复一张图片（或随命令附图）发 /搜原图。"""
        if not self.searcher:
            yield event.plain_result("🚫 以图搜图功能未启用（reverse_enabled）")
            return
        resolved = await self._resolve_image(event)
        if not resolved:
            logger.info(
                "[yandere] /搜原图 未取到图片，消息链: "
                + str([(c.__class__.__name__, str(getattr(c, "url", ""))[:40],
                        str(getattr(c, "file", ""))[:48]) for c in event.message_obj.message])
            )
            yield event.plain_result("用法: 回复一张图片发 /搜原图，或 /搜原图 + 附图")
            return
        ok, reason = self._gate(event)
        if not ok:
            yield event.plain_result(f"🚫 {reason}")
            return
        kind, val = resolved
        async for msg in self._reverse_and_reply(event, url=val if kind == "url" else None,
                                                 file_path=val if kind == "file" else None):
            yield msg

    async def _reverse_and_reply(self, event: AstrMessageEvent, url: str | None, file_path: str | None):
        try:
            res = await self.searcher.search(image_url=url, file_path=file_path)
        except Exception as exc:
            logger.warning(f"[yandere] 反搜失败: {exc}")
            yield event.plain_result(f"⚠️ 反搜失败: {exc}")
            return
        results = res["results"]
        if not results:
            note = ("；".join(res["notes"]) if res["notes"] else "没搜到出处")
            yield event.plain_result(f"🔍 {note}\n可换一张更清晰/裁切更少的图再试")
            event.stop_event()
            return
        yield event.plain_result(self._format_reverse(res))
        # 命中本插件已启用的图源时直接补发帖子图（按会话分级上限）
        umo = self._chat_key(event)
        cap = self._rating_cap(event, self.gov.get_chat(umo))
        for r in results:
            if r["site"] in self._allowed_sites and r.get("post_id"):
                try:
                    post = await self.client(r["site"]).get_post(r["post_id"])
                except BooruError:
                    post = None
                if not post:
                    break
                if RATING_ORDER.get(post.get("rating"), 0) <= RATING_ORDER.get(cap, 0):
                    try:
                        path = await self._materialize(post)
                        from astrbot.api.all import Image
                        yield event.chain_result([Image.fromFileSystem(str(path))])
                        self.gov.record_usage(umo, event.get_sender_id(), 1)
                    except Exception as exc:
                        logger.warning(f"[yandere] 反搜补图失败: {exc}")
                else:
                    yield event.plain_result(
                        f"命中 {SITES[r['site']]['label']} #{r['post_id']}，"
                        f"但内容分级超出本会话上限 rating:{cap}，已省略图片"
                    )
                break
        event.stop_event()

    def _format_reverse(self, res: dict) -> str:
        lines = [f"🔍 以图搜图（{'、'.join(res['engines']) or '无引擎可用'}）"]
        for i, r in enumerate(res["results"][:6], 1):
            who = r["site"] or (r["index"][:24] if r["index"] else "web")
            title = (r["title"] or "").strip()
            author = (r["author"] or "").strip()
            seg = f"{i}. [{who}] {r['similarity']:.0f}%"
            if title:
                seg += f" {title[:36]}"
            if author:
                seg += f" ／ {author[:20]}"
            lines.append(seg)
            if r["url"]:
                lines.append(f"   {r['url']}")
        if res.get("saucenao_remaining") is not None:
            lines.append(f"SauceNAO 今日剩余额度: {res['saucenao_remaining']}")
        for n in res["notes"]:
            lines.append(f"· {n}")
        return "\n".join(lines)

    @staticmethod
    def _extract_images(event: AstrMessageEvent) -> list[tuple[str, str]]:
        """从消息链（含被回复消息）提取图片，返回 [(kind, value)]，kind=url|file。"""
        out: list[tuple[str, str]] = []

        def _take(comp) -> None:
            if comp.__class__.__name__ != "Image":
                return
            url = str(getattr(comp, "url", "") or "")
            file = str(getattr(comp, "file", "") or "")
            if url.startswith("http"):
                out.append(("url", url))
            elif file.startswith("http"):
                out.append(("url", file))
            elif file.startswith("file://"):
                out.append(("file", file[7:]))

        for comp in list(getattr(event.message_obj, "message", []) or []):
            _take(comp)
            for sub in getattr(comp, "chain", []) or []:
                _take(sub)  # Reply 组件内嵌被回复消息链
        return out

    async def _resolve_image(self, event: AstrMessageEvent) -> tuple[str, str] | None:
        """取第一张可用图片。Reply 引用的历史图常无直链（NapCat get_msg 只给文件名），
        此时走 OneBot get_image 换取 URL 或本地缓存路径。"""
        imgs = self._extract_images(event)
        if imgs:
            return imgs[0]
        bot = getattr(event, "bot", None)
        comps = list(getattr(event.message_obj, "message", []) or [])
        for comp in comps:
            targets = [comp] + list(getattr(comp, "chain", []) or [])
            for sub in targets:
                if sub.__class__.__name__ != "Image":
                    continue
                file = str(getattr(sub, "file", "") or "")
                if not file:
                    continue
                if file.startswith("file://"):
                    file = file[7:]
                if "/" in file and Path(file).exists():
                    return ("file", file)
                if bot is None or not hasattr(bot, "call_action"):
                    continue
                try:
                    info = await bot.call_action("get_image", file_id=file)
                except Exception as exc:
                    logger.warning(f"[yandere] get_image({file[:48]}) 失败: {exc}")
                    continue
                url = str((info or {}).get("url") or "")
                if url.startswith("http"):
                    return ("url", url)
                local = str((info or {}).get("file") or "")
                if local and Path(local).exists():
                    return ("file", local)
        return None

    # ---------- 管理命令 ----------

    @filter.command("搜图设置", alias={"dbset"})
    async def cmd_set(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """管理本会话的搜图设置（仅插件管理员）。"""
        denied = self._admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        query = self._raw_args(event, query)
        umo = self._chat_key(event)
        chat = self.gov.get_chat(umo)
        parts = [p for p in (query or "").split() if p]
        sub = (parts[0] if parts else "查看").lower()
        val = parts[1] if len(parts) > 1 else ""

        if sub in ("查看", "show", ""):
            used = self.gov.usage_today(umo, event.get_sender_id())
            yield event.plain_result(
                "⚙️ 本会话搜图设置\n"
                f"· 分级上限: {chat['rating_cap']}（{RATING_HINT.get(chat['rating_cap'], '')}）"
                f"{' 🔓' if chat.get('r18_ok') else ''}\n"
                f"· 冷却: {chat['cooldown_s']} 秒（管理员不受限）\n"
                f"· 每人每日配额: {chat['quota']} 张（你今天已用 {used}）\n"
                f"· 合并转发: {'开' if chat['forward'] else '关'}\n"
                f"· 总开关: {'开' if chat['enabled'] else '关'}\n"
                f"· 搜图模式: {'开' if chat['searchmode'] else '关'}\n"
                "改法: /搜图设置 r18 on ｜ 分级 q ｜ 冷却 8 ｜ 配额 80 ｜ 转发 on ｜ 开关 on ｜ 模式 off"
            )
            event.stop_event()
            return

        def _onoff(v: str) -> int | None:
            return 1 if str(v).lower() in ("on", "1", "true", "开", "yes") else (
                0 if str(v).lower() in ("off", "0", "false", "关", "no") else None)

        if sub == "r18":
            v = _onoff(val)
            if v is None:
                yield event.plain_result("用法: /搜图设置 r18 on ｜ off（开启=本会话解锁 e 级）")
            elif v:
                self.gov.set_chat(umo, r18_ok=1, rating_cap="e")
                yield event.plain_result("✅ 本会话已解锁 R18，分级上限 → rating:e")
            else:
                default = (
                    str(self.config.get("group_rating_cap", "s")) if self._is_group(event)
                    else str(self.config.get("default_rating", "s"))
                )
                self.gov.set_chat(umo, r18_ok=0, rating_cap=default)
                yield event.plain_result(f"✅ 已关闭 R18，分级上限恢复 rating:{default}（{RATING_HINT.get(default, '')}）")
        elif sub in ("rating", "分级"):
            v = RATING_ALIAS.get(val.lower(), val.lower())
            if v not in ("s", "q", "e"):
                yield event.plain_result("分级上限取值: s / q / e（也接受 r18·擦边·全年龄）")
            elif v == "e" and not self._r18_allowed(umo):
                yield event.plain_result("🚫 本会话未解锁 R18，先 /搜图设置 r18 on")
            else:
                self.gov.set_chat(umo, rating_cap=v)
                yield event.plain_result(f"✅ 本会话分级上限 → rating:{v}（{RATING_HINT.get(v, '')}）")
        elif sub in ("冷却", "cooldown"):
            try:
                v = max(0, int(val))
            except ValueError:
                yield event.plain_result("冷却取值: 非负整数（秒）")
            else:
                self.gov.set_chat(umo, cooldown_s=v)
                yield event.plain_result(f"✅ 本会话冷却 → {v} 秒")
        elif sub in ("配额", "quota"):
            try:
                v = max(1, int(val))
            except ValueError:
                yield event.plain_result("配额取值: 正整数（张/人/日）")
            else:
                self.gov.set_chat(umo, quota=v)
                yield event.plain_result(f"✅ 每人每日配额 → {v} 张")
        elif sub in ("转发", "forward"):
            v = _onoff(val)
            if v is None:
                yield event.plain_result("转发取值: on / off")
            else:
                self.gov.set_chat(umo, forward=v)
                yield event.plain_result(f"✅ 合并转发 → {'开' if v else '关'}")
        elif sub in ("开关", "on", "off", "enable"):
            v = _onoff(sub if sub in ("on", "off") else val)
            if v is None:
                yield event.plain_result("开关取值: on / off")
            else:
                self.gov.set_chat(umo, enabled=v)
                yield event.plain_result(f"✅ 本会话搜图功能 → {'开' if v else '关'}")
        elif sub in ("模式", "searchmode"):
            v = _onoff(val)
            if v is None:
                yield event.plain_result("搜图模式取值: on / off")
            else:
                self.gov.set_chat(umo, searchmode=v)
                yield event.plain_result(
                    f"✅ 搜图模式 → {'开（本会话所有图片自动反搜）' if v else '关'}"
                )
        else:
            yield event.plain_result("未知设置项。/搜图设置 查看")
        event.stop_event()

    @filter.command("搜图模式", alias={"searchmode"})
    async def cmd_searchmode(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """搜图模式开关：开启后本会话所有图片自动反搜（仅插件管理员）。"""
        denied = self._admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        v = self._raw_args(event, query).strip().lower()
        if v not in ("on", "off", "开", "关", "1", "0"):
            yield event.plain_result("用法: /搜图模式 on ｜ off")
            return
        on = 1 if v in ("on", "开", "1") else 0
        self.gov.set_chat(self._chat_key(event), searchmode=on)
        yield event.plain_result(
            f"✅ 搜图模式 → {'开（本会话所有图片自动反搜，30 秒一张）' if on else '关'}"
        )
        event.stop_event()

    @filter.command("订阅", alias={"sub"})
    async def cmd_sub(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """每日定时推送（仅插件管理员）：/订阅 热门 08:00 或 /订阅 猫娘 08:00 3。"""
        denied = self._admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        umo = self._chat_key(event)
        query = self._raw_args(event, query)
        parts = [p for p in (query or "").split() if p]
        if not parts or parts[0] in ("查看", "list"):
            subs = self.gov.list_subs(umo)
            if not subs:
                yield event.plain_result(
                    "本会话还没有订阅。\n用法: /订阅 热门 08:00 ｜ /订阅 猫娘 08:00 3\n退订: /退订 编号"
                )
            else:
                lines = ["📮 本会话订阅:"]
                for s in subs:
                    payload = json.loads(s.get("payload") or "{}")
                    if s["kind"] == "popular":
                        desc = f"热门榜 ×{payload.get('n', 3)}"
                    else:
                        desc = f"[{' '.join(payload.get('tags', []))}] ×{payload.get('n', 3)}"
                    lines.append(f"#{s['id']} 每天 {s['hh_mm']} {desc}")
                lines.append("退订: /退订 编号")
                yield event.plain_result("\n".join(lines))
            event.stop_event()
            return

        time_idx = next((i for i, t in enumerate(parts) if TIME_RE.match(t)), None)
        if time_idx is None:
            yield event.plain_result("没找到时间参数。用法: /订阅 热门 08:00 ｜ /订阅 猫娘 08:00 3")
            return
        hh, mm = TIME_RE.match(parts[time_idx]).groups()
        hh_mm = f"{int(hh):02d}:{mm}"
        head = parts[:time_idx]
        tail = parts[time_idx + 1:]
        max_n = int(self.config.get("max_images", 3))
        n = max(1, min(int(tail[0]), max_n)) if tail and tail[0].isdigit() else min(3, max_n)
        site = self.default_site
        head = [t for t in head if not SITE_TOKEN_RE.match(t)] or head
        for t in parts:
            ms = SITE_TOKEN_RE.match(t)
            if ms and ms.group(1).lower() in self._allowed_sites:
                site = ms.group(1).lower()

        if head and head[0] in ("热门", "hot", "popular"):
            sub_id = self.gov.add_sub(umo, "popular", {"n": n, "site": site}, hh_mm)
            desc = "热门榜"
        elif head:
            sub_id = self.gov.add_sub(umo, "tags", {"tags": head, "n": n, "site": site}, hh_mm)
            desc = f"[{' '.join(head)}]"
        else:
            yield event.plain_result("缺少订阅内容。用法: /订阅 热门 08:00 ｜ /订阅 猫娘 08:00 3")
            return
        yield event.plain_result(f"✅ 已订阅 #{sub_id} {desc}，每天 {hh_mm} 推送 {n} 张")
        event.stop_event()

    @filter.command("退订", alias={"unsub"})
    async def cmd_unsub(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """取消订阅（仅插件管理员）：/退订 编号。"""
        denied = self._admin_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        raw = (query or "").strip()
        if not raw.isdigit():
            yield event.plain_result("用法: /退订 编号（先用 /订阅 查看列表）")
            return
        if self.gov.del_sub(self._chat_key(event), int(raw)):
            yield event.plain_result(f"✅ 已退订 #{raw}")
        else:
            yield event.plain_result(f"没有找到本会话的订阅 #{raw}")
        event.stop_event()

    # ---------- 收藏 ----------

    @filter.command("收藏", alias={"fav"})
    async def cmd_fav(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """收藏最近一次搜图结果里的图：/收藏 2（缺省为最后一张）。"""
        umo = self._chat_key(event)
        uid = event.get_sender_id()
        h = self.gov.get_history(umo, uid)
        posts = (h or {}).get("posts", [])
        if not posts:
            yield event.plain_result("最近 10 分钟内没有搜过图，先 /p 或 /热门")
            return
        raw = (query or "").strip()
        idx = int(raw) - 1 if raw.isdigit() else len(posts) - 1
        if not (0 <= idx < len(posts)):
            yield event.plain_result(f"序号取值 1-{len(posts)}")
            return
        p = posts[idx]
        site = h.get("site", self.default_site)
        added = self.gov.add_fav(uid, site, p["id"], (p.get("tags") or "")[:120])
        link = SITES[site]["web"].format(id=p["id"])
        if added:
            yield event.plain_result(f"❤️ 已收藏 {link}")
        else:
            yield event.plain_result(f"这张已经在收藏里了 {link}")
        event.stop_event()

    @filter.command("我的收藏", alias={"favlist", "收藏列表"})
    async def cmd_favlist(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """查看收藏列表：/我的收藏 [页码]。"""
        uid = event.get_sender_id()
        raw = (query or "").strip()
        page = max(1, int(raw)) if raw.isdigit() else 1
        per = 5
        total = self.gov.count_favs(uid)
        if not total:
            yield event.plain_result("还没有收藏。搜图后 /收藏 序号")
            return
        rows = self.gov.list_favs(uid, limit=per, offset=(page - 1) * per)
        if not rows:
            yield event.plain_result(f"没有第 {page} 页（共 {(total + per - 1) // per} 页）")
            return
        from datetime import datetime

        lines = [f"❤️ 我的收藏（{total} 张，第 {page}/{(total + per - 1) // per} 页）"]
        for i, r in enumerate(rows, (page - 1) * per + 1):
            label = SITES.get(r["site"], {}).get("label", r["site"])
            link = SITES.get(r["site"], SITES["yandere"])["web"].format(id=r["post_id"])
            tags = (r.get("tags") or "").strip()
            lines.append(f"{i}. [{label}] {link}" + (f"\n   {tags[:60]}" if tags else ""))
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    # ---------- 帮助 ----------

    @filter.command("搜图帮助", alias={"dbhelp", "yshelp"})
    async def cmd_help(self, event: AstrMessageEvent):
        """搜图插件命令一览。"""
        yield event.plain_result(HELP_TEXT)
        event.stop_event()

    # ---------- 搜图模式：被动反搜 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """搜图模式开启时，本会话收到的图片自动反搜（文本回复，30 秒一张）。"""
        try:
            if not self.searcher:
                return
            msg = (event.message_str or "").strip()
            if msg.startswith("/"):
                return
            umo = self._chat_key(event)
            if not self.gov.get_chat(umo)["searchmode"]:
                return
            resolved = await self._resolve_image(event)
            if not resolved:
                return
            now = time.time()
            if now - self._rev_ts.get(umo, 0) < 30:
                return
            self._rev_ts[umo] = now
            self.gov.bump_api("searchmode_hits", 1)
            kind, val = resolved
            try:
                res = await self.searcher.search(
                    image_url=val if kind == "url" else None,
                    file_path=val if kind == "file" else None,
                )
            except Exception as exc:
                logger.warning(f"[yandere] 搜图模式反搜失败: {exc}")
                return
            if not res["results"]:
                return
            chain = MessageChain().message(self._format_reverse(res))
            await event.send(chain)
        except Exception as exc:
            logger.warning(f"[yandere] 搜图模式处理异常: {exc}")
