"""Conversion service layer: bridges uploaded input and engine execution

Responsibilities:
  - types.py     knows formats
  - engines/     know how to convert
  - registry.py  knows which engine to ask
  - service.py   <- turns the upload's base64 into a file, runs the whole path,
                    returns the result

The MCP tool layer must never touch base64 or engines; it talks to this layer only.
"""

import base64

from src.domain.convert.registry import ENGINES, Route, plan_conversion, resolve_formats
from src.domain.convert.types import (
    DOCUMENT_FORMATS,
    IMAGE_FORMATS,
    SHEET_FORMATS,
    SLIDE_FORMATS,
    TEXT_FORMATS,
    ConversionOutput,
    SourceFile,
    UnsupportedFormatError,
    detect_format,
    normalize_format,
    stem_of,
)
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

# Single-file size limit. Uploads travel in an HTTP body; an oversized file
# exhausts memory before conversion even starts.
MAX_INPUT_MB = 50

# Output formats that can carry images. When converting to one of these, the
# source PDF's embedded images are carried across.
# Plain-text formats (txt / csv / tsv / json / xml) are deliberately excluded:
# they cannot express images, and stuffing base64 into them only produces junk.
IMAGE_CARRYING_FORMATS = {
    "docx", "doc", "odt", "rtf", "epub", "pdf", "pptx", "ppt", "odp", "html", "md",
}

# The supported-format list attached to error messages. Derived from the
# constants in types rather than written by hand: a hand-written list silently
# goes stale when formats are added, and error messages are exactly where
# accuracy matters most.
_SUPPORTED_SUMMARY = "; ".join([
    "documents (" + "/".join(sorted(DOCUMENT_FORMATS)) + ")",
    "spreadsheets (" + "/".join(sorted(SHEET_FORMATS)) + ")",
    "presentations (" + "/".join(sorted(SLIDE_FORMATS)) + ")",
    "text (" + "/".join(sorted(TEXT_FORMATS)) + ")",
    "images (" + "/".join(sorted(IMAGE_FORMATS)) + ")",
])


# The base64 alphabet (plus newlines and whitespace, which show up in practice).
# Any character outside this set means the input is definitely not base64.
_BASE64_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\r\n\t "
)


def _decode_file_content(file_content: str) -> tuple[bytes, bool]:
    """Decode file_content into bytes; returns (bytes, was_plain_text)

    The contract says this parameter is base64, but in practice the model often
    pastes plain text straight in, and that is perfectly reasonable: in a
    multi-step flow (PDF -> Markdown -> Word) it is holding a Markdown string and
    has no reason to base64-encode it first. Real logs show exactly this, followed
    by `'ascii' codec can't encode characters in position 2-3` (position 2 was a
    non-ASCII heading character) and eight rounds of guess-and-retry.

    base64 is a transport encoding, not the essence of the contract; the essence
    is "this parameter carries the file content". Plain text is used as plain
    text; there is no reason to reject it.

    Detection: the base64 alphabet is narrow, so any character outside it
    (non-ASCII text, punctuation, `#`) proves plain text. Input that is within
    the alphabet but fails to decode (bad length or padding) is treated as plain
    text as well.
    """
    stripped = file_content.strip()

    if not set(stripped) <= _BASE64_CHARS:
        return stripped.encode("utf-8"), True

    try:
        return base64.b64decode(stripped, validate=False), False
    except Exception:
        # Every character is in the alphabet yet it does not decode (padding or
        # length issue): treat as plain text
        return stripped.encode("utf-8"), True


def _sniff_plain_text(raw: bytes) -> str | None:
    """Infer the format of plain text from syntactic cues

    Plain text has no magic bytes; only its shape can be inspected. Checks run
    from the most distinctive cue to the vaguest, and fall back to txt when
    nothing matches: plain text can at least still be converted.
    """
    try:
        text = raw.decode("utf-8").lstrip()
    except UnicodeDecodeError:
        return None
    if not text:
        return None

    head = text[:2000]
    lowered = head.lower()

    if lowered.startswith("<?xml") or lowered.startswith("<!doctype xml"):
        return "xml"
    if lowered.startswith("<!doctype html") or lowered.startswith("<html"):
        return "html"
    if head[0] in "{[" and head.rstrip()[-1:] in "}]":
        return "json"

    # Markdown: any one of heading, list, table, code fence or image syntax is enough
    markdown_signals = ("# ", "## ", "- ", "* ", "|", "```", "![", "> ")
    if any(signal in head for signal in markdown_signals):
        return "md"

    return "txt"


def _looks_like_a_file_name(value: str) -> bool:
    """Tell whether file_content received a file *name* rather than file content

    The model occasionally puts the file name into file_content. The signature
    is unmistakable: short, single line, has an extension.
    """
    candidate = value.strip()
    return (
        len(candidate) <= 120
        and "\n" not in candidate
        and "." in candidate
        and len(candidate.rsplit(".", 1)[-1]) <= 5
    )


def resolve_artifact_input(
    file_content: str | None,
    file_name: str | None,
    mime_type: str | None,
) -> SourceFile:
    """Resolve the triple injected by the host's file-injection (artifact) mechanism

    These parameter names (file_content / file_name / mime_type) are the host
    contract and must not change: the injection mechanism matches on parameter
    names to decide where to place the file.
    """
    if not file_content or not file_content.strip():
        raise UnsupportedFormatError(
            "No file provided. Upload a file first; the host injects it into "
            "file_content automatically."
        )

    raw, was_plain_text = _decode_file_content(file_content)

    size_mb = len(raw) / (1024 * 1024)
    if size_mb > MAX_INPUT_MB:
        raise UnsupportedFormatError(
            f"File too large: {size_mb:.1f} MB, limit is {MAX_INPUT_MB} MB. "
            "Tell the user the file exceeds the processing limit. For a PDF, "
            "pdf_extract_pages can pull out the needed page range first; for other "
            "formats ask the user to reduce the file and upload again. "
            "Retrying with the same file will not succeed."
        )

    fmt = detect_format(file_name=file_name, mime_type=mime_type, data=raw)

    # The file-name check must run before plain-text inference: _sniff_plain_text
    # falls back to "txt", and once that runs the precise "you passed a file name"
    # diagnosis is never reached, and the user gets a Word file whose only content
    # is a single line holding a file name.
    if not fmt and was_plain_text and _looks_like_a_file_name(file_content):
        # Precise diagnosis: the model sometimes puts the file name (rather than
        # the file content) into file_content. A generic "unrecognized format"
        # message sends it guessing at names and formats in a retry loop; naming
        # the exact mistake is what gets it to the right next step.
        raise UnsupportedFormatError(
            f"file_content looks like a file name ({file_content.strip()[:60]!r}) "
            "rather than file content. This parameter must hold the file itself: "
            "for an uploaded file, pass the upload reference (of the form "
            "[Uploaded Artifact: \"...\"]) and the host will substitute the content; "
            "if you are holding text (e.g. Markdown produced by a previous step), "
            "paste the **complete content** here.",
            file_name=file_name,
        )

    if not fmt and was_plain_text:
        # Plain text has no magic bytes; look at syntactic cues instead.
        # This is what makes the natural "paste Markdown, get Word" usage work.
        fmt = _sniff_plain_text(raw)

    if not fmt:
        # Wording matters: the model decides its next step from this message.
        # The old text only said "provide a file_name with an extension", so the
        # model kept moving parameters around and retrying (six wasted calls in
        # 12 seconds in real logs). Stating that format detection is already
        # reliable, plus a list of what is inherently unsupported, is what makes
        # it give up and inform the user.
        raise UnsupportedFormatError(
            "Could not recognize this file's format. This service only handles "
            "documents, spreadsheets, presentations and images; audio, video, "
            "archives, executables and similar are never supported. Supported "
            f"formats: {_SUPPORTED_SUMMARY}. "
            "If the file is one of those types, retry with a file_name that has an "
            "extension; otherwise tell the user this file type is not supported "
            "and do not retry.",
            file_name=file_name,
            mime_type=mime_type,
        )

    # Log file_name with repr so None and "" stay distinguishable; they mean
    # different failures:
    #   None  -> the model did not send the parameter; the host's setdefault should
    #            have filled in the original name, so its absence means injection
    #            did not run or the upload was not resolved
    #   ""    -> the key exists but is empty. Either the model sent an empty value
    #            (defeating setdefault), or it put the upload reference in
    #            file_name and the host blanked it
    # Printing just "(unnamed)" would conflate the two and hide which side failed.
    logger.info(
        f"Artifact resolved: file_name={file_name!r} → format={fmt}, {size_mb:.2f} MB"
    )
    return SourceFile(data=raw, fmt=fmt, name=stem_of(file_name))


async def run_plan(source: SourceFile, plan: list[Route], **opts) -> bytes:
    """Execute every hop of the path in order"""
    data = source.data
    for hop, route in enumerate(plan, start=1):
        engine = ENGINES[route.engine]
        logger.info(f"Hop {hop}/{len(plan)}: {route.describe()}")
        data = await engine.convert(data, route.src, route.dst, **opts)
    return data


async def convert_artifact(
    file_content: str | None,
    file_name: str | None,
    mime_type: str | None,
    target_format: str,
    prefer_fidelity: bool = True,
    output_name: str | None = None,
    **opts,
) -> tuple[ConversionOutput, list[Route]]:
    """Full conversion flow: validate target -> decode -> detect -> plan -> run

    **Validate target_format before touching the file.** The order matters:
    target_format is chosen entirely by the model and can be judged without any
    file content. The order used to be reversed, so when a user asked to
    "convert to wav" the model received "could not recognize the file format",
    an error unrelated to the real problem. It then guessed at file names,
    shuffled parameters and retried (six wasted calls in 12 seconds) without
    ever learning the key fact: audio formats are simply not supported.

    Returns:
        (output, the path actually taken). The path is returned so the tool
        layer can tell the model honestly which engines were involved and how
        much was lost.
    """
    if not target_format or not target_format.strip():
        # After the host resolves an upload reference, it blanks the parameter
        # that held the reference. If the model mistakenly put the reference in
        # target_format, what arrives here is an empty string.
        raise UnsupportedFormatError(
            "target_format is empty. Specify the target format explicitly (e.g. "
            "'pdf', 'docx', 'md'). The uploaded file is injected into file_content "
            "automatically; do not put a file name or upload reference into "
            "target_format."
        )

    dst = normalize_format(target_format)
    if not dst:
        raise UnsupportedFormatError(
            f"'{target_format}' is not a target format this service supports. "
            "This service only converts documents, spreadsheets, presentations and "
            "images; audio, video and archives are not supported. Available target "
            f"formats: {_SUPPORTED_SUMMARY}. "
            "Choose one of those, or tell the user this request is outside the "
            "scope of this service."
        )

    source = resolve_artifact_input(file_content, file_name, mime_type)
    src, dst = resolve_formats(source.fmt, dst)

    plan = plan_conversion(src, dst, prefer_fidelity=prefer_fidelity)
    if not plan:
        # Source and target are the same format; return as is
        return ConversionOutput(source.data, dst, output_name or source.name), []

    # When the output format can carry images, bring the source PDF's embedded
    # images along. Not enabled unconditionally: extract_text goes through this
    # path too, and a stream of base64 data URIs poured into the model's context
    # is a disaster. That caller passes embed_images explicitly.
    opts.setdefault("embed_images", dst in IMAGE_CARRYING_FORMATS)

    data = await run_plan(source, plan, **opts)
    name = stem_of(output_name, default=source.name) if output_name else source.name
    return ConversionOutput(data=data, fmt=dst, name=name), plan
