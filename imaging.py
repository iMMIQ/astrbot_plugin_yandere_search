"""图片本地转码：原图 -> WebP（限制长边、质量参数可配）。"""

from __future__ import annotations

from io import BytesIO

from PIL import Image


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
