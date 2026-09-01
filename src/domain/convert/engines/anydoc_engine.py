"""anydoc engine: Office / PDF / EPUB -> GitHub-Flavored Markdown

Firecrawl's anydoc (Rust with PyO3 bindings, MIT). It replaced MarkItDown for
these reasons:

  speed      measured 22x faster on docx, 86x on xlsx, 17x on epub, 2x on pdf
  quality    keeps blockquotes, [^1] footnote syntax, bold/italic; MarkItDown
             drops all of these
  deps       abi3 wheel, no onnxruntime/torch. MarkItDown[all] pulls in 411MB
             of dependencies and pins the Python version because onnxruntime
             only ships wheels up to cp313
  interface  to_markdown_bytes() takes bytes directly; no temp file needed

It only does one-way "document -> Markdown" and never produces files; file
generation remains the job of LibreOffice and Pandoc.

anydoc does not handle images, and it explicitly rejects scanned PDFs (see the
OCR fallback below).
"""

import asyncio
import base64

from src.domain.convert.engines.base import ConversionEngine
from src.domain.convert.types import ConversionFailedError
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

# Source formats anydoc accepts (all convert to md only). html / json / xml /
# txt are not included; those go to Pandoc and TextEngine.
SUPPORTED_SOURCES = {
    "pdf", "docx", "doc", "odt", "rtf", "epub",
    "xlsx", "xls", "ods", "pptx", "ppt", "odp", "csv",
}


_IMAGE_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "bmp": "image/bmp", "tiff": "image/tiff",
    "webp": "image/webp",
}


def _asset_images(data: bytes, fmt) -> list:
    """Pull embedded images from anydoc's document model

    Returns [(index, media_type, bytes), ...].

    The document model from `to_document()` carries assets: the embedded images
    of docx / pptx / odt / epub are all in there with a correct media_type. Far
    more reliable than parsing the container ourselves.

    Note that `to_markdown()` **drops images entirely** (only the alt text
    survives as a piece of text), so getting images always means going through
    to_document.

    Not applicable to PDF: anydoc refuses to_document for PDF ("PDF converts
    directly to Markdown"); PDF images are extracted with pypdf instead.
    """
    import anydoc

    doc = anydoc.to_document(data, fmt)
    assets = list(doc.assets or [])
    return [
        (i + 1, a.media_type or "image/png", a.data)
        for i, a in enumerate(assets)
        if a.data and (a.media_type or "").startswith("image/")
    ]


def _render_images(items: list, heading: str, label: str) -> str:
    """Render a list of (index, mime, bytes) as a Markdown image section"""
    if not items:
        return ""
    parts = [f"\n\n---\n\n## {heading}\n", f"_{label}_\n"]
    for index, mime, blob in items:
        encoded = base64.b64encode(blob).decode("ascii")
        # Leave the alt text empty: pandoc turns it into a caption below the
        # image, and an internal index would only leave a meaningless line in
        # the user's Word file.
        parts.append(f"\n**Image {index}**\n\n![](data:{mime};base64,{encoded})\n")
    return "".join(parts)


async def _images_as_markdown(pdf_bytes: bytes) -> str:
    """Turn the PDF's embedded images into a Markdown section appended after the text

    Why data: URIs rather than files on disk: each hop of the conversion
    pipeline passes **bytes**, not a directory. `![](logo.png)` in the markdown
    would not resolve at the next hop (pandoc). A data: URI embeds the image in
    the markdown text itself, and pandoc restores it as word/media/*.png inside
    the docx. Verified to work, and the pipeline's data model stays untouched.

    Images are always appended at the end, labelled by page number, with no
    attempt to restore layout position: image coordinates in a PDF have no
    reliable relation to the text's reading order, and forcing them in would
    put them in the wrong place.
    """
    from src.domain.convert import pdfops

    try:
        images = await pdfops.extract_images(pdf_bytes)
    except Exception as e:
        # A failed image extraction must not fail the whole conversion; the
        # text is still a valuable result
        logger.warning(f"[anydoc] image extraction failed, continuing text-only: {e}")
        return ""

    if not images:
        return ""

    parts = ["\n\n---\n\n## Images from the original document\n",
             "_The images below were extracted from the PDF and are listed by page; "
             "their original placement cannot be restored._\n"]
    for page_no, name, blob in images:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else "png"
        mime = _IMAGE_MIME.get(ext, "image/png")
        encoded = base64.b64encode(blob).decode("ascii")
        # Leave the alt text empty: pandoc turns it into a caption below the
        # image, and the PDF's internal image name (Im15.jpg) would only leave a
        # meaningless line in the user's Word file. The bold page line above the
        # image is enough.
        parts.append(f"\n**Page {page_no}**\n\n![](data:{mime};base64,{encoded})\n")

    logger.info(f"[anydoc] embedded {len(images)} image(s) as data URIs")
    return "".join(parts)


class AnydocEngine(ConversionEngine):
    """firecrawl-anydoc (Rust, pure wheel install, no system dependencies)"""

    name = "anydoc"

    def is_available(self) -> bool:
        try:
            import anydoc  # noqa: F401
            return True
        except ImportError:
            return False

    def can_handle(self, src: str, dst: str) -> bool:
        return self.is_available() and dst == "md" and src in SUPPORTED_SOURCES

    async def convert(self, data: bytes, src: str, dst: str, **opts) -> bytes:
        if dst != "md":
            raise ConversionFailedError(self.name, src, dst, "anydoc only produces markdown")

        try:
            import anydoc
        except ImportError:
            raise ConversionFailedError(
                self.name, src, dst, "firecrawl-anydoc package not installed"
            )

        # Pass the format explicitly instead of letting anydoc sniff: our
        # detection chain (extension -> MIME -> magic bytes) is more reliable
        # than content sniffing, especially for ZIP-based Office formats.
        fmt = anydoc.format_from_extension(src)

        def _run() -> str:
            return anydoc.to_markdown_bytes(data, fmt)

        # Get the text first, whether from anydoc itself or the OCR fallback;
        # image embedding happens once, at the end.
        #
        # This used to be a `return fallback` early exit, so scanned/image-only
        # PDFs taking the OCR path skipped image embedding entirely. A typical
        # bug shape: a later-added step is hung on the tail of the "main path"
        # only, while the error-handling branches each return early, and the new
        # feature silently does nothing on those branches.
        try:
            # The conversion releases the GIL, but the call itself still blocks;
            # run it in the thread pool so other requests on the event loop keep
            # moving.
            text = await asyncio.to_thread(_run)
        except anydoc.EncryptedError:
            raise ConversionFailedError(
                self.name, src, dst,
                "The file is password-protected and its content cannot be read. "
                "Ask the user for an unencrypted version.",
            )
        except anydoc.UnsupportedError as e:
            text = await self._ocr_or_fail(data, src, dst, str(e), **opts)
        except anydoc.ConvertError as e:
            raise ConversionFailedError(self.name, src, dst, str(e)[:400])

        if not text.strip():
            text = await self._ocr_or_fail(
                data, src, dst,
                "The document parsed successfully but contains no text.", **opts
            )

        if opts.get("embed_images"):
            if src == "pdf":
                # PDF has no document model; images come from XObjects via pypdf
                text += await _images_as_markdown(data)
            else:
                text += await self._asset_images_markdown(data, fmt)

        result = text.encode("utf-8")
        logger.info(f"[anydoc] {src} → md: {len(data)} → {len(result)} bytes")
        return result

    async def _asset_images_markdown(self, data: bytes, fmt) -> str:
        """Embedded images from Office / EPUB sources

        to_markdown() drops images, so a second pass through to_document() is
        needed to get the assets. The cost is one extra parse (measured 1-20ms);
        the payoff is that images survive when pptx / xlsx / epub etc. are
        converted to an editable document.
        """
        try:
            items = await asyncio.to_thread(_asset_images, data, fmt)
        except Exception as e:
            # This path is a bonus; its failure must not take the already
            # extracted text down with it
            logger.warning(f"[anydoc] asset extraction failed, text only: {e}")
            return ""

        if not items:
            return ""
        logger.info(f"[anydoc] embedded {len(items)} asset image(s)")
        return _render_images(
            items, "Images from the original document",
            "The images below were extracted from the original document in order of "
            "appearance; their original paragraph positions are not preserved.",
        )

    async def _ocr_or_fail(self, data: bytes, src: str, dst: str,
                           reason: str, **opts) -> str:
        """Let OCR take over; if it cannot, raise the original failure reason

        Returns text rather than bytes so the caller can keep going (e.g. append
        images): processing after either path should be identical.
        """
        fallback = await self._try_ocr(data, src, dst, reason, **opts)
        if fallback is None:
            raise ConversionFailedError(self.name, src, dst, reason[:400])
        return fallback.decode("utf-8", errors="replace")

    async def _try_ocr(self, data: bytes, src: str, dst: str,
                       reason: str, **opts) -> bytes | None:
        """OCR takeover for scanned PDFs

        On a PDF without a text layer anydoc raises an explicit
        `UnsupportedError: PDF has no extractable text (Scanned, N pages): OCR is required`.
        A clean signal, unlike MarkItDown, which silently returned an empty string.

        Engines calling each other does break the layering, but the alternative
        is a generic fallback mechanism in the planner; that complexity is not
        worth it for a single case. Returns None when OCR cannot take over, and
        the caller proceeds with its original error handling.
        """
        if src != "pdf":
            return None

        from src.domain.convert.engines.ocr import OcrEngine

        ocr = OcrEngine()
        if not ocr.can_ocr_pdf():
            logger.warning(
                f"[anydoc] {reason} -- OCR required but tesseract/poppler not installed"
            )
            return None

        logger.info(f"[anydoc] {reason} → falling back to OCR")
        return await ocr.convert(data, src, dst, **opts)
