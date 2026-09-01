"""Pillow engine: image format conversion, resizing, compression"""

import asyncio
import io

from src.domain.convert.engines.base import ConversionEngine
from src.domain.convert.types import ConversionFailedError
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

# Internal code -> Pillow format name
_PIL_FORMAT = {
    "png": "PNG",
    "jpg": "JPEG",
    "webp": "WEBP",
    "gif": "GIF",
    "bmp": "BMP",
    "tiff": "TIFF",
}

# Formats without an alpha channel: transparent areas must be flattened onto a
# white background first, otherwise Pillow raises
_NO_ALPHA = {"jpg", "bmp"}


class ImageEngine(ConversionEngine):
    """Pillow image conversion"""

    name = "pillow"

    def is_available(self) -> bool:
        try:
            import PIL  # noqa: F401
            return True
        except ImportError:
            return False

    async def convert(self, data: bytes, src: str, dst: str, **opts) -> bytes:
        pil_format = _PIL_FORMAT.get(dst)
        if not pil_format:
            raise ConversionFailedError(
                self.name, src, dst, f"Pillow cannot write '{dst}'"
            )

        quality = int(opts.get("quality", 90))
        max_width = opts.get("max_width")

        def _run() -> bytes:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as img:
                if dst in _NO_ALPHA and img.mode in ("RGBA", "LA", "P"):
                    # A transparent background turns into black noise in JPEG;
                    # always fill with white
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    rgba = img.convert("RGBA")
                    background.paste(rgba, mask=rgba.split()[-1])
                    img = background
                elif img.mode == "P" and dst != "gif":
                    img = img.convert("RGB")

                if max_width and img.width > int(max_width):
                    ratio = int(max_width) / img.width
                    img = img.resize(
                        (int(max_width), max(1, round(img.height * ratio))),
                        Image.LANCZOS,
                    )

                buffer = io.BytesIO()
                save_kwargs = {}
                if pil_format in ("JPEG", "WEBP"):
                    save_kwargs["quality"] = quality
                if pil_format == "JPEG":
                    save_kwargs["optimize"] = True
                img.save(buffer, format=pil_format, **save_kwargs)
                return buffer.getvalue()

        try:
            result = await asyncio.to_thread(_run)
        except ImportError:
            raise ConversionFailedError(self.name, src, dst, "Pillow not installed")
        except Exception as e:
            raise ConversionFailedError(self.name, src, dst, str(e)[:500])

        logger.info(f"[pillow] {src} → {dst}: {len(data)} → {len(result)} bytes")
        return result
