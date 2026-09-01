"""Shared conversion types and format detection

This layer knows nothing about engines. It only answers "what format is this"
and "what does a conversion failure look like". Every engine and the tool layer
depend on the normalized format codes defined here, so "jpg" / "jpeg" /
"image/jpeg" never spread through the codebase as three spellings.
"""

import io
import zipfile
from dataclasses import dataclass

from src.domain.exceptions import DomainException

# How far to scan for OLE2 directory entries. The directory usually sits near the
# start of the file; 64KB is plenty and avoids scanning huge files end to end.
_OLE_SCAN_BYTES = 65536

# Tolerance window for the PDF header. ISO 32000 does not require %PDF- at offset
# 0, and real-world readers accept leading junk (BOM, whitespace, HTTP residue).
_PDF_HEADER_SCAN_BYTES = 1024

# Replacement name used when the original file name is unavailable. It is a
# constant so upper layers can detect "we fell back" and warn about it: a user
# receiving converted.pdf is an anomaly worth logging, not a normal outcome.
FALLBACK_NAME = "converted"

# ============================================================================
# Normalized format codes
# ============================================================================
# Always lowercase, no leading dot. This is the only format identifier the
# system recognizes internally.

DOCUMENT_FORMATS = {"pdf", "docx", "doc", "odt", "rtf", "epub"}
SHEET_FORMATS = {"xlsx", "xls", "ods", "csv", "tsv"}
SLIDE_FORMATS = {"pptx", "ppt", "odp"}
TEXT_FORMATS = {"md", "html", "txt", "rst", "tex", "json", "xml"}
IMAGE_FORMATS = {"png", "jpg", "webp", "gif", "bmp", "tiff"}

ALL_FORMATS = (
    DOCUMENT_FORMATS | SHEET_FORMATS | SLIDE_FORMATS | TEXT_FORMATS | IMAGE_FORMATS
)

# Extension aliases -> normalized code
#
# Macro-enabled variants (docm / xlsm / pptm) and the slideshow variant (ppsx)
# collapse onto their base format here. They share the same ZIP+XML container;
# the only differences are an extra VBA project and a different content-type,
# and every engine (anydoc, LibreOffice) detects the format from content anyway.
# Verified: .docm and .xlsm both parse fine. Collapsing them here means we do
# not have to duplicate a full set of routes per variant.
#
# .xlsb is also folded in here, but on different grounds, which deserve a note:
#
# An earlier test with a synthetic BIFF12 container was rejected by anydoc with
# MalformedError, and I concluded xlsb was unsupported. That conclusion was
# wrong: the synthetic file was not a valid xlsb to begin with, so rejecting it
# was correct behaviour. Reading anydoc's source settled it: the doc comment on
# Format::Excel explicitly says "every container calamine reads: .xlsx, .xlsm,
# .xlsb, and binary .xls", and calamine does support BIFF12.
#
# Caveat: this has still not been verified against a real .xlsb produced by
# Excel (LibreOffice cannot export XLSB and Python has no xlsb writer). If it
# fails in practice, removing this one line restores the previous behaviour.
_EXT_ALIASES = {
    "jpeg": "jpg",
    "jpe": "jpg",
    "tif": "tiff",
    "htm": "html",
    "markdown": "md",
    "mdown": "md",
    "text": "txt",
    "latex": "tex",
    "yml": "yaml",
    "docm": "docx",
    "dotx": "docx",
    "xlsm": "xlsx",
    "pptm": "pptx",
    "ppsx": "pptx",
    "xlsb": "xlsx",
}

# MIME -> normalized code. Only informative types are listed; deliberately
# uninformative ones such as application/octet-stream are left out.
_MIME_MAP = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/epub+zip": "epub",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
    "text/markdown": "md",
    "text/html": "html",
    "text/plain": "txt",
    "text/csv": "csv",
    "text/tab-separated-values": "tsv",
    "application/json": "json",
    "application/xml": "xml",
    "text/xml": "xml",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
}

# Magic bytes -> normalized code. Formats that can be settled from the first few
# bytes alone go here. ZIP (PK) and OLE2 are "containers": identical headers,
# completely different contents. Those are handed to _sniff_zip / _sniff_ole
# below, which look inside the container instead of guessing.
_MAGIC_SIGNATURES = [
    (b"%PDF-", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"{\\rtf", "rtf"),
]

_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"

# Directory prefix inside a ZIP container -> format. OOXML uses a fixed directory
# layout to distinguish document types.
_ZIP_PART_PREFIXES = [
    ("word/", "docx"),
    ("xl/", "xlsx"),
    ("ppt/", "pptx"),
]

# Stream names inside an OLE2 compound document -> format. Directory entries are
# stored as UTF-16LE.
_OLE_STREAM_MARKERS = [
    ("WordDocument", "doc"),
    ("Workbook", "xls"),
    ("Book", "xls"),          # legacy name used by Excel 5.0/95
    ("PowerPoint Document", "ppt"),
]


# ============================================================================
# Exceptions
# ============================================================================

class UnsupportedFormatError(DomainException):
    """The source format could not be recognized, or the target is not supported"""

    def __init__(self, message: str, file_name: str | None = None,
                 mime_type: str | None = None):
        details = {}
        if file_name:
            details["file_name"] = file_name
        if mime_type:
            details["mime_type"] = mime_type
        super().__init__(message=message, error_code="UNSUPPORTED_FORMAT", details=details)


class NoConversionPathError(DomainException):
    """No path exists, or one exists but its quality is not acceptable

    `hint` is the suggested next step, written for the AI model. This exception
    becomes the error message the model reads, and what the model needs is not
    "it failed" but "so what do I do now". Without a hint it usually just relays
    the technical error to the user verbatim.
    """

    def __init__(self, source: str, target: str, hint: str | None = None):
        message = f"No usable conversion path from '{source}' to '{target}'."
        message += f" {hint}" if hint else (
            " Call list_supported_conversions to see what is available."
        )
        details = {"source_format": source, "target_format": target}
        if hint:
            details["hint"] = hint
        super().__init__(message=message, error_code="NO_CONVERSION_PATH", details=details)


class ConversionFailedError(DomainException):
    """A path exists but execution failed (engine crash, corrupt file, timeout...)"""

    def __init__(self, engine: str, source: str, target: str, reason: str):
        super().__init__(
            message=f"Conversion {source} → {target} failed in engine '{engine}': {reason}",
            error_code="CONVERSION_FAILED",
            details={"engine": engine, "source_format": source,
                     "target_format": target, "reason": reason},
        )


# ============================================================================
# Data model
# ============================================================================

@dataclass(frozen=True)
class SourceFile:
    """A decoded input file whose format has been identified"""
    data: bytes
    fmt: str
    name: str          # file name stem (no extension), used to name the output

    @property
    def size_mb(self) -> float:
        return len(self.data) / (1024 * 1024)


@dataclass(frozen=True)
class ConversionOutput:
    """A single conversion result"""
    data: bytes
    fmt: str
    name: str


# ============================================================================
# Format detection
# ============================================================================

def normalize_format(value: str | None) -> str | None:
    """Normalize a format string from the user/AI into an internal code

    Tolerates spellings such as ".PDF" / "pdf" / "PDF" / "jpeg". Returns None
    when nothing matches.
    """
    if not value:
        return None
    token = value.strip().lower().lstrip(".")
    token = _EXT_ALIASES.get(token, token)
    return token if token in ALL_FORMATS else None


def _sniff_zip(data: bytes) -> str | None:
    """Determine the format from the internal structure of a ZIP container

    docx / xlsx / pptx / odt / ods / odp / epub all start with `PK\\x03\\x04`,
    but the container contents are unambiguous:
      - ODF and EPUB carry a `mimetype` entry holding the full MIME string
      - OOXML uses fixed directories: word/, xl/, ppt/

    This is far more reliable than the extension and very cheap: zipfile only
    reads the central directory and never decompresses the contents.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()

            # ODF / EPUB: the mimetype entry gives the answer directly
            if "mimetype" in names:
                declared = archive.read("mimetype").decode("ascii", "ignore").strip()
                fmt = _MIME_MAP.get(declared)
                if fmt:
                    return fmt

            name_set = set(names)
            for prefix, fmt in _ZIP_PART_PREFIXES:
                if any(n.startswith(prefix) for n in name_set):
                    # XLSB stores BIFF12 binary rather than XML but still lives
                    # under xl/. anydoc reads it via calamine, so it is reported
                    # as xlsx too (shared internal code; every engine detects the
                    # real format from content).
                    return fmt
    except (zipfile.BadZipFile, KeyError, OSError):
        return None
    return None


def _ole_directory(data: bytes) -> bytes:
    """Locate the directory sectors from the OLE2 header and return them

    Scanning the start of the file is not enough: an OLE2 directory can live
    anywhere. LibreOffice-produced .ppt files were observed with the directory at
    the **end** (499200-byte file, directory at 498560). Computing the position
    from the header fields is the only reliable approach:

        0x1E  uint16  sector size as a power of two (9 -> 512 bytes, 12 -> 4096)
        0x30  uint32  number of the first directory sector

    Offset is (number + 1) * sector size; the +1 accounts for the 512-byte header,
    which sector numbers do not include. Eight sectors are enough to cover every
    stream name.
    """
    if len(data) < 512:
        return b""
    sector_shift = int.from_bytes(data[0x1E:0x20], "little")
    if not 6 <= sector_shift <= 16:          # outside the sane range: corrupt header
        return b""
    sector_size = 1 << sector_shift
    dir_start = int.from_bytes(data[0x30:0x34], "little")
    if dir_start >= 0xFFFFFFFE:              # FREESECT / ENDOFCHAIN
        return b""
    offset = (dir_start + 1) * sector_size
    return data[offset:offset + sector_size * 8]


def _sniff_ole(data: bytes) -> str | None:
    """Determine the format from stream names in an OLE2 compound document

    doc / xls / ppt all start with `\\xd0\\xcf\\x11\\xe0`; only the streams listed
    in the directory tell them apart. Directory entry names are stored as
    UTF-16LE, so searching for marker strings is enough. Parsing the full OLE
    structure would need an extra dependency, and all we need is the type.

    **Matching must be case-insensitive.** The CFB spec itself compares names
    after upper-casing, and producers are inconsistent in practice (`Workbook` /
    `WORKBOOK` / `Book`). anydoc's detect.rs uses `eq_ignore_ascii_case` for
    this and explicitly notes "producers vary"; this mirrors that approach.
    """
    regions = [_ole_directory(data)]
    if not regions[0]:
        # Fallback when the header is untrustworthy: scan a slice at each end.
        # In practice the directory is either near the start or at the very end.
        regions = [data[:_OLE_SCAN_BYTES], data[-_OLE_SCAN_BYTES:]]

    for region in regions:
        # Entries are UTF-16LE; decode, then lower-case once to cover all spellings
        names = region.decode("utf-16-le", errors="ignore").lower()
        for marker, fmt in _OLE_STREAM_MARKERS:
            if marker.lower() in names:
                return fmt
    return None


def detect_format(
    file_name: str | None = None,
    mime_type: str | None = None,
    data: bytes | None = None,
) -> str | None:
    """Four-stage detection: extension -> MIME -> container structure -> magic bytes

    The extension wins over MIME because uploads often arrive with an
    uninformative MIME such as application/octet-stream, while the file name is
    almost always genuine.

    **But the file name is frequently missing altogether.** After the host
    application's file-injection (artifact) mechanism places the file into
    `file_content`, it blanks the parameter that originally held the upload
    reference. If the model put that reference into `file_name` (a natural
    mistake), the name is gone. The log line
    `Artifact resolved: (unnamed) -> format=pptx` is exactly this situation.

    Container sniffing is therefore not a nicety but the thing that makes this
    service work reliably: with it, docx/xlsx/pptx/odt/ods/odp/epub/doc/xls/ppt
    are all identified correctly without a file name. Returns None only when
    all four stages fail.
    """
    if file_name and "." in file_name:
        fmt = normalize_format(file_name.rsplit(".", 1)[-1])
        if fmt:
            return fmt

    if mime_type:
        fmt = _MIME_MAP.get(mime_type.split(";")[0].strip().lower())
        if fmt:
            return fmt

    if data:
        if data.startswith(_ZIP_MAGIC):
            fmt = _sniff_zip(data)
            if fmt:
                return fmt
        elif data.startswith(_OLE_MAGIC):
            fmt = _sniff_ole(data)
            if fmt:
                return fmt

        for signature, fmt in _MAGIC_SIGNATURES:
            if data.startswith(signature):
                return fmt

        # A PDF header may be preceded by junk: ISO 32000 does not require %PDF-
        # at offset 0 and real readers accept it. Scanning the first 1024 bytes
        # is what anydoc does; mirrored here. With startswith alone, a PDF with a
        # BOM or leading whitespace would fail detection.
        if b"%PDF-" in data[:_PDF_HEADER_SCAN_BYTES]:
            return "pdf"

    return None


def stem_of(file_name: str | None, default: str = FALLBACK_NAME) -> str:
    """Return the file name stem (path and extension stripped) for naming output"""
    if not file_name:
        return default
    base = file_name.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return stem.strip() or default
