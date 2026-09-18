#!/usr/bin/env python3
"""构建语义标签索引 tag_index.npz —— 供 embedder.py 做中文→yande.re 标签的向量召回。

流程：
1. 复用 build_tags.py 的抓取逻辑拉「搜索有效集」（canonical count>=10 ∪ 别名，
   结果缓存 tools/.build_cache/live.json，重跑免爬）
2. 载入 tags_zh.json 反转出 en→zh 锚点；嵌入文本 = "英文标签(下划线转空格) 中文"
3. 调 bge-m3（OpenAI 兼容 /v1/embeddings，直连）批量嵌入，L2 归一化后存 float16 npz
4. 末尾跑几个探针词打印 召回+重排 分数，用于校准 embed_min_score

用法:
  HTTPS_PROXY=http://127.0.0.1:7890 python3 tools/build_embed_index.py

嵌入/重排端点必须通过环境变量指定（也可用任意 OpenAI 兼容嵌入服务 + jina/vLLM 风格
rerank 服务，模型名需与 AstrBot 插件配置里的 embed_model 一致）：
  EMBED_URL   例 https://your-embed-host/v1     EMBED_KEY  EMBED_MODEL(默认 bge-m3)
  RERANK_URL  例 https://your-rerank-host/v1/rerank  RERANK_KEY  RERANK_MODEL(默认 bge-reranker-v2-m3)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import build_tags as bt  # noqa: E402  fetch_tags / fetch_aliases / build_live

ROOT = bt.ROOT
EMB_URL = os.environ.get("EMBED_URL", "").rstrip("/")
EMB_KEY = os.environ.get("EMBED_KEY", "")
EMB_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
RR_URL = os.environ.get("RERANK_URL", "").rstrip("/")
RR_KEY = os.environ.get("RERANK_KEY", "")
RR_MODEL = os.environ.get("RERANK_MODEL", "bge-reranker-v2-m3")
BATCH = 64
CONC = 4

PROBES = ["泪痣", "颈环", "大腿袜", "和服浴衣", "白发女仆", "兔耳"]

if not EMB_URL:
    sys.exit("请用环境变量 EMBED_URL 指定嵌入服务地址（详见本文件头部说明）")


def build_corpus(live: dict, en2zh: dict) -> tuple[list[str], list[str]]:
    """返回 (标签名列表, 嵌入文本列表)，顺序一致。"""
    tags, texts = [], []
    for name in live:
        t = name.replace("_", " ")
        zh = en2zh.get(name)
        if zh:
            t = f"{t} {zh}"
        tags.append(name)
        texts.append(t)
    return tags, texts


async def embed_all(client: httpx.AsyncClient, texts: list[str]) -> np.ndarray:
    sem = asyncio.Semaphore(CONC)
    out: list[list[float]] = [[] for _ in texts]
    batches = [(i, texts[i:i + BATCH]) for i in range(0, len(texts), BATCH)]

    async def run(bi: int, chunk: list[str]):
        for attempt in range(4):
            try:
                async with sem:
                    r = await client.post(
                        f"{EMB_URL}/embeddings",
                        headers={"Authorization": f"Bearer {EMB_KEY}"},
                        json={"model": EMB_MODEL, "input": chunk},
                    )
                r.raise_for_status()
                data = r.json()["data"]
                for d in data:
                    out[bi + d["index"]] = d["embedding"]
                done = sum(1 for v in out if v)
                print(f"\r嵌入 {done}/{len(texts)}", end="", flush=True)
                return
            except Exception as exc:
                if attempt == 3:
                    raise
                print(f"\n批次失败重试({attempt + 1}): {exc}")
                await asyncio.sleep(2 * (attempt + 1))

    await asyncio.gather(*(run(bi, ch) for bi, ch in batches))
    print()
    mat = np.asarray(out, dtype=np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
    return mat


async def main():
    t0 = time.time()
    live_cache = bt.WORK / "live.json"
    if live_cache.exists():
        live = {k: tuple(v) for k, v in json.load(open(live_cache, encoding="utf-8")).items()}
        print(f"有效集(缓存): {len(live)}")
    else:
        async with httpx.AsyncClient(base_url=bt.BASE, timeout=30, follow_redirects=True, headers=bt.UA) as c:
            print("拉取 yande.re 标签库 …")
            tags = await bt.fetch_tags(c)
            print(f"canonical: {len(tags)}")
            aliases = await bt.fetch_aliases(c)
            print(f"别名: {len(aliases)}")
        live = bt.build_live(tags, aliases)
        bt.WORK.mkdir(parents=True, exist_ok=True)
        live_cache.write_text(json.dumps([[k, list(v)] for k, v in live.items()], ensure_ascii=False), encoding="utf-8")
        print(f"有效集: {len(live)}（已缓存 {live_cache}）")

    zh2en = json.load(open(ROOT / "tags_zh.json", encoding="utf-8"))
    en2zh: dict[str, str] = {}
    for zh, en in zh2en.items():
        en2zh.setdefault(en, zh)
    tags, texts = build_corpus(live, en2zh)
    cjk = re.compile(r"[\u4e00-\u9fff]")
    anchored = sum(1 for t in texts if cjk.search(t))
    print(f"语料: {len(tags)} 条，带中文锚点 {anchored}")

    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        mat = await embed_all(c, texts)
        emb_f16 = mat.astype(np.float16)
        np.savez_compressed(ROOT / "tag_index.npz", emb=emb_f16)
        meta = {
            "model": EMB_MODEL, "dim": int(mat.shape[1]), "n": len(tags),
            "built": int(time.time()), "tags": tags, "texts": texts,
        }
        (ROOT / "tag_index_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        print(f"索引就绪: {len(tags)}×{mat.shape[1]} f16 "
              f"({(ROOT / 'tag_index.npz').stat().st_size / 1e6:.1f}MB)，总耗时 {time.time() - t0:.0f}s")

        # 探针：真实链路（嵌入→余弦 top5→重排）打印分数，校准 embed_min_score
        print("\n---- 探针（余弦召回 top5 → 重排） ----")
        q = await embed_all(c, PROBES)
        for i, word in enumerate(PROBES):
            sims = (mat @ q[i]).astype(np.float32)
            top = np.argsort(-sims)[:5]
            docs = [texts[j] for j in top]
            rr = await c.post(
                RR_URL, headers={"Authorization": f"Bearer {RR_KEY}"},
                json={"model": RR_MODEL, "query": word, "documents": docs}, timeout=30)
            rr.raise_for_status()
            results = sorted(rr.json()["results"], key=lambda x: -x["relevance_score"])
            line = " | ".join(
                f"{docs[r['index']]}:{r['relevance_score']:.2f}(cos{sims[top[r['index']]]:.2f})"
                for r in results[:3])
            print(f"{word:8s} → {line}")


if __name__ == "__main__":
    asyncio.run(main())
