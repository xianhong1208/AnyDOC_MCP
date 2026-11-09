"""Pandoc engine: semantic conversion between text-based formats

Pandoc's model is "source -> abstract syntax tree -> target", so what it
preserves is **structure** (heading hierarchy, lists, tables, footnotes), not
layout. For visual fidelity use LibreOffice.

-> pdf is deliberately unsupported: Pandoc needs a full LaTeX toolchain to
produce PDF (texlive is about 2GB, and CJK text needs a separate engine on
top). The same request works as md -> docx -> pdf in two hops using the
already-installed LibreOffice, saving 2GB in the image.
"""

import base64
import json
import mimetypes
from pathlib import Path

from src.domain.convert.engines import base
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


# Documents are untrusted, and pandoc reads whatever they point at: a LaTeX
# \input or \lstinputlisting pulls a file from the server into the output (the
# service runs as root, so .env included), and writers that embed images open
# any path or URL an image names. So a conversion runs in two steps:
#
#   1. read with --sandbox into pandoc's JSON AST: readers may open nothing but
#      the input file. (The whole conversion cannot run sandboxed: Debian's pandoc
#      reads its docx/pptx/epub/LaTeX templates from disk, and --sandbox blocks
#      those reads too.)
#   2. write from that AST, after removing everything a writer would open.
#
# Writers that open the images they embed. The others write an image's address
# as text and open nothing.
_EMBEDDING_WRITERS = {"docx", "odt", "epub", "pptx", "rtf"}

# Where step 1 puts images embedded in the source (docx, odt, epub, data: URIs).
_MEDIA_DIR = "media"

# Metadata a writer may use; everything else is dropped before writing. The EPUB
# writer opens the files `cover-image`, `css` and `stylesheet` name.
_SAFE_METADATA = {
    "title", "subtitle", "author", "date", "lang", "dir", "abstract",
    "keywords", "subject", "description", "institute", "toc-title",
}


def _without_file_references(ast: dict, media_dir: Path, *, epub: bool) -> dict:
    """The AST with nothing left in it that makes a writer open a file or URL.

    An image keeps its picture only when it is a data: URI or one of the files
    step 1 extracted (inlined as a data: URI); any other image becomes its alt
    text. For EPUB, raw HTML goes too: that writer embeds the files <img>,
    <video>, <audio> and <source> tags in it name.
    """
    media_root = media_dir.resolve()
    drop = object()  # not None: the AST has meaningful nulls (a table's short caption)

    def inline(url: str) -> str | None:
        if url.startswith("data:"):
            return url
        # One rule for everything else: it must resolve inside media_dir. That also
        # turns away absolute paths, ../ and URLs, which never resolve there.
        path = (media_dir.parent / url).resolve()
        if not path.is_relative_to(media_root) or not path.is_file():
            return None
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"

    def clean(node):
        if isinstance(node, list):
            return [kept for kept in (clean(n) for n in node) if kept is not drop]
        if not isinstance(node, dict):
            return node
        kind = node.get("t")
        if kind == "Image":
            attr, alt, (url, title) = node["c"]
            safe = inline(url)
            if safe is None:
                return {"t": "Span", "c": [attr, clean(alt)]}
            return {"t": "Image", "c": [attr, clean(alt), [safe, title]]}
        if epub and kind in ("RawBlock", "RawInline") and node["c"][0].startswith("html"):
            return drop
        return {key: clean(value) for key, value in node.items()}

    meta = {key: value for key, value in ast.get("meta", {}).items() if key in _SAFE_METADATA}
    return {**ast, "meta": clean(meta), "blocks": clean(ast.get("blocks", []))}


class PandocEngine(CliEngine):
    """pandoc -f <reader> -t <writer>"""

    name = "pandoc"
    binary = "pandoc"

    async def convert(self, data: bytes, src: str, dst: str, **opts) -> bytes:
        reader = _PANDOC_READER.get(src)
        writer = _PANDOC_WRITER.get(dst)
        if not reader or not writer:
            raise ConversionFailedError(
                self.name, src, dst, f"pandoc cannot map {src} → {dst}"
            )

        timeout = base.resolve_timeout(opts, 120)
        standalone = opts.get("standalone", True)

        with Workspace() as ws:
            ws.write(f"input.{src}", data)
            # Read, sandboxed, into pandoc's AST; images embedded in the source are
            # written under media/.
            await self._run_command(
                [self.resolved_binary(), "--sandbox", f"--extract-media={_MEDIA_DIR}",
                 "-f", reader, "-t", "json", "-o", "ast.json", f"input.{src}"],
                cwd=ws.path, src=src, dst=dst, timeout=timeout,
            )
            ast = json.loads(ws.read("ast.json"))
            if dst in _EMBEDDING_WRITERS:
                ast = _without_file_references(ast, ws.path / _MEDIA_DIR, epub=dst == "epub")
            ws.write("safe.json", json.dumps(ast).encode())

            argv = [self.resolved_binary(), "-f", "json", "-t", writer]
            if standalone and dst in _NEEDS_STANDALONE:
                argv.append("--standalone")
            if dst == "pptx":
                # Slides split on h1/h2. A source without a heading hierarchy
                # ends up as a single slide; that is a content problem, not a
                # conversion problem, and is not patched here.
                argv += ["--slide-level", str(opts.get("slide_level", 2))]
            if dst == "docx" and opts.get("toc"):
                argv.append("--toc")
            argv += ["-o", f"output.{dst}", "safe.json"]

            await self._run_command(argv, cwd=ws.path, src=src, dst=dst, timeout=timeout)
            result = ws.read(f"output.{dst}")

        logger.info(f"[pandoc] {src} → {dst}: {len(data)} → {len(result)} bytes")
        return result
