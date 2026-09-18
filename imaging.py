# SPDX-License-Identifier: LGPL-3.0-or-later
"""图片本地转码：原图 -> WebP（限制长边、质量参数可配）。

jitter_bytes：发送前扰动——视觉不可感知的随机微裁边/微缩放/亮度对比度微调/
细噪声 + 质量随机化重编码，每次输出字节级唯一，用于规避平台按 md5/感知指纹
的图片拦截（同一缓存图反复发送字节完全相同，被标记一次即反复命中）。"""

from __future__ import annotations

import random
from io import BytesIO

import numpy
from PIL import Image, ImageEnhance


def convert_to_webp(data: bytes, max_px: int = 1080, quality: int = 95) -> bytes:
    img = Image.open(BytesIO(data))
    animated = bool(getattr(img, "is_animated", False))
    if not animated:
        # 两步缩放：reduce 是整型 box 滤镜(极快)，再 BILINEAR 精修到目标尺寸
        if max(img.size) > max_px * 2:
            factor = max(img.size) // (max_px * 2)
            if factor > 1:
                img = img.reduce(factor)
        img.thumbnail((max_px, max_px), Image.BILINEAR)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
        out = BytesIO()
        img.save(out, "WEBP", quality=quality, method=1)
        return out.getvalue()
    # 动图（GIF）：整段转 animated webp，不缩放以避免逐帧处理开销
    out = BytesIO()
    img.save(out, "WEBP", save_all=True, quality=quality, method=1)
    return out.getvalue()


def jitter_bytes(data: bytes) -> bytes:
    """发送前扰动，输出视觉等价但字节/指纹唯一的 webp。动图透传。

    各步骤都针对一类指纹：裁边+缩放改变几何对齐（pHash/dHash 分桶移位），
    亮度/对比度微调改变全局统计，细噪声打破逐像素比对；幅度均在 1080p 下
    不可感知的量级（裁边 ≤4px、缩放 ±1.5%、光度 ±1.5%、噪声 σ=1.5）。"""
    img = Image.open(BytesIO(data))
    if getattr(img, "is_animated", False):
        return data
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
    w, h = img.size
    l, t, r, b = (random.randint(0, 4) for _ in range(4))
    if w - l - r > 16 and h - t - b > 16 and (l or t or r or b):
        img = img.crop((l, t, w - r, h - b))
    f = random.uniform(0.985, 1.015)
    img = img.resize((max(1, round(img.width * f)), max(1, round(img.height * f))),
                     Image.BILINEAR)
    img = ImageEnhance.Brightness(img).enhance(random.uniform(0.985, 1.015))
    img = ImageEnhance.Contrast(img).enhance(random.uniform(0.985, 1.015))
    arr = numpy.array(img)  # 可写副本；alpha 通道不动，只扰动 RGB
    noise = numpy.random.default_rng().normal(0.0, 1.5, arr.shape[:2] + (3,))
    arr[..., :3] = numpy.clip(arr[..., :3].astype(numpy.int16) + noise, 0, 255)
    out = BytesIO()
    Image.fromarray(arr).save(out, "WEBP", quality=random.randint(89, 95), method=1)
    return out.getvalue()
