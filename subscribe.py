"""订阅推送：插件内 asyncio 调度循环（30s 粒度）+ context.send_message 主动推送。

AstrBot 框架没有内置的插件定时装饰器，所以在 __init__ 里 create_task 启动本循环、
terminate 时取消。订阅分两类：
- popular: 每日推 yande.re 今日热门（按会话分级上限过滤）
- tags:    每日推关键词新图（默认序=最新在前，排除上次已推的帖子 id）
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from astrbot.api.event import MessageChain

from .governance import RATING_ORDER

log = logging.getLogger("yandere.subscribe")


class SubScheduler:
    def __init__(self, plugin):
        self.p = plugin
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        # 避开插件加载期，等平台就绪
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=20)
            return
        except asyncio.TimeoutError:
            pass
        while not self._stopped.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 单轮异常不中断调度
                log.warning(f"[yandere] 订阅调度异常: {exc}")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=30)
            except asyncio.TimeoutError:
                continue

    async def tick(self) -> None:
        now = datetime.now()
        today = now.strftime("%F")
        hhmm = now.strftime("%H:%M")
        for sub in self.p.gov.due_subs(hhmm, today):
            try:
                await self.push(sub)
            except Exception as exc:
                log.warning(f"[yandere] 推送订阅 #{sub['id']} 失败: {exc}")
            finally:
                self.p.gov.mark_sub_run(sub["id"], today)

    async def push(self, sub: dict) -> None:
        umo = sub["umo"]
        payload = json.loads(sub.get("payload") or "{}")
        n = max(1, min(int(payload.get("n", 3)), 5))
        site = payload.get("site", "yandere")
        client = self.p.client(site)
        cap = self.p.gov.get_chat(umo)["rating_cap"]

        if sub["kind"] == "popular":
            posts = await client.popular("day")
            posts = [p for p in posts if RATING_ORDER.get(p.get("rating"), 0) <= RATING_ORDER.get(cap, 0)]
            posts = posts[:n]
            header = "☀️ yande.re 今日热门"
        else:
            raw_tags = payload.get("tags", [])
            tags = [self.p.zh_map.get(t.lower(), t) for t in raw_tags]
            posts = await client.search(tags, limit=max(n * 3, 10), order="none", rating=cap)
            posts = [p for p in posts if p["id"] not in set(payload.get("last_ids", []))][:n]
            header = f"📮 订阅推送 [{' '.join(raw_tags)}]"
            payload["tags"] = raw_tags

        if not posts:
            log.info(f"[yandere] 订阅 #{sub['id']} 今日无内容可推，跳过")
            return

        results = await asyncio.gather(
            *(self.p._materialize(p) for p in posts), return_exceptions=True
        )
        paths = [r for r in results if not isinstance(r, Exception)]
        if not paths:
            log.warning(f"[yandere] 订阅 #{sub['id']} 图片全部下载失败")
            return

        echo = header + "\n" + " ".join(f"#{p['id']}(⭐{p.get('score', 0)})" for p in posts[: len(paths)])
        chain = MessageChain().message(echo)
        for p in paths:
            chain = chain.file_image(str(p))
        ok = await self.p.context.send_message(umo, chain)
        if not ok:
            log.warning(f"[yandere] 订阅 #{sub['id']} 推送失败（会话不存在或平台不支持主动消息）")
            return

        payload["last_ids"] = [p["id"] for p in posts] + payload.get("last_ids", [])
        payload["last_ids"] = payload["last_ids"][:200]
        payload.setdefault("tags", [])
        self.p.gov.update_sub_payload(sub["id"], payload)
