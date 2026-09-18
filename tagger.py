# SPDX-License-Identifier: LGPL-3.0-or-later
"""LLM 自然语言 -> yande.re 标签：LLM 只产候选，词表值域做裁判。

触发条件（main 侧控制）：查询里没有任何词表命中的中文词、且包含 CJK 字符。
返回的标签必须出现在本地有效集（tags_zh.json 的值域 + 手工词典）里，无效即丢，
保证 LLM 幻觉标签永远到不了图站 API。
"""

from __future__ import annotations

import re

from astrbot.api import logger

SPLIT_RE = re.compile(r"[,，\n;；]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

SYSTEM_PROMPT = (
    "你是二次元图站 yande.re 的标签检索助手。把用户的中文描述翻译成英文蛇形标签"
    "（snake_case，如 silver_hair、catgirl、rain、ponytail 不存在时不要编造）。"
    "规则：只输出小写英文标签、用英文逗号分隔、最多 6 个、不要任何解释；"
    "只保留描述中明确出现的视觉元素（发色/瞳色/服装/物种/场景/情绪），"
    "不要输出画风、质量词或不存在的概念。"
)


class LLMTagger:
    def __init__(self, context, valid_tags: set[str]):
        self.context = context
        self.valid_tags = valid_tags

    @staticmethod
    def has_cjk(text: str) -> bool:
        return bool(CJK_RE.search(text))

    async def extract(self, umo: str, text: str) -> tuple[list[str], int]:
        """返回 (有效标签列表, 被丢弃的候选数)。provider 不可用时返回 ([], 0)。"""
        try:
            prov = await self.context.get_using_provider_async(umo=umo)
        except Exception:
            prov = None
        if prov is None:
            return [], 0
        try:
            resp = await prov.text_chat(prompt=text, system_prompt=SYSTEM_PROMPT)
            raw = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.warning(f"[yandere] LLM 标签生成失败: {exc}")
            return [], 0
        if not raw:
            return [], 0
        tokens: list[str] = []
        for part in SPLIT_RE.split(raw):
            t = part.strip().lower().replace(" ", "_").strip("._-")
            if t and t not in tokens:
                tokens.append(t)
        valid = [t for t in tokens if t in self.valid_tags]
        return valid, len(tokens) - len(valid)
