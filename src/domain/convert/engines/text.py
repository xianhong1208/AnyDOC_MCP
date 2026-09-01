"""Plain-text formats -> Markdown

Covers the few plain-text formats neither anydoc nor Pandoc handles (json /
xml / tsv). These files are already text; "conversion" just wraps them in a
shape the model reads comfortably: structured data goes into a code fence to
keep indentation, and TSV is laid out as a Markdown table.

No dependencies on purpose: installing a parser for these three formats is not
worth it.
"""

import asyncio
import csv
import io

from src.domain.convert.engines.base import ConversionEngine
from src.domain.convert.types import ConversionFailedError
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

SUPPORTED_SOURCES = {"json", "xml", "tsv"}

# Beyond this many rows the table is truncated: a 50,000-row TSV rendered as a
# Markdown table helps the model not at all and only floods the context.
MAX_TABLE_ROWS = 500


class TextEngine(ConversionEngine):
    """json / xml / tsv -> md (pure Python, no dependencies)"""

    name = "text"

    def can_handle(self, src: str, dst: str) -> bool:
        return dst == "md" and src in SUPPORTED_SOURCES

    async def convert(self, data: bytes, src: str, dst: str, **opts) -> bytes:
        if dst != "md" or src not in SUPPORTED_SOURCES:
            raise ConversionFailedError(
                self.name, src, dst, f"TextEngine cannot do {src} → {dst}"
            )

        def _run() -> str:
            text = _decode(data, src, dst)
            return _tsv_to_table(text) if src == "tsv" else f"```{src}\n{text.strip()}\n```"

        result = (await asyncio.to_thread(_run)).encode("utf-8")
        logger.info(f"[text] {src} → md: {len(data)} → {len(result)} bytes")
        return result


def _decode(data: bytes, src: str, dst: str) -> str:
    for encoding in ("utf-8", "utf-8-sig", "big5", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ConversionFailedError("text", src, dst, "could not determine the file encoding")


def _tsv_to_table(text: str) -> str:
    rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
    if not rows:
        return ""

    truncated = ""
    if len(rows) > MAX_TABLE_ROWS:
        truncated = f"\n\n_({len(rows)} rows in total; only the first {MAX_TABLE_ROWS} shown)_"
        rows = rows[:MAX_TABLE_ROWS]

    header, *body = rows
    width = len(header)
    lines = [
        "| " + " | ".join(_escape(c) for c in header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for row in body:
        # Pad or trim to the header width, otherwise the Markdown table breaks
        cells = (row + [""] * width)[:width]
        lines.append("| " + " | ".join(_escape(c) for c in cells) + " |")
    return "\n".join(lines) + truncated


def _escape(cell: str) -> str:
    """A | inside a cell would split the table"""
    return cell.replace("|", "\\|").replace("\n", " ").strip()
