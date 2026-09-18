"""Moebooru 图站客户端（yande.re / konachan 系）：纯 httpx 实现，不依赖 astrbot。

Moebooru 特性：
- 无匿名标签数上限，order:score/random 在大标签上也不会超时
- 无内容付费墙（file_url 对匿名开放）
- rating 枚举只有 s/q/e（没有 g，g 统一按 s 处理）
- 无 counts 接口，总数用 post.xml 根节点的 count 属性
- order:random 每次只返回 1-2 张，随机模式需多次请求累积去重
- 热门榜是独立接口 post/popular_by_{day,week,month}.json：
  不接受 rating 参数、返回混合分级（s/q/e），必须由调用方过滤
  （无 by_day 后缀的 /post/popular.json 是 404；age:<1d 元标签无效）
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx

VALID_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
RATING_RE = re.compile(r"^rating:([gsqe])\b", re.IGNORECASE)
RATING_MAP = {"g": "s", "s": "s", "q": "q", "e": "e"}

# 站点注册表：全部是 Moebooru 系，API 完全同构，仅 base_url 不同
SITES: dict[str, dict[str, str]] = {
    "yandere": {
        "api": "https://yande.re",
        "label": "yande.re",
        "web": "https://yande.re/post/show/{id}",
    },
    "konachan": {
        "api": "https://konachan.com",
        "label": "konachan",
        "web": "https://konachan.com/post/show/{id}",
    },
    "konachan_net": {
        "api": "https://konachan.net",
        "label": "konachan.net",
        "web": "https://konachan.net/post/show/{id}",
    },
    # 国内直连回退源（pixiv 库），客户端在 lolicon.py，非 Moebooru 协议
    "lolicon": {
        "api": "https://api.lolicon.app/setu/v2",
        "label": "Lolicon·pixiv",
        "web": "https://www.pixiv.net/artworks/{id}",
    },
}

PERIODS = {"day": "day", "week": "week", "month": "month"}


class BooruError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# 旧名兼容
YandereError = BooruError


class MoebooruClient:
    def __init__(
        self,
        api_url: str = "https://yande.re",
        site_key: str = "yandere",
        proxy_url: str = "",
        timeout: float = 15.0,
    ):
        self.site_key = site_key
        self.label = SITES.get(site_key, {}).get("label", site_key)
        self.api_url = api_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.api_url,
            timeout=httpx.Timeout(timeout, read=30.0),
            proxy=proxy_url or None,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; astrbot-yandere-plugin/2.0)"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _build_query(
        self, tags: list[str], rating: str = "", order: str = ""
    ) -> list[str]:
        query: list[str] = []
        has_rating = False
        for t in (x.strip() for x in tags):
            if not t:
                continue
            m = RATING_RE.match(t)
            if m:
                has_rating = True
                query.append(f"rating:{RATING_MAP[m.group(1).lower()]}")
            else:
                query.append(t)
        if rating and not has_rating:
            query.append(f"rating:{RATING_MAP.get(rating.lower(), 's')}")
        if order in ("score", "random"):
            query.append(f"order:{order}")
        return query

    async def search(
        self,
        tags: list[str],
        limit: int = 3,
        order: str = "score",
        rating: str = "",
        exclude: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """按标签取帖。order: score | random | none（none=默认序，即最新在前）。
        用户写的 rating 标签会归一化（g→s），未显式指定时追加调用方传入的默认分级。
        exclude 里的帖子 id 会被跳过（用于“下一张”去重）。"""
        query = self._build_query(tags, rating=rating, order=order)
        exclude = exclude or set()

        if order == "random":
            # yande.re 的 order:random 每次只返回少量帖子，需多次请求累积去重；
            # 且对多数常用标签会稳定返回空（服务端 quirk，实测 loli/genshin_impact/
            # china_dress 挂、catgirl 通），拿不满时兜底「随机页抽样」——
            # 按标签总数随机挑一页再抽样，而不是永远从 top-100 高分池里抽（否则必然重复）
            collected: dict[int, dict[str, Any]] = {}
            for _ in range(4):
                resp = await self._get(
                    "/post.json",
                    params={"tags": " ".join(query), "limit": 100, "page": 1},
                )
                batch = resp.json()
                if not isinstance(batch, list):
                    break
                if not batch:
                    break  # 空结果说明 random quirk 生效（或本就无图），再刷也是空
                for p in batch:
                    if self._usable(p) and p["id"] not in collected and p["id"] not in exclude:
                        collected[p["id"]] = p
                if len(collected) >= limit:
                    break
                await asyncio.sleep(0.2)
            if len(collected) >= limit:
                return list(collected.values())[:limit]
            fallback_query = self._build_query(tags, rating=rating, order="score")
            total = await self._count_query(fallback_query)
            effective_exclude = exclude
            if 0 < total <= len(exclude):
                # 排除集合已覆盖整个池子（小标签看遍）：本轮忽略排除重新轮播
                effective_exclude = set()
            pool = await self._random_page(fallback_query, total, effective_exclude, collected)
            if not pool and effective_exclude:
                # 随机页全撞在已排除的帖子上：放弃排除保产出
                pool = await self._random_page(fallback_query, total, set(), collected)
            need = limit - len(collected)
            out = list(collected.values()) + random.sample(pool, min(need, len(pool)))
            return out[:limit]

        posts: list[dict[str, Any]] = []
        page = 1
        while len(posts) < limit and page <= 3:
            resp = await self._get(
                "/post.json",
                params={"tags": " ".join(query), "limit": min(max(limit * 3, 10), 100), "page": page},
            )
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            posts.extend(p for p in batch if self._usable(p) and p["id"] not in exclude)
            page += 1
        return posts[:limit]

    async def _count_query(self, query: list[str]) -> int:
        """按既有 query 查总数（post.xml 根节点 count 属性）；失败按 0 处理。"""
        try:
            resp = await self._get("/post.xml", params={"tags": " ".join(query), "limit": 1})
            return int(ElementTree.fromstring(resp.text).get("count", 0))
        except Exception:
            return 0

    async def _random_page(
        self,
        query: list[str],
        total: int,
        exclude: set[int],
        collected: dict[int, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """在 [1, total/100] 里随机挑一页取样。随机页可能越界或整页被排除，
        依次回退到前一页、第 1 页，仍为空则返回 []（由调用方决定是否放弃排除）。"""
        pages = max(1, min((total + 99) // 100, 1000))
        page = random.randint(1, pages)
        for cand in dict.fromkeys((page, max(1, page - 1), 1)):
            resp = await self._get(
                "/post.json", params={"tags": " ".join(query), "limit": 100, "page": cand}
            )
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                continue
            pool = [
                p for p in batch
                if self._usable(p) and p["id"] not in exclude and p["id"] not in collected
            ]
            if pool:
                return pool
        return []

    async def page_posts(
        self,
        tags: list[str],
        page: int = 1,
        rating: str = "",
        order: str = "score",
        exclude: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """取单独一页（100 张/页，已过 _usable 过滤），供“下一张”按游标切片。"""
        query = self._build_query(tags, rating=rating, order=order)
        resp = await self._get(
            "/post.json",
            params={"tags": " ".join(query), "limit": 100, "page": page},
        )
        batch = resp.json()
        if not isinstance(batch, list):
            return []
        exclude = exclude or set()
        return [p for p in batch if self._usable(p) and p["id"] not in exclude]

    async def popular(self, period: str = "day", days_back: int = 0) -> list[dict[str, Any]]:
        """热门榜。period: day|week|month；day 支持往前回看几天（分级过滤由调用方做）。"""
        key = PERIODS.get(period, "day")
        params: dict[str, Any] = {}
        if key == "day":
            d = date.today() - timedelta(days=days_back)
            params = {"day": d.day, "month": d.month, "year": d.year}
        resp = await self._get(f"/post/popular_by_{key}.json", params=params)
        batch = resp.json()
        if not isinstance(batch, list):
            return []
        return [p for p in batch if self._usable(p)]

    async def get_post(self, post_id: int) -> dict[str, Any] | None:
        """按 id 取单个帖子（反搜命中本插件图源时取图用）。"""
        resp = await self._get("/post.json", params={"tags": f"id:{post_id}"})
        batch = resp.json()
        if isinstance(batch, list) and batch:
            return batch[0]
        return None

    async def count(self, tags: list[str]) -> int:
        """Moebooru 没有 counts 接口，用 post.xml 根节点 count 属性。"""
        query = " ".join(t for t in tags if t)
        resp = await self._get("/post.xml", params={"tags": query, "limit": 1})
        try:
            root = ElementTree.fromstring(resp.text)
            return int(root.get("count", 0))
        except Exception as exc:
            raise BooruError(f"解析 {self.label} 计数响应失败") from exc

    async def suggest_tags(self, word: str, limit: int = 5) -> list[tuple[str, int]]:
        """按子串模糊找相似标签（best-effort，部分线路可能不通）。"""
        try:
            resp = await self._client.get(
                "/tag.json",
                params={"name": f"*{word}*", "order": "count", "limit": limit + 1},
            )
            if resp.status_code != 200 or not isinstance(resp.json(), list):
                return []
            return [
                (t["name"], t["count"])
                for t in resp.json()
                if t.get("count", 0) > 0
            ][:limit]
        except Exception:
            return []

    async def fetch(self, post: dict[str, Any], quality: str = "original") -> tuple[str, bytes]:
        """按档位取回图片字节，返回 (扩展名, data)。"""
        url = self.pick_url(post, quality)
        ext = Path(url.split("?")[0]).suffix.lower()
        if ext not in VALID_EXTS:
            raise BooruError(f"不支持的文件类型: {ext}")
        resp = await self._client.get(url)
        resp.raise_for_status()
        return ext, resp.content

    @staticmethod
    def pick_url(post: dict[str, Any], quality: str) -> str:
        if quality == "original":
            return post["file_url"]
        if quality == "preview":
            return post.get("preview_url") or post["jpeg_url"]
        return post.get("jpeg_url") or post["file_url"]

    @staticmethod
    def _usable(post: dict[str, Any]) -> bool:
        if not isinstance(post, dict):
            return False
        if post.get("status") == "deleted" or post.get("rating") not in ("s", "q", "e"):
            return False
        for u in (post.get("jpeg_url"), post.get("file_url")):
            if u and Path(u.split("?")[0]).suffix.lower() in VALID_EXTS:
                return True
        return False

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise BooruError(f"请求 {self.label} 超时，请检查代理是否可用") from exc
        except httpx.HTTPError as exc:
            raise BooruError(f"网络错误: {exc}") from exc
        if resp.status_code == 429:
            raise BooruError("请求太频繁，被限流了，稍后再试", 429)
        if resp.status_code >= 500:
            raise BooruError(f"{self.label} 返回 {resp.status_code}，稍后再试", resp.status_code)
        if resp.status_code >= 400:
            raise BooruError(
                f"{self.label} 返回 {resp.status_code}（标签可能不存在或参数有误）", resp.status_code
            )
        return resp
