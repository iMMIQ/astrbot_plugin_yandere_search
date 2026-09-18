"""语义标签匹配（RAG）：词表未命中的中文词 → bge-m3 向量召回 → bge-reranker-v2-m3 重排。

沿用 tagger.py 的哲学：模型只产候选，本地有效集做裁判——索引语料就是 yande.re
搜索有效集（tools/build_embed_index.py 构建：canonical count>=10 ∪ 别名，全部站内可搜），
重排选出的标签天然合法，无需再回图站验证。

端点自动发现：读 AstrBot 已配置的 openai_embedding / vllm_rerank provider（用户为
知识库配好的 bge-m3 / bge-reranker-v2-m3 直接复用），插件配置可覆盖。
索引缺失/模型不一致/服务不可达时整层静默降级为纯词表行为，不影响主流程。
结果（含未命中负样本）写入治理库 tagmap 表，同一个词只算一次。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import httpx
import numpy as np

from astrbot.api import logger

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
NEG_TTL = 14 * 86400      # 负样本缓存期：词表/语料会迭代，未命中别记一辈子
COS_FLOOR = 0.40          # 余弦预筛下限：垃圾词与全索引的相似度普遍 <0.35，在此拦截
COS_WIN_GATE = 0.45       # 双门槛：重排高分者的余弦也必须过线（压制音译撞名陷阱）
NEAR_BAND = 0.08          # canonical 近分带：带内候选按站内帖数决胜
MIN_WIN_POSTS = 50        # 当选标签最低帖数：拦住 17 帖的 random 之类垃圾命中
COS_ONLY_GATE = 0.50      # 无重排服务时的纯余弦采纳门槛
ERR_LOG_INTERVAL = 300    # 服务故障日志限频（秒）


class EmbedTagger:
    def __init__(
        self,
        emb_url: str, emb_key: str, emb_model: str,
        rr_url: str, rr_key: str, rr_model: str,
        plugin_dir: Path, gov,
        topk: int = 30, min_score: float = 0.45, proxy: str = "",
    ):
        self.emb_url = emb_url
        self.emb_key = emb_key
        self.emb_model = emb_model
        self.rr_url = rr_url
        self.rr_key = rr_key
        self.rr_model = rr_model
        self.plugin_dir = Path(plugin_dir)
        self.gov = gov
        self.topk = max(1, topk)
        self.min_score = min_score
        self.proxy = proxy
        self._emb: np.ndarray | None = None
        self._tags: list[str] = []
        self._texts: list[str] = []
        self._counts: list[int] = []
        self._aliases: list[bool] = []
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._broken = False
        self._last_err = 0.0

    # ---------- 装配 ----------

    @classmethod
    def create(cls, context, plugin_dir: Path, config, gov) -> "EmbedTagger | None":
        """按 配置开关 → 索引存在 → 端点发现( AstrBot provider → 插件配置覆盖 ) 装配。
        任何一环不满足都返回 None，调用方当作功能不存在。"""
        if not config.get("embed_enabled", True):
            return None
        plugin_dir = Path(plugin_dir)
        index_p = plugin_dir / "tag_index.npz"
        meta_p = plugin_dir / "tag_index_meta.json"
        if not index_p.exists() or not meta_p.exists():
            logger.info("[yandere] 语义标签索引缺失，语义匹配关闭（tools/build_embed_index.py 可重建）")
            return None
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("[yandere] tag_index_meta.json 损坏，语义匹配关闭")
            return None

        emb_url = emb_key = emb_model = ""
        rr_url = rr_key = rr_model = ""
        try:
            # 4.28 的插件 Context 用 get_config()（无 umo=默认配置）；
            # astrbot_config 属性是旧版 API，留作兜底
            root_cfg = None
            getter = getattr(context, "get_config", None)
            if callable(getter):
                try:
                    root_cfg = getter()
                except Exception:
                    root_cfg = None
            if not root_cfg:
                root_cfg = getattr(context, "astrbot_config", None)
            providers = (root_cfg or {}).get("provider", []) or []
            for p in providers:
                if not isinstance(p, dict) or not p.get("enable", True):
                    continue
                if p.get("type") == "openai_embedding" and not emb_url:
                    emb_url = str(p.get("embedding_api_base", "") or "").rstrip("/")
                    emb_key = str(p.get("embedding_api_key", "") or "")
                    emb_model = str(p.get("embedding_model", "") or "")
                elif p.get("type") == "vllm_rerank" and not rr_url:
                    rr_url = (str(p.get("rerank_api_base", "") or "").rstrip("/")
                              + str(p.get("rerank_api_suffix", "/v1/rerank") or "/v1/rerank"))
                    rr_key = str(p.get("rerank_api_key", "") or "")
                    rr_model = str(p.get("rerank_model", "") or "")
        except Exception:
            pass
        # 插件配置覆盖（适合 AstrBot 里没配 provider 的部署）
        if str(config.get("embed_api_url", "") or "").strip():
            emb_url = str(config["embed_api_url"]).strip().rstrip("/")
            emb_key = str(config.get("embed_api_key", "") or "")
            emb_model = str(config.get("embed_model", "") or "") or emb_model
        if str(config.get("rerank_api_url", "") or "").strip():
            rr_url = str(config["rerank_api_url"]).strip()
        if not emb_url:
            logger.info("[yandere] 未找到嵌入服务（AstrBot openai_embedding provider 或插件 embed_api_url），语义匹配关闭")
            return None
        if emb_model and meta.get("model") and emb_model != meta["model"]:
            logger.warning(
                f"[yandere] 索引模型 {meta['model']} 与运行模型 {emb_model} 不一致，"
                "语义匹配关闭（换模型需用新模型重建索引）")
            return None

        use_rr = bool(config.get("rerank_enabled", True)) and bool(rr_url)
        inst = cls(
            emb_url, emb_key, emb_model or str(meta.get("model", "")),
            rr_url if use_rr else "", rr_key, rr_model,
            plugin_dir, gov,
            topk=int(config.get("embed_topk", 200)),
            min_score=float(config.get("embed_min_score", 0.70)),
            proxy=str(config.get("embed_proxy", "") or ""),
        )
        logger.info(
            f"[yandere] 语义标签匹配就绪: 索引 {meta.get('n', '?')}×{meta.get('dim', '?')} "
            f"model={inst.emb_model} rerank={'on' if use_rr else 'off'}")
        return inst

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # ---------- 主入口 ----------

    async def resolve(self, tokens: list[str]) -> tuple[dict[str, str], list[str]]:
        """tokens: 词表未命中的原始词。

        返回 (映射, 明确拒绝列表)：映射={小写词: yande.re 标签}只含过阈值者；
        拒绝列表=已定性「站内无此概念」的词（余弦/重排不达标或帖数过低，
        已缓存负样本），调用方可以放心把它们从 AND 搜索里剔除。
        服务/索引故障时拒绝列表为空（非定性失败，调用方回退旧行为）。"""
        if self._broken or not await self._ensure_ready():
            return {}, []
        now = time.time()
        uniq: dict[str, None] = {}
        for t in tokens:
            key = str(t).strip().lower()
            if key and CJK_RE.search(key):
                uniq[key] = None
        out: dict[str, str] = {}
        rej: list[str] = []
        pend: list[str] = []
        for key in uniq:
            cached = self.gov.get_tagmap(key)
            if cached:
                tag, _score, ts = cached
                if tag:
                    out[key] = tag
                elif now - ts >= NEG_TTL:  # 过期负样本重算
                    pend.append(key)
                else:
                    rej.append(key)
                continue
            pend.append(key)
        if not pend:
            return out, rej

        try:
            qvecs = await self._api_embed(pend)
        except Exception as exc:
            self._log_err(f"嵌入服务失败: {exc}")
            return out, rej
        for i, key in enumerate(pend):
            sims = self._cosine(qvecs[i])
            cand = [int(j) for j in np.argsort(-sims)[: self.topk] if sims[j] >= COS_FLOOR]
            if not cand:
                self.gov.set_tagmap(key, "", 0.0)
                rej.append(key)
                continue
            if self.rr_url:
                try:
                    results = await self._api_rerank(key, [self._texts[j] for j in cand])
                except Exception as exc:
                    # 重排抖动不算负样本，下次重试
                    self._log_err(f"重排服务失败: {exc}")
                    continue
                # 只允许 canonical 当选：别名标签的帖数继承自目标(如 maid_panties 继承
                # panties 的 18 万)，按帖数决胜时会被别名污染；而 canonical 搜索永远安全。
                # 别名仍参与召回/重排，防止正确目标不在候选里。
                passing = [
                    r for r in results
                    if float(r.get("relevance_score", 0.0)) >= self.min_score
                    and float(sims[cand[int(r["index"])]]) >= COS_WIN_GATE
                    and not self._aliases[cand[int(r["index"])]]
                ]
                if not passing:
                    self.gov.set_tagmap(key, "", 0.0)
                    rej.append(key)
                    continue
                # bge-reranker 对相邻候选打分饱和(挤在 0.95-1.0)，分数分不出对错：
                # canonical 最高分的 NEAR_BAND 带内，按站内帖数决胜——音译撞名的角色
                # 标签帖数通常几百，正解的通用标签动辄几万
                top = max(float(r["relevance_score"]) for r in passing)
                near = [r for r in passing if top - float(r["relevance_score"]) <= NEAR_BAND]
                best = max(
                    near,
                    key=lambda r: (self._counts[cand[int(r["index"])]],
                                   float(r["relevance_score"])),
                )
                j = cand[int(best["index"])]
                if self._counts[j] < MIN_WIN_POSTS:
                    self.gov.set_tagmap(key, "", 0.0)
                    rej.append(key)
                    continue
                out[key] = self._tags[j]
                self.gov.set_tagmap(key, self._tags[j], float(best["relevance_score"]))
            else:
                j = cand[0]
                if float(sims[j]) >= max(COS_ONLY_GATE, self.min_score):
                    out[key] = self._tags[j]
                    self.gov.set_tagmap(key, self._tags[j], float(sims[j]))
                else:
                    self.gov.set_tagmap(key, "", 0.0)
                    rej.append(key)
        return out, rej

    # ---------- 内部 ----------

    def _log_err(self, msg: str) -> None:
        now = time.time()
        if now - self._last_err >= ERR_LOG_INTERVAL:
            self._last_err = now
            logger.warning(f"[yandere] {msg}")

    async def _ensure_ready(self) -> bool:
        if self._emb is not None:
            return True
        async with self._lock:
            if self._emb is not None:
                return True

            def load():
                z = np.load(self.plugin_dir / "tag_index.npz")
                emb = z["emb"]
                m = json.loads((self.plugin_dir / "tag_index_meta.json").read_text(encoding="utf-8"))
                tags, texts = m["tags"], m["texts"]
                counts = [int(c) for c in m.get("counts", [0] * len(tags))]
                aliases = [bool(a) for a in m.get("aliases", [False] * len(tags))]
                if not (len(tags) == len(texts) == emb.shape[0] == len(counts) == len(aliases)):
                    raise ValueError(
                        f"索引尺寸不一致 {emb.shape[0]}/{len(tags)}/{len(texts)}/{len(counts)}/{len(aliases)}")
                return emb, tags, texts, counts, aliases

            try:
                self._emb, self._tags, self._texts, self._counts, self._aliases = (
                    await asyncio.to_thread(load))
                logger.info(f"[yandere] 语义索引已加载: {len(self._tags)} 条")
                return True
            except Exception as exc:
                self._broken = True
                logger.warning(f"[yandere] 语义索引加载失败，语义匹配停用: {exc}")
                return False

    def _cosine(self, q: np.ndarray) -> np.ndarray:
        """索引向量已在构建期 L2 归一化；分块转 float32 避免整表驻留 f32。"""
        q = q.astype(np.float32)
        q /= np.linalg.norm(q) + 1e-9
        n = self._emb.shape[0]
        out = np.empty(n, dtype=np.float32)
        step = 8192
        for i in range(0, n, step):
            out[i:i + step] = self._emb[i:i + step].astype(np.float32) @ q
        return out

    def _client_(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30, trust_env=False, proxy=self.proxy or None)
        return self._client

    async def _api_embed(self, texts: list[str]) -> np.ndarray:
        r = await self._client_().post(
            f"{self.emb_url}/embeddings",
            headers={"Authorization": f"Bearer {self.emb_key}"} if self.emb_key else {},
            json={"model": self.emb_model, "input": texts},
        )
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return np.asarray([d["embedding"] for d in data], dtype=np.float32)

    async def _api_rerank(self, query: str, docs: list[str]) -> list[dict]:
        r = await self._client_().post(
            self.rr_url,
            headers={"Authorization": f"Bearer {self.rr_key}"} if self.rr_key else {},
            json={"model": self.rr_model, "query": query, "documents": docs},
        )
        r.raise_for_status()
        return sorted(r.json().get("results", []), key=lambda x: -float(x.get("relevance_score", 0.0)))
