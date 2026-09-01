"""AnyDoc MCP tools

Input contract: file_content (base64) / file_name / mime_type, injected
automatically by the host application's file-injection (artifact) mechanism.
The parameter names must not change; the host matches on them.

Output contract: a list of [text summary, File(...)], where File comes from
fastmcp.utilities.types; the client receives a downloadable file directly
(the same pattern other MCP servers use).

**Every tool that returns a list must be declared `@mcp.tool(output_schema=None)`.**
FastMCP derives an outputSchema from the `-> list` annotation and then demands
structured output; but we return content blocks (text + file), not structured
data, so the client gets:

    ToolError: Output validation error: outputSchema defined but no structured
    output returned

This error is invisible in unit tests: the tool function returns fine, and it
is FastMCP that rejects it while serializing the response. It only shows up
when a real MCP client makes the call.
"""

from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.utilities.types import File
from pydantic import Field

from src.domain.convert import pdfops
from src.domain.convert.registry import available_engines, capability_matrix
from src.domain.convert.service import convert_artifact, resolve_artifact_input
from src.domain.convert.types import FALLBACK_NAME as _FALLBACK_NAME
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

# Shared parameter descriptions for the upload triple, so each tool does not
# repeat them.
#
# The wording of these two descriptions directly decides whether the service
# works; see the note on file_name below.
_FILE_CONTENT = Annotated[str | None, Field(
    description="The file uploaded by the user. If you see a reference such as "
                "[Uploaded Artifact: \"xxx\"], put it in this parameter; the host "
                "replaces it with the actual file content automatically.")]
_FILE_NAME = Annotated[str | None, Field(
    description="Original file name (with extension). Filled in by the host "
                "automatically; you do not need to set it. Never put an upload "
                "reference here.")]
_MIME_TYPE = Annotated[str | None, Field(
    description="MIME type (optional, usually empty)")]

# ------------------------------------------------------------------------
# Why the file_content description actively invites the model to put the
# upload reference there
#
# The host's file-injection mechanism works like this:
#
#     tool_args["file_content"] = b64
#     tool_args.setdefault("file_name", resolved_key)
#     if key != "file_content":
#         tool_args[key] = ""          # <- blanks the parameter that held the reference
#
# where key is whichever parameter the model put the reference into. So:
#
#   reference in file_content  -> key == "file_content", nothing is blanked,
#                                 setdefault fills the original name into
#                                 file_name (good)
#   reference in file_name     -> setdefault is a no-op because the key exists,
#                                 then file_name is blanked to "" (bad)
#
# The latter really happened in production logs
# (`Artifact resolved: (unnamed) -> format=pptx`), with two symptoms: the first
# call failed because the format could not be detected, and the returned file
# lost its original name. The old description said "the model need not fill
# this in", which pushed the model down exactly the wrong path.
#
# Format detection no longer depends on the file name (detect_format inspects
# the container structure), but the name still decides what the returned file
# is called, so this guidance is still necessary.
#
# mime_type is never injected on the MCP path (only the host's REST-style
# path does that). The parameter is kept for compatibility with that path; do
# not remove it because it "looks unused".
# ------------------------------------------------------------------------


def _log_delivered(name: str, fmt: str, size: int, source_name: str | None) -> None:
    """Log the file name the user will actually download

    Without this line, "is the file name right" can only be learned from user
    reports: server logs look perfectly normal up to `Artifact resolved` and
    never show what the delivered file was called. Falling back to the default
    name gets an extra warning, because it means the original name was lost
    somewhere.
    """
    delivered = f"{name}.{fmt}"
    if name == _FALLBACK_NAME:
        logger.warning(
            f"[DELIVER] {delivered} ({size / 1024:.1f} KB) -- "
            f"fallback file name used. The original name did not arrive "
            f"(file_name={source_name!r}) and the model gave no output_name, so the "
            f"user receives a file with an unrecognizable name."
        )
    else:
        logger.info(f"[DELIVER] {delivered} ({size / 1024:.1f} KB)")


def _fidelity_warning(plan) -> str:
    """Phrase the quality loss along the path as a warning for the model

    Once the model has the result it must decide what to tell the user. If a
    lossy engine was involved, this text lets it know it should warn the user
    that the layout has changed, instead of reporting a bare "conversion
    succeeded".
    """
    if not plan:
        return ""
    lossy = [r for r in plan if r.fidelity == "lossy"]
    if lossy:
        # The wording must be precise about what survived and what did not.
        # The old text said "images have been lost", which stopped being true
        # once image extraction was added; the model relayed it, and users
        # assumed the images were gone without looking at the end of the
        # document. A wrong warning misleads more than no warning.
        return (
            "\n⚠️ This conversion went through content extraction. Proactively tell "
            "the user two things: (1) text and images are preserved, but **layout, "
            "fonts and table styling have been re-flowed** and need manual "
            "adjustment; (2) the original images are collected in an **\"Images "
            "from the original document\" section at the end** of the output, "
            "labelled by page, not at their original paragraph positions. The PDF "
            "does not record the relative placement of text and images, so it "
            "cannot be restored."
        )
    if len(plan) > 1:
        return (
            "\nNote: this conversion went through an intermediate format; fine "
            "styling details may differ slightly from the original."
        )
    return ""


def register_anydoc_tools(mcp: FastMCP):
    """Register every AnyDoc tool (config.yaml's function_name points here)"""

    # ------------------------------------------------------------------
    # Capability queries
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_supported_conversions(
        source_format: Annotated[str | None, Field(
            description="Set this to see only what one source format can be converted "
                        "to, e.g. 'docx'")] = None,
    ) -> dict[str, Any]:
        """List the conversion pairs this service actually supports.

        Before calling convert_document, use this tool if you are unsure whether a
        conversion is possible. It returns the paths available given the
        **engines actually installed** on this server, not a theoretical list.

        Args:
            source_format: Optional. When set, only that format's targets are returned.

        Returns:
            supported: {source format: [reachable target formats]}
            engines: availability of each engine
            note: unlisted pairs may still work via an intermediate format; just try
                  convert_document
        """
        matrix = capability_matrix()
        if source_format:
            key = source_format.strip().lower().lstrip(".")
            matrix = {key: matrix.get(key, [])}

        return {
            "supported": matrix,
            "engines": available_engines(),
            "note": (
                "This table lists single-step direct conversions only. Unlisted pairs "
                "(e.g. md -> pdf) may still be possible via an intermediate format; "
                "just call convert_document, which reports a clear error if the "
                "conversion is not feasible."
            ),
        }

    @mcp.tool()
    async def inspect_document(
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> dict[str, Any]:
        """Show basic information about an uploaded file: detected format, size,
        and the target formats it can be converted to.

        When the user uploads a file without saying what to do with it, use this
        tool first to see what you have, then decide which conversion to
        suggest. For PDFs the page count is also returned.
        """
        source = resolve_artifact_input(file_content, file_name, mime_type)
        info: dict[str, Any] = {
            "detected_format": source.fmt,
            "size_mb": round(source.size_mb, 3),
            "file_name": file_name,
            "can_convert_to": capability_matrix().get(source.fmt, []),
        }
        if source.fmt == "pdf":
            try:
                info["page_count"] = await pdfops.page_count(source.data)
            except Exception as e:
                info["page_count_error"] = str(e)
        return info

    # ------------------------------------------------------------------
    # Main conversion
    # ------------------------------------------------------------------

    @mcp.tool(output_schema=None)
    async def convert_document(
        target_format: Annotated[str, Field(
            description="Target format, e.g. pdf / docx / md / xlsx / png. If unsure "
                        "whether it is supported, call list_supported_conversions first")],
        output_name: Annotated[str, Field(
            description="Output file name without extension. Use the stem of the "
                        "user's original file name: if the upload is "
                        "\"Vendor Review 2026.pptx\", pass \"Vendor Review 2026\" so the "
                        "user recognizes the download. If the user asked for a new "
                        "name, use that instead.")],
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
        allow_quality_loss: Annotated[bool, Field(
            description="Allow layout quality to be sacrificed so the conversion can "
                        "succeed; allowed by default. The response carries an explicit "
                        "quality warning that you must relay to the user. Set to False "
                        "only when the user explicitly says the layout must not be "
                        "lost, even at the cost of not converting")] = True,
        ctx: Context = None,
    ) -> list:
        """Convert the uploaded file to the given format and return the converted file.

        Supports conversions between Office documents, PDF, Markdown, HTML,
        spreadsheets, presentations and images. The source format is detected
        automatically from the file; no need to specify it.

        Typical usage:
          - "Turn this Word file into a PDF"     -> target_format="pdf"
          - "Show me what is in this PDF"        -> target_format="md"
          - "Make a Word document from this Markdown" -> target_format="docx"
          - "Convert this image to JPG"          -> target_format="jpg"

        If the response contains a quality warning, relay it to the user proactively.
        """
        if ctx:
            await ctx.info(f"Converting to {target_format}...")

        output, plan = await convert_artifact(
            file_content=file_content,
            file_name=file_name,
            mime_type=mime_type,
            target_format=target_format,
            prefer_fidelity=not allow_quality_loss,
            output_name=output_name,
        )

        path_desc = (
            " → ".join([plan[0].src] + [r.dst for r in plan]) if plan else "no conversion needed"
        )
        engines_used = ", ".join(r.engine for r in plan) if plan else "-"
        summary = (
            f"Conversion complete: {output.name}.{output.fmt}\n"
            f"Path: {path_desc} (engines: {engines_used})\n"
            f"Size: {len(output.data) / 1024:.1f} KB"
            f"{_fidelity_warning(plan)}"
        )

        if ctx:
            await ctx.info(f"Done: {len(output.data)} bytes")

        _log_delivered(output.name, output.fmt, len(output.data), file_name)
        return [summary, File(data=output.data, format=output.fmt, name=output.name)]

    @mcp.tool()
    async def extract_text(
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
        max_chars: Annotated[int, Field(
            description="Maximum number of characters returned; longer text is "
                        "truncated. 0 means no limit")] = 50000,
    ) -> str:
        """Extract the document's text as Markdown and return it directly (no file).

        Use this when you need to *read* the user's uploaded document to answer
        questions, summarize or analyze it, rather than convert_document: this
        tool returns text you can read directly.

        Supports PDF, Word, Excel, PowerPoint, HTML, CSV and images (OCR).
        """
        output, _ = await convert_artifact(
            file_content=file_content,
            file_name=file_name,
            mime_type=mime_type,
            target_format="md",
            prefer_fidelity=False,
            # Explicitly disable image embedding. md counts as an image-carrying
            # format and would get data: URIs by default, but this tool's return
            # value is text bound for the model's context: one 500KB image would
            # become 680,000 characters of base64.
            embed_images=False,
        )
        text = output.data.decode("utf-8", errors="replace")
        if max_chars and len(text) > max_chars:
            omitted = len(text) - max_chars
            text = text[:max_chars] + f"\n\n... (truncated, {omitted} characters omitted)"
        return text

    # ------------------------------------------------------------------
    # PDF operations
    # ------------------------------------------------------------------

    @mcp.tool(output_schema=None)
    async def pdf_extract_pages(
        pages: Annotated[str, Field(
            description="Page range, 1-based and inclusive. E.g. '1-3,7,10-' means "
                        "pages 1 to 3, page 7, and page 10 through the last")],
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> list:
        """Extract the given pages from a PDF into a new PDF.

        Page order matters: pages='3,1' produces a PDF with page 3 first and
        page 1 second, which can be used to reorder pages.
        """
        source = resolve_artifact_input(file_content, file_name, mime_type)
        if source.fmt != "pdf":
            return [
                f"This is not a PDF (detected {source.fmt}). "
                "Convert it to PDF with convert_document first."
            ]

        data = await pdfops.extract_pages(source.data, pages)
        name = f"{source.name}_p{pages.replace(',', '_').replace('-', 'to')}"
        return [
            f"Extracted pages {pages}, {len(data) / 1024:.1f} KB",
            File(data=data, format="pdf", name=name),
        ]

    @mcp.tool(output_schema=None)
    async def pdf_split(
        pages_per_file: Annotated[int, Field(
            description="Pages per output file. 1 means one file per page")] = 1,
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> list:
        """Split a PDF into several files and return all of them.

        Note: a PDF with many pages produces many files. Check the page count
        with inspect_document first and confirm the split with the user.
        """
        source = resolve_artifact_input(file_content, file_name, mime_type)
        if source.fmt != "pdf":
            return [f"This is not a PDF (detected {source.fmt})."]

        chunks = await pdfops.split_pdf(source.data, pages_per_file)
        if len(chunks) > 50:
            return [
                f"This would produce {len(chunks)} files, which is too many. "
                f"Increase pages_per_file, or use pdf_extract_pages to take only the "
                f"pages you need."
            ]

        result: list[Any] = [f"Split into {len(chunks)} files"]
        for label, blob in chunks:
            result.append(File(data=blob, format="pdf", name=f"{source.name}_{label}"))
        return result

    # ------------------------------------------------------------------
    # pdf_merge is deliberately not exposed as an MCP tool
    #
    # The host's file-injection mechanism can inject only **one** file per
    # call. This is a structural limit, not a difference in convention. The
    # injection logic looks roughly like this:
    #
    #   for key, value in list(tool_args.items()):
    #       if not isinstance(value, str):     # <- list parameters are skipped
    #           continue
    #       ...
    #       tool_args["file_content"] = b64            # <- overwritten, last one wins
    #       tool_args.setdefault("file_name", key)     # <- first one wins
    #
    # Two consequences:
    #   1. No List[str] parameter is ever filled, so a merge tool never receives
    #      its files
    #   2. Even if the model puts two upload references into two different string
    #      parameters, the result is file_content from B and file_name from A,
    #      mismatched, with no warning
    #
    # The merge logic itself is implemented and tested
    # (src/domain/convert/pdfops.merge_pdfs). Once the host supports multi-file
    # injection, a thin wrapper here is all that is needed. Until then it stays
    # unregistered: a tool that structurally cannot receive its input only makes
    # the model retry and report failure.
    # ------------------------------------------------------------------

    @mcp.tool(output_schema=None)
    async def pdf_rotate(
        degrees: Annotated[int, Field(
            description="Rotation angle, must be a multiple of 90 (90/180/270)")],
        pages: Annotated[str, Field(
            description="Page range to rotate, e.g. '1-3'. Empty means all pages")] = "",
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> list:
        """Rotate the given pages of a PDF (commonly used to fix wrongly oriented scans)."""
        source = resolve_artifact_input(file_content, file_name, mime_type)
        if source.fmt != "pdf":
            return [f"This is not a PDF (detected {source.fmt})."]

        data = await pdfops.rotate_pages(source.data, degrees, pages)
        scope = pages or "all pages"
        return [
            f"Rotated {scope} by {degrees} degrees",
            File(data=data, format="pdf", name=f"{source.name}_rotated"),
        ]

    @mcp.tool(output_schema=None)
    async def pdf_protect(
        password: Annotated[str, Field(description="Password required to open the PDF")],
        file_content: _FILE_CONTENT = None,
        file_name: _FILE_NAME = None,
        mime_type: _MIME_TYPE = None,
    ) -> list:
        """Add an open password to a PDF (AES-256 encryption).

        The password appears in the conversation history; remind the user not to
        reuse a password from another system.
        """
        source = resolve_artifact_input(file_content, file_name, mime_type)
        if source.fmt != "pdf":
            return [f"This is not a PDF (detected {source.fmt})."]

        data = await pdfops.encrypt_pdf(source.data, password)
        return [
            "Encrypted. The open password is the one the user specified; remind them "
            "to keep it safe (it now appears in the conversation, so it is best not "
            "to reuse a password from another system).",
            File(data=data, format="pdf", name=f"{source.name}_protected"),
        ]

    logger.info("AnyDoc MCP tools registered")
