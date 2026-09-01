"""Tesseract OCR engine: text recognition for images and scanned PDFs

Why it is needed: MarkItDown handles images through LLM image description, not
OCR, and without an llm_client configured it only returns EXIF metadata. The
same goes for scanned PDFs: the pages are images, and pdfminer finds no text
layer at all. Both inputs are extremely common in business settings (scanned
contracts, photographed forms); without OCR the whole path is a dead end.

The tesseract CLI is called directly rather than through pytesseract: CliEngine
already handles subprocesses, timeouts and temp directories, and an extra
Python wrapper is just one more dependency.
"""


from src.domain.convert.engines.base import CliEngine, Workspace
from src.domain.convert.engines.discovery import find_binary
from src.domain.convert.types import IMAGE_FORMATS, ConversionFailedError
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

# Load the Traditional Chinese and English models together by default. Chinese
# documents routinely mix in English terms, and chi_tra alone recognizes English
# poorly.
DEFAULT_LANGS = "chi_tra+eng"

# Page limit for scanned PDFs. Every page has to be rasterized and then OCRed,
# which is expensive; a 300-page scan would run into the timeout, so it is
# better to tell the user explicitly.
MAX_OCR_PAGES = 30

# Rasterization resolution. 150 DPI is the practical balance between OCR
# accuracy and speed; below 120 the recognition rate for CJK text drops
# noticeably.
RASTER_DPI = 150


class OcrEngine(CliEngine):
    """tesseract (images) + pdftoppm (PDF rasterization)"""

    name = "ocr"
    binary = "tesseract"

    def is_available(self) -> bool:
        """Image OCR only needs tesseract; PDF OCR additionally needs poppler's pdftoppm"""
        return find_binary("tesseract") is not None

    def can_ocr_pdf(self) -> bool:
        return self.is_available() and find_binary("pdftoppm") is not None

    async def convert(self, data: bytes, src: str, dst: str, **opts) -> bytes:
        if dst not in ("md", "txt"):
            raise ConversionFailedError(self.name, src, dst, "OCR only produces text")

        langs = opts.get("ocr_lang", DEFAULT_LANGS)
        timeout = int(opts.get("timeout", 300))

        if src == "pdf":
            text = await self._ocr_pdf(data, langs, timeout)
        elif src in IMAGE_FORMATS:
            text = await self._ocr_image(data, src, langs, timeout)
        else:
            raise ConversionFailedError(
                self.name, src, dst, f"OCR cannot handle '{src}'"
            )

        if not text.strip():
            raise ConversionFailedError(
                self.name, src, dst,
                "OCR recognized no text. The page may be blank, the image quality "
                "too low, or the document language not covered by the installed "
                "recognition models.",
            )
        return text.encode("utf-8")

    async def _ocr_image(self, data: bytes, fmt: str, langs: str, timeout: int) -> str:
        with Workspace() as ws:
            ws.write(f"input.{fmt}", data)
            # tesseract's output argument has no extension; it appends .txt itself
            argv = [self.resolved_binary(), f"input.{fmt}", "output", "-l", langs]
            await self._run(argv, cwd=ws.path, src=fmt, dst="md", timeout=timeout)
            return ws.read("output.txt").decode("utf-8", errors="replace")

    async def _ocr_pdf(self, data: bytes, langs: str, timeout: int) -> str:
        poppler = find_binary("pdftoppm")
        if not poppler:
            raise ConversionFailedError(
                self.name, "pdf", "md",
                "PDF OCR requires poppler-utils (pdftoppm), which is not installed "
                "in this environment",
            )

        with Workspace() as ws:
            ws.write("input.pdf", data)

            # Rasterize first. -r sets the DPI, -png the format, and the trailing
            # 'page' is the file name prefix: output is page-01.png / page-02.png ...
            await self._run(
                [poppler, "-png", "-r", str(RASTER_DPI),
                 "-l", str(MAX_OCR_PAGES), "input.pdf", "page"],
                cwd=ws.path, src="pdf", dst="md", timeout=timeout,
            )

            pages = sorted(ws.path.glob("page-*.png"))
            if not pages:
                raise ConversionFailedError(
                    self.name, "pdf", "md", "pdftoppm produced no page images"
                )

            logger.info(f"[ocr] rasterized {len(pages)} page(s) at {RASTER_DPI} DPI")

            # OCR page by page. Running pages concurrently saturates the CPU and
            # drags down every other concurrent request, and tesseract is already
            # multi-threaded, so sequential processing is faster overall.
            chunks = []
            for idx, page in enumerate(pages, start=1):
                await self._run(
                    [self.resolved_binary(), page.name, f"ocr-{idx}", "-l", langs],
                    cwd=ws.path, src="pdf", dst="md", timeout=timeout,
                )
                text = ws.read(f"ocr-{idx}.txt").decode("utf-8", errors="replace")
                chunks.append(f"<!-- page {idx} -->\n\n{text.strip()}")

            truncated = ""
            if len(pages) >= MAX_OCR_PAGES:
                truncated = (
                    f"\n\n---\n\n⚠️ Only the first {MAX_OCR_PAGES} pages were recognized. "
                    f"For later pages, extract that range with pdf_extract_pages first "
                    f"and send it for OCR."
                )
            return "\n\n".join(chunks) + truncated
