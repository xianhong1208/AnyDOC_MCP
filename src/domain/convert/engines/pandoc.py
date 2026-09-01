"""Pandoc engine: semantic conversion between text-based formats

Pandoc's model is "source -> abstract syntax tree -> target", so what it
preserves is **structure** (heading hierarchy, lists, tables, footnotes), not
layout. For visual fidelity use LibreOffice.

-> pdf is deliberately unsupported: Pandoc needs a full LaTeX toolchain to
produce PDF (texlive is about 2GB, and CJK text needs a separate engine on
top). The same request works as md -> docx -> pdf in two hops using the
already-installed LibreOffice, saving 2GB in the image.
"""

from src.domain.convert.engines.base import CliEngine, Workspace
from src.domain.convert.types import ConversionFailedError
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

# Internal format code -> pandoc format name (they do not fully coincide)
_PANDOC_READER = {
    "md": "markdown",
    "html": "html",
    "docx": "docx",
    "odt": "odt",
    "epub": "epub",
    "rst": "rst",
    "tex": "latex",
    "txt": "markdown",       # read plain text as markdown; at least paragraphs come out right
    "rtf": "rtf",
    "csv": "csv",
    "json": "json",
}

_PANDOC_WRITER = {
    "md": "gfm",             # GitHub Flavored Markdown: best table support
    "html": "html5",
    "docx": "docx",
    "odt": "odt",
    "epub": "epub3",
    "rst": "rst",
    "tex": "latex",
    "txt": "plain",
    "rtf": "rtf",
    "pptx": "pptx",          # every h1/h2 becomes a slide
}


# Target formats that come out as a "fragment" rather than a complete file
# without --standalone.
#
# This set must be complete: one omission hands the user a file that will not
# open, with no error message at all:
#   html  missing <head> and charset declaration, browsers render non-ASCII
#         text with the wrong encoding
#   rtf   missing the {\rtf1\ansi header and font table, the file starts with
#         {\pard. LibreOffice is lenient and opens it, but Microsoft Word does
#         not recognize it, so the user gets a broken file
#
# The rtf case only surfaced while exercising all 162 routes: byte count looked
# normal, no exception, and only a magic-bytes check revealed it. Route sweeps
# must therefore validate structure, not just size.
_NEEDS_STANDALONE = {"html", "docx", "odt", "epub", "tex", "pptx", "rtf"}


class PandocEngine(CliEngine):
    """pandoc -f <reader> -t <writer>"""

    name = "pandoc"
    binary = "pandoc"

    @classmethod
    def supports(cls, src: str, dst: str) -> bool:
        return src in _PANDOC_READER and dst in _PANDOC_WRITER

    async def convert(self, data: bytes, src: str, dst: str, **opts) -> bytes:
        reader = _PANDOC_READER.get(src)
        writer = _PANDOC_WRITER.get(dst)
        if not reader or not writer:
            raise ConversionFailedError(
                self.name, src, dst, f"pandoc cannot map {src} → {dst}"
            )

        timeout = int(opts.get("timeout", 120))
        standalone = opts.get("standalone", True)

        with Workspace() as ws:
            ws.write(f"input.{src}", data)
            argv = [self.resolved_binary(), "-f", reader, "-t", writer]
            if standalone and dst in _NEEDS_STANDALONE:
                argv.append("--standalone")
            if dst == "pptx":
                # Slides split on h1/h2. A source without a heading hierarchy
                # ends up as a single slide; that is a content problem, not a
                # conversion problem, and is not patched here.
                argv += ["--slide-level", str(opts.get("slide_level", 2))]
            if dst == "docx" and opts.get("toc"):
                argv.append("--toc")
            argv += ["-o", f"output.{dst}", f"input.{src}"]

            await self._run(argv, cwd=ws.path, src=src, dst=dst, timeout=timeout)
            result = ws.read(f"output.{dst}")

        logger.info(f"[pandoc] {src} → {dst}: {len(data)} → {len(result)} bytes")
        return result
