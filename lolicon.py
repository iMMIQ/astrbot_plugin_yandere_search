# SPDX-License-Identifier: LGPL-3.0-or-later
"""Lolicon API（pixiv 库）——yande.re 的国内回退图源。

端点 https://api.lolicon.app/setu/v2（2026-09 实测已从 /api/v2/setu 迁移），
国内直连无需代理（实测容器内 ~1s）。标签走 pixiv 生态（中日双语标签都常见），
多 tag 是 **OR** 宽松语义（与 yande.re 的 AND 不同）——回退场景以快速有图优先。
图片取 urls.regular（master1200 ≈1200px，i.pixiv.re 镜像国内直连，实测 ~6s/700KB），
转码 1080p 无画质损失；original 是 i.pximg.net 被墙，仅显式 original 档走代理尝试。
"""

from __future__ import annotations

from typing import Any

import httpx

from astrbot.api import logger

from .booru import BooruError

API_URL = "https://api.lolicon.app/setu/v2"


class LoliconClient:
    site_key = "lolicon"
    label = "Lolicon"

    def __init__(self, api_key: str = "", timeout: float = 15, proxy_url: str = ""):
        self._key = str(api_key or "")
        self._timeout = timeout
        self._proxy = proxy_url  # 仅 original(pximg.net) 下载用
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        self._px: httpx.AsyncClient | None = None

    async def search(
        self,
        tags: list[str],
        limit: int = 1,
        order: str = "",
        rating: str = "s",
        exclude: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """按标签取帖。rating: s/q/-e→r18=0，e→r18=1，all→r18=2(混合)。返回插件统一 post 结构。"""
        params: dict[str, Any] = {
            "num": max(1, min(int(limit), 20)),
            "size": "regular",
            "excludeAI": "false",
        }
        params["r18"] = {"e": 1, "all": 2}.get(str(rating).lower(), 0)
        if tags:
            params["tag"] = list(tags)
        if self._key:
            params["apikey"] = self._key
        try:
            r = await self._client.get(API_URL, params=params)
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:
            raise BooruError(f"lolicon 请求失败: {exc}") from exc
        if payload.get("error"):
            raise BooruError(f"lolicon: {payload['error']}")
        out: list[dict[str, Any]] = []
        for d in payload.get("data", []):
            pid = d.get("pid")
            if pid is None or (exclude and pid in exclude):
                continue
            urls = d.get("urls") or {}
            out.append({
                "id": pid,
                "score": 0,
                "rating": "e" if d.get("r18") else "s",
                "tags": " ".join(d.get("tags") or []),
                "urls": {
                    "regular": urls.get("regular", ""),
                    "original": urls.get("original", ""),
                },
                "_site": "lolicon",
            })
        return out

    async def fetch(self, post: dict, quality: str) -> tuple[str, bytes]:
        """quality original→pximg(代理)；其余(jpeg/large/preview)→regular 镜像直连。"""
        if quality == "original":
            url = post["urls"].get("original", "")
        else:
            url = post["urls"].get("regular", "")
        if not url:
            raise BooruError("lolicon: 该帖无可用图链")
        client = self._client
        if "pximg.net" in url:
            if self._px is None:
                self._px = httpx.AsyncClient(
                    timeout=self._timeout, trust_env=False, proxy=self._proxy or None)
            client = self._px
        try:
            r = await client.get(url, headers={"Referer": "https://www.pixiv.net/"})
            r.raise_for_status()
        except Exception as exc:
            raise BooruError(f"lolicon 图片下载失败: {exc}") from exc
        name = url.rsplit("/", 1)[-1].split("?")[0] or "pixiv.jpg"
        return name, r.content

    async def close(self) -> None:
        for c in (self._client, self._px):
            if c is not None:
                try:
                    await c.aclose()
                except Exception:
                    pass
        logger.info("[yandere] lolicon 客户端已关闭")
