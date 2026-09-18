# SPDX-License-Identifier: LGPL-3.0-or-later
#!/usr/bin/env python3
"""重建 tags_zh.json —— yande.re 校准中文标签词表。

流程：
1. 拉 yande.re 全站标签库 tag.json（count>=10 的 canonical 标签，含 id）
2. 拉全量别名表 tag_alias.json（注意：该接口每页固定 20 条、无视 limit 参数）
3. 组成"搜索有效集"：canonical ∪ 别名（计数继承目标标签）
4. 载入语料源（按人工维护优先级），en 侧全部对有效集过滤
5. 反转为 zh->en；同一中文多义时取站内计数最高者，同数时 canonical 优先于别名

语料源（优先级从高到低）：
- yande.csv                    yande.re 专属人工对照 (zhzwz/yande-re-chinese-patch 生态)
- moebooru_tags_cn.json        yande.re/konachan 人工词表 (asadahimeka/yandere-masonry)
- all_tags_cn.json             masonry 聚合词表
- danbooru_tags_cn.json        masonry danbooru 词表（键为空格分隔需转下划线）
- 旧 tags_zh.json              本插件上一版词表（反转参与）
- ffdkj tag.sqlite             328K Danbooru 中英对照 (Gemini 翻译+人工校对)

用法: python3 tools/build_tags.py   （在仓库根目录执行，需 httpx，产物覆盖 tags_zh.json）
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import sqlite3
import sys
from pathlib import Path

import httpx

BASE = "https://yande.re"
MASONRY = "https://raw.githubusercontent.com/asadahimeka/yandere-masonry/master/src/data"
FFDKJ = "https://raw.githubusercontent.com/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table/main/tag.sqlite"
UA = {"User-Agent": "astrbot-yandere-tags-builder/1.0"}
CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")  # 汉字+假名
MIN_COUNT = 10

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "tools" / ".build_cache"


def ok_zh(zh: str) -> str | None:
    zh = str(zh).strip()
    # 拒绝: 空/超长/含引号或竖线斜杠（ffdkj 会产出 "贫乳|娇小的乳房(B)" 这类带注释的复合键）
    if not zh or len(zh) > 30 or '"' in zh or "|" in zh or "/" in zh or not CJK.search(zh):
        return None
    return zh


async def fetch_tags(client: httpx.AsyncClient) -> dict[int, dict]:
    """canonical 标签: id -> {name, count}。order=count 分页，count<10 即停。"""
    sem = asyncio.Semaphore(3)

    async def page(p: int):
        async with sem:
            r = await client.get("/tag.json", params={"limit": 100, "order": "count", "page": p})
            if r.status_code == 429:
                await asyncio.sleep(5)
                r = await client.get("/tag.json", params={"limit": 100, "order": "count", "page": p})
            return r.json()

    tasks = [page(p) for p in range(1, 400)]
    tags: dict[int, dict] = {}
    for batch in await asyncio.gather(*tasks):
        if not batch:
            continue
        if all(t["count"] < MIN_COUNT for t in batch):
            continue
        for t in batch:
            if t["count"] >= MIN_COUNT:
                tags[t["id"]] = {"name": t["name"], "count": t["count"]}
    return tags


async def fetch_aliases(client: httpx.AsyncClient) -> list[dict]:
    """全量别名表。注意每页固定 20 条（limit 参数无效），需二分找末页。"""

    async def page(p: int) -> list:
        r = await client.get("/tag_alias.json", params={"limit": 100, "page": p})
        return r.json()

    lo, hi = 1, 1
    while await page(hi):
        lo = hi
        hi *= 2
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if await page(mid):
            lo = mid
        else:
            hi = mid

    sem = asyncio.Semaphore(3)

    async def safe(p: int) -> list:
        async with sem:
            for _ in range(3):
                try:
                    return await page(p)
                except Exception:
                    await asyncio.sleep(1)
        return []

    batches = await asyncio.gather(*(safe(p) for p in range(1, lo + 1)))
    return [a for b in batches if b for a in b if not a.get("pending")]


def build_live(tags: dict[int, dict], aliases: list[dict]):
    """搜索有效集: name -> (count, is_alias)。别名继承目标标签计数。"""
    live: dict[str, tuple[int, bool]] = {v["name"]: (v["count"], False) for v in tags.values()}
    for a in aliases:
        tgt = tags.get(a["alias_id"])
        if tgt and a["name"] not in live:
            live[a["name"]] = (tgt["count"], True)
    return live


def load_sources(work: Path) -> list[tuple[dict, int, str]]:
    pools: list[tuple[dict, int, str]] = []

    d = {}
    with open(work / "yande.csv", encoding="utf-8") as fp:
        for line in fp:
            if "," in line:
                en, zh = line.rstrip("\n").split(",", 1)
                d[en.strip()] = zh
    pools.append((d, 1, "yande.csv"))

    pools.append((json.load(open(work / "moebooru_tags_cn.json", encoding="utf-8")), 2, "moebooru_tags_cn"))
    pools.append((json.load(open(work / "all_tags_cn.json", encoding="utf-8")), 3, "all_tags_cn"))
    pools.append(({k.replace(" ", "_"): v for k, v in json.load(open(work / "danbooru_tags_cn.json", encoding="utf-8")).items()}, 4, "danbooru_tags_cn"))

    old_path = ROOT / "tags_zh.json"
    if old_path.exists():
        inv = {}
        for zh, en in json.load(open(old_path, encoding="utf-8")).items():
            inv.setdefault(en, zh)
        pools.append((inv, 5, "旧tags_zh"))

    db = sqlite3.connect(work / "ffdkj.sqlite")
    ff = {name: cn for name, cn in db.execute("SELECT name, cn_name FROM tags") if cn}
    pools.append((ff, 6, "ffdkj"))
    return pools


async def download_sources(client: httpx.AsyncClient, work: Path):
    work.mkdir(parents=True, exist_ok=True)
    files = ["yande.csv", "moebooru_tags_cn.json", "all_tags_cn.json", "danbooru_tags_cn.json"]
    for f in files:
        if not (work / f).exists():
            print(f"下载 {f} …")
            r = await client.get(f"{MASONRY}/{f}")
            (work / f).write_bytes(r.content)
    if not (work / "ffdkj.sqlite").exists():
        print("下载 ffdkj.sqlite (约24MB) …")
        r = await client.get(FFDKJ)
        (work / "ffdkj.sqlite").write_bytes(r.content)


async def main():
    work = WORK
    async with httpx.AsyncClient(base_url=BASE, timeout=30, follow_redirects=True, headers=UA) as client:
        await download_sources(client, work)
        print("拉取 yande.re 标签库 …")
        tags = await fetch_tags(client)
        print(f"canonical (count>={MIN_COUNT}): {len(tags)}")
        print("拉取别名表 …")
        aliases = await fetch_aliases(client)
        print(f"别名: {len(aliases)}")

    live = build_live(tags, aliases)
    print(f"搜索有效集: {len(live)}")

    en2zh: dict[str, str] = {}
    dropped = 0
    for pool, prio, name in sorted(load_sources(work), key=lambda x: x[1]):
        kept = seen = 0
        for en, zh in pool.items():
            en = en.strip()
            zh = ok_zh(zh)
            if not zh or zh == en:
                continue
            if en not in live:
                dropped += 1
                continue
            seen += 1
            if en not in en2zh:
                en2zh[en] = zh
                kept += 1
        print(f"[{name}] 有效 {seen}, 新增 {kept}")

    # 反转 zh->en：计数最高者胜，同数 canonical 优先（alias 记 0.5 罚分）
    zh2en: dict[str, str] = {}
    for en, zh in en2zh.items():
        count, is_alias = live[en]
        score = count - (0.5 if is_alias else 0)
        cur = zh2en.get(zh)
        if cur is None or score > live[cur][0] - (0.5 if live[cur][1] else 0):
            zh2en[zh] = en

    out = ROOT / "tags_zh.json"
    n_old = 0
    if out.exists():
        n_old = len(json.load(open(out, encoding="utf-8")))
    out.write_text(json.dumps(zh2en, ensure_ascii=False), encoding="utf-8")
    print(f"\n完成: en 有效 {len(en2zh)} (剔除无效 {dropped}), zh 条目 {len(zh2en)} (旧 {n_old})")


if __name__ == "__main__":
    asyncio.run(main())
