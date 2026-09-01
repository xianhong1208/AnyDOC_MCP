"""Desktop conversion facade: wraps the domain layer as "file in, file out".

The GUI never touches the engines and never re-implements conversion logic. The
whole of `src/domain/convert/` (routing table, multi-hop planning, quality gate,
image preservation, OCR fallback) is reused as-is; the MCP tools and this GUI are
two front ends over the same core.

The only difference is the shape of input and output:
    MCP      base64 string -> ConversionOutput -> File content block
    Desktop  disk path     -> ConversionOutput -> written back to disk
"""

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# After PyInstaller bundling src/ sits alongside; when run directly from desktop/
# sys.path needs the project root added.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.domain.convert.registry import (
    available_engines,
    capability_matrix,
    plan_conversion,
)
from src.domain.convert.service import IMAGE_CARRYING_FORMATS, run_plan
from src.domain.convert.types import (
    DomainException,
    SourceFile,
    detect_format,
    normalize_format,
    stem_of,
)


@dataclass
class ConversionResult:
    """Result of converting one file; success and failure share the same type.

    In a batch, "three succeed, one fails" is the normal case. Expressing failure
    as an exception would force callers to wrap a try around the loop, and the
    failed file still has to appear in the list rather than disappear.
    """
    source: Path
    ok: bool
    output: Path | None = None
    message: str = ""
    lossy: bool = False


def target_formats_for(source: Path) -> list[str]:
    """Formats this file can be converted to (given the engines actually installed).

    Lists direct routes plus the common multi-hop targets. Enumerating every
    multi-hop combination would make the dropdown unusably long, and most of those
    combinations are never chosen anyway.
    """
    fmt = detect_format(file_name=source.name, data=_head(source))
    if not fmt:
        return []

    direct = set(capability_matrix().get(fmt, []))
    # Multi-hop targets are probed separately: the most common combination,
    # md -> pdf, is not in the direct routing table
    for candidate in ("pdf", "docx", "md", "html", "txt", "pptx", "xlsx", "odt", "epub"):
        if candidate == fmt or candidate in direct:
            continue
        try:
            plan_conversion(fmt, candidate, prefer_fidelity=False)
            direct.add(candidate)
        except DomainException:
            pass
    return sorted(direct)


def detect_source_format(source: Path) -> str | None:
    return detect_format(file_name=source.name, data=_head(source))


def _head(path: Path, size: int = 65536) -> bytes:
    """Read only the file head for format detection; no need to load 50MB for that.

    64KB covers every signal except the ZIP central directory; full ZIP and OLE
    detection runs again on the complete content when the conversion happens.
    """
    try:
        with path.open("rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def convert_file(
    source: Path,
    target_format: str,
    output_dir: Path | None = None,
    overwrite: bool = False,
) -> ConversionResult:
    """Convert one file and return the result (never raises).

    The output keeps the original file stem: the desktop app has the real file
    name, unlike the MCP path where the name competes with the host application's
    file-injection (artifact) mechanism.
    """
    dst = normalize_format(target_format)
    if not dst:
        return ConversionResult(
            source, False, message=f"Unsupported target format: {target_format}"
        )

    try:
        data = source.read_bytes()
    except OSError as e:
        return ConversionResult(source, False, message=f"Failed to read file: {e}")

    src_fmt = detect_format(file_name=source.name, mime_type=None, data=data)
    if not src_fmt:
        return ConversionResult(source, False, message="Could not recognize this file's format")

    if src_fmt == dst:
        return ConversionResult(
            source, False, message="Source and target formats are the same; nothing to do"
        )

    try:
        # The desktop app always allows lossy conversions: the user picked the
        # target format themselves, and refusing would leave them stuck. Quality
        # loss is flagged in the returned message instead.
        plan = plan_conversion(src_fmt, dst, prefer_fidelity=False)
        payload = asyncio.run(run_plan(
            SourceFile(data=data, fmt=src_fmt, name=stem_of(source.name)),
            plan,
            embed_images=dst in IMAGE_CARRYING_FORMATS,
        ))
    except DomainException as e:
        return ConversionResult(source, False, message=e.message)
    except Exception as e:  # an unexpected engine error must not crash the whole GUI
        return ConversionResult(source, False, message=f"Conversion failed: {e}")

    destination = _unique_path(
        (output_dir or source.parent) / f"{source.stem}.{dst}", overwrite
    )
    try:
        destination.write_bytes(payload)
    except OSError as e:
        return ConversionResult(source, False, message=f"Failed to write file: {e}")

    lossy = any(r.fidelity == "lossy" for r in plan)
    hops = " -> ".join([plan[0].src] + [r.dst for r in plan])
    return ConversionResult(
        source, True, output=destination,
        message=f"{hops} ({destination.name})", lossy=lossy,
    )


def convert_many(
    sources: list[Path],
    target_format: str,
    output_dir: Path | None = None,
    on_progress: Callable[[int, int, Path], None] | None = None,
) -> list[ConversionResult]:
    """Batch conversion. One file failing does not affect the others."""
    results = []
    total = len(sources)
    for index, source in enumerate(sources, start=1):
        if on_progress:
            on_progress(index, total, source)
        results.append(convert_file(source, target_format, output_dir))
    return results


def _unique_path(path: Path, overwrite: bool) -> Path:
    """Avoid overwriting existing files; a desktop tool must not eat the user's files.

    When report.pdf exists, save as report (1).pdf, following the Windows Explorer
    convention.
    """
    if overwrite or not path.exists():
        return path
    for n in range(1, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def engine_status() -> dict:
    """Readiness of each engine, for GUI display and diagnostics."""
    return available_engines()


def missing_engine_hint() -> str:
    """One-sentence explanation for the user when engines are missing.

    No technical names: the user does not need to know what pandoc is, only which
    feature is missing and how to get it back.
    """
    status = available_engines()
    missing = []
    if not status.get("libreoffice"):
        missing.append("conversion between Office documents and PDF")
    if not status.get("pandoc"):
        missing.append("conversion between Markdown/HTML and Word")
    if not status.get("ocr"):
        missing.append("text recognition for scans and images")
    if not missing:
        return ""
    return (
        "Currently unavailable: " + ", ".join(missing) +
        ". Run \"Repair / install dependencies\" from the installer, or reinstall."
    )
