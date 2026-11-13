"""Behavioral tests for the engine layer.

The focus is on "no exception, but the output is broken" failures; they are the
hardest to notice because every layer looks successful. So these tests check the
**structure** of the output, not its size or presence.

Tests needing an external binary skip when it is missing, so a clean CI runner
does not produce false failures.
"""

import pytest

from src.domain.convert.engines.libreoffice import (
    _COMPONENT_FOR_FORMAT,
    LibreOfficeEngine,
)
from src.domain.convert.engines.pandoc import _NEEDS_STANDALONE, PandocEngine
from src.domain.convert.engines.text import TextEngine


@pytest.fixture
def pandoc():
    engine = PandocEngine()
    if not engine.is_available():
        pytest.skip("pandoc not installed")
    return engine


class TestPandocStandalone:
    """A missing --standalone produces a "fragment" instead of a complete file.

    These tests come from a full-route sweep: md -> rtf had a normal byte count
    and no exception, but the output was an RTF fragment starting with `{\\pard`,
    missing the `{\\rtf1\\ansi` header and font table. LibreOffice is lenient and
    opens it; Microsoft Word does not recognize it.
    """

    def test_rtf_has_proper_header(self, pandoc):
        async def run():
            return await pandoc.convert(b"# %s\n\n%s\n" % (b"Title", b"body"), "md", "rtf")

        import asyncio
        data = asyncio.run(run())
        assert data.startswith(b"{\\rtf"), (
            f"RTF header missing, starts with {data[:12]!r}; a sign of missing --standalone"
        )

    def test_html_has_head_and_charset(self, pandoc):
        import asyncio
        # CJK content on purpose: the charset declaration is what keeps it readable
        data = asyncio.run(pandoc.convert("# 標題\n\n中文內容\n".encode(), "md", "html"))
        text = data.decode("utf-8")
        assert "<head>" in text and "charset" in text, "HTML lacks <head> / charset declaration"

    def test_standalone_set_covers_all_container_formats(self):
        """Every format handed to the user as a "file" must be in the standalone set.

        Plain-text targets (md / txt / rst) do not need it; they work as fragments.
        """
        container_formats = {"html", "docx", "odt", "epub", "rtf", "pptx"}
        assert container_formats <= _NEEDS_STANDALONE


class TestLibreOfficeComponents:
    """Having soffice does not mean every component is present.

    Debian splits writer / calc / impress into separate packages. With only
    writer installed the soffice binary exists and is_available() returns True,
    yet every spreadsheet conversion fails. A capability claim wrong at this
    granularity gives users "says supported, does not convert".
    """

    def test_component_map_covers_every_office_format(self):
        for fmt in ["docx", "doc", "odt", "rtf", "txt", "html",
                    "xlsx", "xls", "ods", "csv", "pptx", "ppt", "odp"]:
            assert fmt in _COMPONENT_FOR_FORMAT, f"{fmt} is not mapped to a LibreOffice component"

    def test_pdf_needs_no_specific_component(self):
        """pdf is output-only; any component can produce it, so it must not be bound to one."""
        assert "pdf" not in _COMPONENT_FOR_FORMAT

    def test_can_handle_requires_both_sides(self, monkeypatch):
        engine = LibreOfficeEngine()
        monkeypatch.setattr(engine, "is_available", lambda: True)
        monkeypatch.setattr(
            engine, "has_component", lambda c: c == "swriter"
        )
        assert engine.can_handle("docx", "pdf") is True      # only needs writer
        assert engine.can_handle("xlsx", "pdf") is False     # needs calc
        assert engine.can_handle("pptx", "pdf") is False     # needs impress
        assert engine.can_handle("docx", "odt") is True

    def test_unavailable_binary_blocks_everything(self, monkeypatch):
        engine = LibreOfficeEngine()
        monkeypatch.setattr(engine, "is_available", lambda: False)
        assert engine.can_handle("docx", "pdf") is False


class TestTextEngine:
    def test_tsv_becomes_markdown_table(self):
        import asyncio
        # CJK cells on purpose: the table must survive non-ASCII content
        data = "部門\tQ1\n業務\t120\n".encode()
        out = asyncio.run(TextEngine().convert(data, "tsv", "md")).decode()
        assert "| 部門 | Q1 |" in out
        assert "| --- | --- |" in out

    def test_pipe_in_cell_is_escaped(self):
        """An unescaped | inside a cell breaks the whole table."""
        import asyncio
        out = asyncio.run(TextEngine().convert(b"a\tb|c\nd\te\n", "tsv", "md")).decode()
        assert "b\\|c" in out

    def test_ragged_rows_are_padded(self):
        """Ragged rows break the Markdown table; pad them to the header width."""
        import asyncio
        out = asyncio.run(TextEngine().convert(b"a\tb\tc\nd\n", "tsv", "md")).decode()
        rows = [line for line in out.splitlines() if line.startswith("|")]
        assert all(line.count("|") == 4 for line in rows), out

    def test_json_is_wrapped_in_code_fence(self):
        import asyncio
        out = asyncio.run(TextEngine().convert(b'{"a": 1}', "json", "md")).decode()
        assert out.startswith("```json") and out.rstrip().endswith("```")


class TestPdfImagePreservation:
    """Converting PDF to an editable format must carry the embedded images along.

    A real user report: after PDF -> DOCX "the images are gone". The route is
    pdf -> md -> docx, and the intermediate Markdown carried only text, so the
    images simply vanished.

    The fix extracts the images and embeds them as data: URIs in the Markdown.
    Writing files and referencing them does not work: hops pass bytes, not a
    directory, so pandoc on the next hop cannot find the files.
    """

    def test_image_carrying_formats_exclude_plain_text(self):
        """Plain-text formats cannot express images; stuffing base64 in only makes garbage."""
        from src.domain.convert.service import IMAGE_CARRYING_FORMATS

        for fmt in ("txt", "csv", "tsv", "json", "xml"):
            assert fmt not in IMAGE_CARRYING_FORMATS
        for fmt in ("docx", "odt", "html", "epub", "pdf"):
            assert fmt in IMAGE_CARRYING_FORMATS

    def test_tiny_decorative_images_are_skipped(self):
        """PDFs carry many tiny decorative images (rules, icons); embedding them all is bloat."""
        from src.domain.convert.pdfops import MIN_IMAGE_BYTES

        assert MIN_IMAGE_BYTES >= 1024

    def test_extraction_failure_does_not_abort_conversion(self, monkeypatch):
        """Image extraction failing must still deliver the text; text alone is valuable output."""
        import asyncio

        from src.domain.convert import pdfops
        from src.domain.convert.engines import firecrawl_engine

        async def boom(_data):
            raise RuntimeError("corrupt image object")

        monkeypatch.setattr(pdfops, "extract_images", boom)
        assert asyncio.run(firecrawl_engine._pdf_images_markdown(b"%PDF-fake")) == ""

    def test_images_are_embedded_as_data_uris(self, monkeypatch):
        """Output must be data: URIs, never file path references."""
        import asyncio

        from src.domain.convert import pdfops
        from src.domain.convert.engines import firecrawl_engine

        async def fake(_data):
            return [(3, "Im1.png", b"\x89PNG" + b"x" * 3000)]

        monkeypatch.setattr(pdfops, "extract_images", fake)
        md = asyncio.run(firecrawl_engine._pdf_images_markdown(b"%PDF-fake"))
        assert "data:image/png;base64," in md
        label = md.split("![]")[0]
        assert "3" in label, "the image must be labeled with its page number"
        assert "![]" in md, "alt text must stay empty, or the internal image id becomes a caption"

    def test_images_shared_across_pages_are_deduplicated(self):
        """PDF image XObjects can be shared across pages; without dedup they embed repeatedly.

        Observed: a 4-page LibreOffice document with 4 images, yet page.images
        reported all 4 on every page; without dedup that is 16 images and a
        docx four times the size. A header logo repeated on every page is the
        same problem.
        """
        import asyncio
        from unittest.mock import patch

        from src.domain.convert import pdfops

        blob_a = b"\x89PNG" + b"a" * 5000
        blob_b = b"\x89PNG" + b"b" * 5000

        class FakeImage:
            def __init__(self, name, data):
                self.name, self.data = name, data

        class FakePage:
            images = [FakeImage("Im1.png", blob_a), FakeImage("Im2.png", blob_b)]

        class FakeReader:
            def __init__(self, _stream):
                self.pages = [FakePage(), FakePage(), FakePage()]

        with patch.object(pdfops, "_require_pypdf", lambda: (FakeReader, None)):
            result = asyncio.run(pdfops.extract_images(b"%PDF-fake"))

        assert len(result) == 2, f"expected 2 images after dedup, got {len(result)}"
        assert [r[0] for r in result] == [1, 1], "dedup must keep the page of first occurrence"

    def test_office_sources_use_firecrawl_assets_not_pypdf(self, monkeypatch):
        """Images in docx / pptx / epub must come from firecrawl's document model.

        to_markdown() drops images entirely (keeping only the alt text as a
        paragraph), so assets must be fetched via to_document() separately. The
        pypdf route only applies to PDF; firecrawl refuses to_document for PDF.
        """
        import asyncio

        from src.domain.convert.engines import firecrawl_engine

        called = {}

        def fake_assets(data, fmt):
            called["fmt"] = fmt
            return [(1, "image/png", b"\x89PNG" + b"z" * 4000)]

        monkeypatch.setattr(firecrawl_engine, "_extract_asset_images", fake_assets)
        md = asyncio.run(
            firecrawl_engine.FirecrawlEngine()._asset_images_markdown(b"fake", "pptx")
        )
        assert "data:image/png;base64," in md
        assert called["fmt"] == "pptx"

    def test_asset_extraction_failure_keeps_the_text(self, monkeypatch):
        """Failing to fetch images is a bonus failing; the extracted text must not die with it."""
        import asyncio

        from src.domain.convert.engines import firecrawl_engine

        def boom(data, fmt):
            raise RuntimeError("model unavailable")

        monkeypatch.setattr(firecrawl_engine, "_extract_asset_images", boom)
        assert asyncio.run(
            firecrawl_engine.FirecrawlEngine()._asset_images_markdown(b"fake", "docx")
        ) == ""


class TestLossyConversionIsAllowedByDefault:
    """Lossy conversions are allowed by default, with a warning rather than a refusal.

    The default used to refuse any lossy path. Real usage overturned that: after
    "PDF to Word" was refused, the AI went pdf -> md, then guessed parameters 8
    times to get md into docx, and 2.5 minutes later had exactly the result the
    direct conversion would have given. The gate did not prevent a bad result;
    it only sent the same result on a long detour.
    """

    def test_warning_does_not_claim_images_are_lost(self):
        """Images are preserved now, so the warning must say so.

        The old warning said "images have been lost", which stopped being true
        once image extraction was added. The AI relays it verbatim, and the user
        assumes the images are gone instead of looking at the end of the
        document. A wrong warning misleads more than none.
        """
        from src.domain.convert.registry import Route
        from src.fastmcp_tools.morphyard_tools import _fidelity_warning

        plan = [Route("pdf", "md", "firecrawl", "lossy"),
                Route("md", "docx", "pandoc", "structural")]
        warning = _fidelity_warning(plan)
        assert "image" in warning.lower()
        assert "lost" not in warning.lower(), "the old wrong wording must not come back"

    def test_high_fidelity_path_gets_no_warning(self):
        """docx -> pdf keeps the layout; do not scare the user for nothing."""
        from src.domain.convert.registry import Route
        from src.fastmcp_tools.morphyard_tools import _fidelity_warning

        assert _fidelity_warning([Route("docx", "pdf", "libreoffice", "high")]) == ""


class TestPandocSandbox:
    """Documents are untrusted, and pandoc without --sandbox reads whatever they point at.

    A .tex upload holding \\lstinputlisting{/path} came back with that file's
    contents (as root: .env included), and an <img> pointing at a local path is
    pulled into a .docx. --sandbox limits pandoc to the input file it is given.
    """

    @pytest.mark.parametrize("src, dst", [("md", "docx"), ("tex", "md"), ("html", "docx"), ("md", "pptx")])
    async def test_the_read_step_runs_sandboxed(self, monkeypatch, src, dst):
        seen = {}

        async def fake_run(self, argv, cwd, src, dst, timeout=None):
            seen["argv"] = argv
            raise RuntimeError("stop after the command is built")

        monkeypatch.setattr(PandocEngine, "_run_command", fake_run)
        with pytest.raises(RuntimeError):
            await PandocEngine().convert(b"x", src, dst)
        assert "--sandbox" in seen["argv"]

    async def test_tex_cannot_include_a_file_from_the_server(self, pandoc, tmp_path):
        secret = tmp_path / "secret.env"
        secret.write_text("API_KEY=do-not-leak\n")
        tex = (
            "\\documentclass{article}\\begin{document}\n"
            f"\\lstinputlisting{{{secret}}}\n\\input{{{secret}}}\n"
            "\\end{document}\n"
        )
        out = await pandoc.convert(tex.encode(), "tex", "md")
        assert b"do-not-leak" not in out

    async def test_html_image_from_a_local_path_is_not_embedded(self, pandoc, tmp_path):
        import io
        import zipfile

        from PIL import Image

        image = tmp_path / "private.png"
        Image.new("RGB", (4, 4), "red").save(image)
        html = f'<html><body><p>x</p><img src="{image}"></body></html>'
        out = await pandoc.convert(html.encode(), "html", "docx")
        names = zipfile.ZipFile(io.BytesIO(out)).namelist()
        assert not [n for n in names if n.startswith("word/media/")]

    async def test_data_uri_images_are_still_embedded(self, pandoc):
        """The PDF -> DOCX route carries images as data: URIs; the sandbox must keep them."""
        import base64
        import io
        import zipfile

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (4, 4), "blue").save(buf, format="PNG")
        md = f"# Figure\n\n![chart](data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()})\n"
        out = await pandoc.convert(md.encode(), "md", "docx")
        names = zipfile.ZipFile(io.BytesIO(out)).namelist()
        assert [n for n in names if n.startswith("word/media/")]


def _png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "green").save(buf, format="PNG")
    return buf.getvalue()


def _media(archive: bytes) -> list[str]:
    import io
    import zipfile

    return [n for n in zipfile.ZipFile(io.BytesIO(archive)).namelist()
            if n.lower().endswith((".png", ".jpg", ".jpeg", ".gif"))]


class TestPandocWriterOpensNothing:
    """What reaches a writer must not name a server file: images, metadata, raw HTML."""

    async def test_latex_includegraphics_of_a_server_file_is_dropped(self, pandoc, tmp_path):
        image = tmp_path / "private.png"
        image.write_bytes(_png())
        tex = f"\\documentclass{{article}}\\begin{{document}}\n\\includegraphics{{{image}}}\n\\end{{document}}\n"
        assert _media(await pandoc.convert(tex.encode(), "tex", "docx")) == []

    async def test_epub_cover_image_metadata_is_dropped(self, pandoc, tmp_path):
        image = tmp_path / "private.png"
        image.write_bytes(_png())
        md = f"---\ntitle: Book\ncover-image: {image}\n---\n\n# One\n\ntext\n"
        out = await pandoc.convert(md.encode(), "md", "epub")
        assert _media(out) == []
        assert b"Book" in _epub_text(out)

    async def test_raw_html_image_is_not_embedded_in_epub(self, pandoc, tmp_path):
        image = tmp_path / "private.png"
        image.write_bytes(_png())
        md = f'# One\n\n<img src="{image}">\n\ntext\n'
        assert _media(await pandoc.convert(md.encode(), "md", "epub")) == []

    @pytest.mark.parametrize("dst", ["epub", "odt", "docx"])
    async def test_images_embedded_in_a_docx_are_kept(self, pandoc, dst):
        import base64

        md = f"# Figure\n\n![chart](data:image/png;base64,{base64.b64encode(_png()).decode()})\n"
        docx = await pandoc.convert(md.encode(), "md", "docx")
        assert _media(docx)
        assert _media(await pandoc.convert(docx, "docx", dst))

    async def test_text_targets_are_unchanged(self, pandoc):
        md = "---\ntitle: T\ncss: style.css\n---\n\n![remote](https://example.com/a.png)\n"
        out = await pandoc.convert(md.encode(), "md", "md")
        assert b"https://example.com/a.png" in out


def _epub_text(archive: bytes) -> bytes:
    import io
    import zipfile

    z = zipfile.ZipFile(io.BytesIO(archive))
    return b"".join(z.read(n) for n in z.namelist() if n.endswith((".xhtml", ".opf")))


class TestWithoutFileReferences:
    """The AST filter on its own: what stays, what goes."""

    @staticmethod
    def image(url: str) -> dict:
        return {"t": "Image", "c": [["", [], []], [{"t": "Str", "c": "alt"}], [url, ""]]}

    def run(self, tmp_path, blocks, meta=None, epub=False):
        from src.domain.convert.engines.pandoc import _without_file_references

        media = tmp_path / "media"
        (media / "media").mkdir(parents=True, exist_ok=True)
        (media / "media" / "rId1.png").write_bytes(_png())
        (tmp_path / "input.docx").write_bytes(b"not an image")
        ast = {"pandoc-api-version": [1, 23], "meta": meta or {}, "blocks": blocks}
        return _without_file_references(ast, media, epub=epub)

    def para(self, *inlines):
        return [{"t": "Para", "c": list(inlines)}]

    @pytest.mark.parametrize("url", [
        "/etc/hostname", "media/../input.docx", "../secret.env", "https://10.0.0.1/a.png",
        "file:///etc/hostname", "\\\\host\\share\\a.png", "media/media/missing.png",
    ])
    def test_an_image_naming_anything_else_becomes_its_alt_text(self, tmp_path, url):
        out = self.run(tmp_path, self.para(self.image(url)))
        assert out["blocks"][0]["c"][0] == {"t": "Span", "c": [["", [], []], [{"t": "Str", "c": "alt"}]]}

    def test_extracted_media_is_inlined(self, tmp_path):
        out = self.run(tmp_path, self.para(self.image("media/media/rId1.png")))
        assert out["blocks"][0]["c"][0]["c"][2][0].startswith("data:image/png;base64,")

    def test_data_uri_is_kept(self, tmp_path):
        out = self.run(tmp_path, self.para(self.image("data:image/png;base64,AAAA")))
        assert out["blocks"][0]["c"][0]["c"][2][0] == "data:image/png;base64,AAAA"

    def test_only_safe_metadata_is_kept_and_cleaned(self, tmp_path):
        meta = {
            "title": {"t": "MetaInlines", "c": [self.image("/etc/hostname")]},
            "cover-image": {"t": "MetaString", "c": "/etc/hostname"},
            "css": {"t": "MetaString", "c": "/etc/hostname"},
        }
        out = self.run(tmp_path, [], meta=meta)
        assert set(out["meta"]) == {"title"}
        assert out["meta"]["title"]["c"][0]["t"] == "Span"

    def test_raw_html_goes_only_for_epub(self, tmp_path):
        raw = {"t": "RawBlock", "c": ["html", '<img src="/etc/hostname">']}
        assert self.run(tmp_path, [raw], epub=True)["blocks"] == []
        assert self.run(tmp_path, [raw])["blocks"] == [raw]

    def test_nulls_in_the_ast_survive(self, tmp_path):
        caption = {"t": "Figure", "c": [["", [], []], [None, []], []]}
        assert self.run(tmp_path, [caption])["blocks"] == [caption]
