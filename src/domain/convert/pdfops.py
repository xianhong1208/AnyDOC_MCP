"""PDF-specific operations: split, merge, extract pages, rotate, encrypt, page count

Deliberately kept out of engines/: these are pdf -> pdf *operations*, not
format conversions. Putting them in the (src, dst) route table would muddy its
semantics (one key mapping to several behaviours). Kept separate, the route
table only ever answers one question: "how do I get from this format to that".
"""

import asyncio
import hashlib
import io

from src.domain.convert.types import ConversionFailedError
from src.log import get_mcptools_logger

logger = get_mcptools_logger()


def _require_pypdf():
    try:
        from pypdf import PdfReader, PdfWriter
        return PdfReader, PdfWriter
    except ImportError:
        raise ConversionFailedError("pypdf", "pdf", "pdf", "pypdf not installed")


def parse_page_range(spec: str, total: int) -> list[int]:
    """Parse "1-3,7,10-" into a list of 0-based page indices

    Users and the model both think in 1-based, inclusive page numbers; internally
    everything is 0-based. "10-" means page 10 through the last page.

    Raises:
        ConversionFailedError: syntax error or page number out of range
    """
    if not spec or not spec.strip():
        return list(range(total))

    pages: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            if "-" in chunk:
                start_s, end_s = chunk.split("-", 1)
                start = int(start_s) if start_s.strip() else 1
                end = int(end_s) if end_s.strip() else total
            else:
                start = end = int(chunk)
        except ValueError:
            raise ConversionFailedError(
                "pypdf", "pdf", "pdf",
                f"invalid page range syntax: '{chunk}' (expected e.g. '1-3,7,10-')",
            )

        if start < 1 or end > total or start > end:
            raise ConversionFailedError(
                "pypdf", "pdf", "pdf",
                f"page range '{chunk}' out of bounds (document has {total} pages)",
            )
        pages.extend(range(start - 1, end))

    # Deduplicate while keeping the user's order: "3,1" is meaningful (reordering)
    seen = set()
    return [p for p in pages if not (p in seen or seen.add(p))]


async def page_count(data: bytes) -> int:
    """Return the total number of pages in the PDF"""
    def _run() -> int:
        PdfReader, _ = _require_pypdf()
        return len(PdfReader(io.BytesIO(data)).pages)

    try:
        return await asyncio.to_thread(_run)
    except ConversionFailedError:
        raise
    except Exception as e:
        raise ConversionFailedError("pypdf", "pdf", "pdf", str(e)[:500])


async def extract_pages(data: bytes, page_spec: str) -> bytes:
    """Extract the given pages into a new PDF"""
    def _run() -> bytes:
        PdfReader, PdfWriter = _require_pypdf()
        reader = PdfReader(io.BytesIO(data))
        indices = parse_page_range(page_spec, len(reader.pages))

        writer = PdfWriter()
        for i in indices:
            writer.add_page(reader.pages[i])
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    result = await asyncio.to_thread(_run)
    logger.info(f"[pypdf] extract '{page_spec}': {len(result)} bytes")
    return result


async def split_pdf(data: bytes, pages_per_chunk: int = 1) -> list[tuple[str, bytes]]:
    """Split the PDF into several parts; returns [(name suffix, bytes), ...]"""
    if pages_per_chunk < 1:
        raise ConversionFailedError(
            "pypdf", "pdf", "pdf", "pages_per_chunk must be >= 1"
        )

    def _run() -> list[tuple[str, bytes]]:
        PdfReader, PdfWriter = _require_pypdf()
        reader = PdfReader(io.BytesIO(data))
        total = len(reader.pages)
        chunks = []
        for start in range(0, total, pages_per_chunk):
            end = min(start + pages_per_chunk, total)
            writer = PdfWriter()
            for i in range(start, end):
                writer.add_page(reader.pages[i])
            buffer = io.BytesIO()
            writer.write(buffer)
            label = f"p{start + 1}" if end == start + 1 else f"p{start + 1}-{end}"
            chunks.append((label, buffer.getvalue()))
        return chunks

    result = await asyncio.to_thread(_run)
    logger.info(f"[pypdf] split into {len(result)} parts")
    return result


async def merge_pdfs(parts: list[bytes]) -> bytes:
    """Merge several PDFs in order"""
    if not parts:
        raise ConversionFailedError("pypdf", "pdf", "pdf", "no input files to merge")

    def _run() -> bytes:
        PdfReader, PdfWriter = _require_pypdf()
        writer = PdfWriter()
        for idx, blob in enumerate(parts):
            try:
                for page in PdfReader(io.BytesIO(blob)).pages:
                    writer.add_page(page)
            except Exception as e:
                raise ConversionFailedError(
                    "pypdf", "pdf", "pdf",
                    f"input #{idx + 1} is not a readable PDF: {e}",
                )
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    result = await asyncio.to_thread(_run)
    logger.info(f"[pypdf] merged {len(parts)} files → {len(result)} bytes")
    return result


async def rotate_pages(data: bytes, degrees: int, page_spec: str = "") -> bytes:
    """Rotate the given pages (degrees must be a multiple of 90)"""
    if degrees % 90 != 0:
        raise ConversionFailedError(
            "pypdf", "pdf", "pdf", f"rotation must be a multiple of 90, got {degrees}"
        )

    def _run() -> bytes:
        PdfReader, PdfWriter = _require_pypdf()
        reader = PdfReader(io.BytesIO(data))
        targets = set(parse_page_range(page_spec, len(reader.pages)))

        writer = PdfWriter()
        for i, page in enumerate(reader.pages):
            if i in targets:
                page.rotate(degrees)
            writer.add_page(page)
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    return await asyncio.to_thread(_run)


# Limits for image extraction. PDFs are often littered with tiny decorative
# images (rules, icons, textures); carrying all of them into a docx only bloats
# the file without adding information.
MIN_IMAGE_BYTES = 2048          # below this it is most likely an icon or texture
MAX_IMAGES = 40
MAX_TOTAL_IMAGE_BYTES = 15 * 1024 * 1024


async def extract_images(data: bytes) -> list[tuple[int, str, bytes]]:
    """Extract the PDF's embedded images; returns [(page number, name, bytes), ...]

    Used for "PDF to editable format". That path is pdf -> md -> docx, and the
    Markdown in the middle carries text only, so images would vanish for good at
    that step: the user's docx would contain nothing but text. This was an
    actual reported problem.

    Extracted images carry no layout position (PDF image coordinates bear no
    relation to reading order), so they can only be labelled with their page
    number and appended at the end of the document; the original placement
    cannot be reconstructed.
    """
    def _run() -> list[tuple[int, str, bytes]]:
        PdfReader, _ = _require_pypdf()
        reader = PdfReader(io.BytesIO(data))

        collected: list[tuple[int, str, bytes]] = []
        seen: set = set()
        total = 0
        duplicates = 0

        for page_no, page in enumerate(reader.pages, start=1):
            try:
                images = list(page.images)
            except Exception as e:
                # A corrupt image object on one page must not fail the whole document
                logger.warning(f"[pypdf] page {page_no} image extraction failed: {e}")
                continue

            for image in images:
                blob = image.data
                if len(blob) < MIN_IMAGE_BYTES:
                    continue

                # Deduplicate by content hash. page.images reads the page's
                # resource dictionary, and PDF XObjects can be shared/inherited
                # across pages: a 4-page LibreOffice document reported all 4
                # images on every page, which would embed 16 without dedup. A
                # header logo repeated on every page is the same problem.
                digest = hashlib.md5(blob).digest()
                if digest in seen:
                    duplicates += 1
                    continue
                seen.add(digest)

                if total + len(blob) > MAX_TOTAL_IMAGE_BYTES:
                    logger.warning(
                        f"[pypdf] image extraction stopped at {len(collected)} images "
                        f"({total / 1024 / 1024:.1f} MB limit reached)"
                    )
                    return collected
                collected.append((page_no, image.name, blob))
                total += len(blob)
                if len(collected) >= MAX_IMAGES:
                    logger.warning(f"[pypdf] image extraction capped at {MAX_IMAGES}")
                    return collected

        if duplicates:
            logger.info(f"[pypdf] skipped {duplicates} duplicate image reference(s)")
        return collected

    result = await asyncio.to_thread(_run)
    logger.info(f"[pypdf] extracted {len(result)} embedded image(s)")
    return result


async def encrypt_pdf(data: bytes, user_password: str,
                      owner_password: str | None = None) -> bytes:
    """Add an open password to the PDF"""
    if not user_password:
        raise ConversionFailedError("pypdf", "pdf", "pdf", "password cannot be empty")

    def _run() -> bytes:
        PdfReader, PdfWriter = _require_pypdf()
        reader = PdfReader(io.BytesIO(data))
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.encrypt(
            user_password=user_password,
            owner_password=owner_password or user_password,
            algorithm="AES-256",
        )
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    return await asyncio.to_thread(_run)
