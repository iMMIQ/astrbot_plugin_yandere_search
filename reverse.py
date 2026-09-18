"""以图搜图：SauceNAO(JSON API) 为主，iqdb.org 网页解析兜底。不依赖 astrbot。

- SauceNAO 免费注册 api key 后约 100 次/天；无 key 也能用但额度更低且有短时限流。
  调用配额由外部（governance.api_usage 表）按天计数控制。
- iqdb.org 无官方 API，解析结果页 HTML（booru 精确匹配率最高）。
- 两者结果按 (site, post_id) / url 去重合并，按相似度排序。
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

import httpx

SAUCENAO_URL = "https://saucenao.com/search.php"
IQDB_URL = "https://iqdb.org/"

# SauceNAO data 字段里各图库的 id 键 -> 展示名与帖子链接模板
SAUCENAO_BOARDS = [
    ("pixiv", "pixiv_id", "https://www.pixiv.net/artworks/{id}"),
    ("yandere", "yandere_id", "https://yande.re/post/show/{id}"),
    ("konachan", "konachan_id", "https://konachan.com/post/show/{id}"),
    ("danbooru", "danbooru_id", "https://danbooru.donmai.us/posts/{id}"),
    ("gelbooru", "gelbooru_id", "https://gelbooru.com/index.php?page=post&s=view&id={id}"),
    ("sankaku", "sankaku_id", "https://chan.sankakucomplex.com/post/show/{id}"),
]

# iqdb 结果页里各图库帖子链接（iqdb 的 href 是协议相对地址 //host/...）
IQDB_LINK_RES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:https?:)?//yande\.re/post/(?:show/)?(\d+)"), "yandere"),
    (re.compile(r"(?:https?:)?//konachan\.com/post/(?:show/)?(\d+)"), "konachan"),
    (re.compile(r"(?:https?:)?//konachan\.net/post/(?:show/)?(\d+)"), "konachan_net"),
    (re.compile(r"(?:https?:)?//danbooru\.donmai\.us/posts/(\d+)"), "danbooru"),
    (re.compile(r"(?:https?:)?//gelbooru\.com/index\.php\?page=post&s=view&id=(\d+)"), "gelbooru"),
]

SIM_RE = re.compile(r"(\d+(?:\.\d+)?)% similarity")
# iqdb 的结果表是无 class 的裸 <table>（"Your image" 等表没有 similarity 字样），靠 similarity 过滤
RESULT_TABLE_RE = re.compile(r"<table[^>]*>.*?</table>", re.S)
HREF_RE = re.compile(r'href="((?:https?:)?//[^"]+)"')


class ReverseError(Exception):
    pass


class ReverseSearcher:
    """image_url 或 file_path 二选一传入 search()。"""

    def __init__(
        self,
        proxy_url: str = "",
        timeout: float = 20.0,
        api_key: str = "",
        iqdb_enabled: bool = True,
        daily_gate: Callable[[str], bool] | None = None,
        daily_bump: Callable[[str, int], Any] | None = None,
    ):
        self.api_key = api_key
        self.iqdb_enabled = iqdb_enabled
        self._gate = daily_gate or (lambda key: True)
        self._bump = daily_bump or (lambda key, n: None)
        self.last_remaining: int | None = None
        # SauceNAO 被 Cloudflare 拦截（"Just a moment..."）时的临时退避截止时间
        self._sn_block_until = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, read=30.0),
            proxy=proxy_url or None,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def search(
        self, image_url: str | None = None, file_path: str | None = None
    ) -> dict[str, Any]:
        """返回 {"results": [...], "engines": [...], "notes": [...], "saucenao_remaining": int|None}"""
        results: list[dict[str, Any]] = []
        engines: list[str] = []
        notes: list[str] = []

        now = time.time()
        if now < self._sn_block_until:
            notes.append("SauceNAO 临时不可用（最近被 Cloudflare 拦截）")
        elif not self._gate("saucenao"):
            notes.append("SauceNAO 今日额度已用完")
        else:
            try:
                found = await self._saucenao(image_url, file_path)
                results.extend(found)
                engines.append("SauceNAO")
                self._bump("saucenao", 1)
            except ReverseError as exc:
                notes.append(f"SauceNAO: {exc}")
                if "Cloudflare" in str(exc):
                    self._sn_block_until = time.time() + 600

        best = max((r["similarity"] for r in results), default=0.0)
        if self.iqdb_enabled and (not results or best < 60):
            try:
                found = await self._iqdb(image_url, file_path)
                results.extend(found)
                engines.append("iqdb")
            except ReverseError as exc:
                notes.append(f"iqdb: {exc}")

        merged = self._dedupe(results)
        merged.sort(key=lambda r: r["similarity"], reverse=True)
        return {
            "results": merged[:6],
            "engines": engines,
            "notes": notes,
            "saucenao_remaining": self.last_remaining,
        }

    # ---------- SauceNAO ----------

    async def _saucenao(
        self, image_url: str | None, file_path: str | None
    ) -> list[dict[str, Any]]:
        params = {"output_type": "2", "db": "999", "numres": "8", "testmode": "0"}
        if self.api_key:
            params["api_key"] = self.api_key
        try:
            if image_url:
                resp = await self._client.get(SAUCENAO_URL, params=params | {"url": image_url})
            elif file_path:
                with open(file_path, "rb") as fp:
                    resp = await self._client.post(
                        SAUCENAO_URL, data=params, files={"file": fp}
                    )
            else:
                raise ReverseError("没有可搜的图片")
        except httpx.TimeoutException as exc:
            raise ReverseError("请求超时") from exc
        except httpx.HTTPError as exc:
            raise ReverseError(f"网络错误 {exc.__class__.__name__}") from exc
        if resp.status_code == 429:
            raise ReverseError("被限流")
        if resp.status_code >= 500:
            raise ReverseError(f"服务端 {resp.status_code}")
        try:
            payload = resp.json()
        except Exception as exc:
            raise ReverseError("响应不是 JSON（可能被 Cloudflare 拦截）") from exc

        header = payload.get("header", {})
        self.last_remaining = header.get("long_remaining")
        status = header.get("status")
        rows = payload.get("results") or []
        if not rows and status not in (0, 1):
            raise ReverseError(f"接口状态 {status}: {header.get('message', '')}".strip())

        out: list[dict[str, Any]] = []
        for row in rows:
            h, d = row.get("header", {}), row.get("data", {})
            try:
                sim = float(h.get("similarity") or 0)
            except (TypeError, ValueError):
                sim = 0.0
            item = {
                "engine": "saucenao",
                "similarity": sim,
                "index": (h.get("index_name") or "").strip(),
                "thumbnail": h.get("thumbnail") or "",
                "site": "",
                "post_id": None,
                "url": "",
                "title": "",
                "author": "",
            }
            for site, key, tpl in SAUCENAO_BOARDS:
                if d.get(key):
                    item["site"] = site
                    try:
                        item["post_id"] = int(d[key])
                    except (TypeError, ValueError):
                        pass
                    item["url"] = tpl.format(id=d[key])
                    break
            if not item["url"] and d.get("ext_urls"):
                item["url"] = d["ext_urls"][0]
            if not item["site"] and item["url"]:
                # 没给图库 id 键时从 ext_urls 反解（如只有 yande.re 帖子链接的结果）
                for rx, name in IQDB_LINK_RES:
                    mm = rx.search(item["url"])
                    if mm:
                        item["site"] = name
                        try:
                            item["post_id"] = int(mm.group(1))
                        except (TypeError, ValueError):
                            pass
                        break
            item["title"] = (d.get("title") or d.get("material") or "").strip()
            item["author"] = (
                d.get("member_name") or d.get("creator") or ""
            ).strip()
            if not item["url"] and not item["thumbnail"]:
                continue
            out.append(item)
        return out

    # ---------- iqdb ----------

    async def _iqdb(
        self, image_url: str | None, file_path: str | None
    ) -> list[dict[str, Any]]:
        try:
            if image_url:
                # iqdb 的搜索表单是 multipart；urlencoded 会被当成首页重新渲染
                resp = await self._client.post(IQDB_URL, files={"url": (None, image_url)})
            elif file_path:
                with open(file_path, "rb") as fp:
                    resp = await self._client.post(IQDB_URL, files={"file": fp})
            else:
                raise ReverseError("没有可搜的图片")
        except httpx.TimeoutException as exc:
            raise ReverseError("请求超时") from exc
        except httpx.HTTPError as exc:
            raise ReverseError(f"网络错误 {exc.__class__.__name__}") from exc
        if resp.status_code >= 500:
            raise ReverseError(f"服务端 {resp.status_code}")
        if resp.status_code >= 400:
            raise ReverseError(f"返回 {resp.status_code}")

        out: list[dict[str, Any]] = []
        for block in RESULT_TABLE_RE.findall(resp.text):
            sim = SIM_RE.search(block)
            if not sim:
                continue
            site, pid, href = "", None, ""
            # 结果表里第一个能对上已知图库帖子模式的链接就是命中帖
            for hm in HREF_RE.finditer(block):
                cand = hm.group(1)
                for rx, name in IQDB_LINK_RES:
                    m = rx.search(cand)
                    if m:
                        site, pid, href = name, int(m.group(1)), cand
                        break
                if site:
                    break
            if not site:
                continue
            if href.startswith("//"):
                href = "https:" + href
            out.append(
                {
                    "engine": "iqdb",
                    "similarity": float(sim.group(1)),
                    "index": "iqdb",
                    "thumbnail": "",
                    "site": site,
                    "post_id": pid,
                    "url": href,
                    "title": "",
                    "author": "",
                }
            )
        return out

    # ---------- 合并去重 ----------

    @staticmethod
    def _dedupe(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best: dict[tuple, dict[str, Any]] = {}
        for r in results:
            key = (
                (r["site"], r["post_id"]) if r.get("site") else ("url", r["url"] or r["thumbnail"])
            )
            if key not in best or r["similarity"] > best[key]["similarity"]:
                best[key] = r
        return list(best.values())
