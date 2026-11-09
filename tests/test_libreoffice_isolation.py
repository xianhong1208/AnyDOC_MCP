"""LibreOffice must not read files or fetch URLs a document points at.

A QA run found that an HTML image naming /usr/share/... or http://127.0.0.1:...
was read or fetched and embedded in the PDF, and that a DOCX image linked to a
file:/// URL was too. Two layers stop it: the profile every conversion runs with
(links from untrusted documents blocked, network through an unreachable proxy),
and stripping outside references from the document first, for the importers that
ignore the block (ODF in Calc and Impress, RTF fields).

The first half tests the stripping on its own; the second runs real conversions
and skips when LibreOffice is not installed.
"""

import asyncio
import http.server
import io
import threading
import zipfile
from pathlib import Path

import pytest

from src.domain.convert.engines import libreoffice
from src.domain.convert.engines.external_refs import (
    UncheckableDocumentError,
    strip_external_references,
)
from src.domain.convert.engines.libreoffice import LibreOfficeEngine
from src.domain.convert.types import ConversionFailedError

ODF_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:form="urn:oasis:names:tc:opendocument:xmlns:form:1.0" '
    'xmlns:of="urn:oasis:names:tc:opendocument:xmlns:of:1.2" office:version="1.3"'
)
MIMETYPES = {
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
}


def _png(width: int, height: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def _zip(files: dict[str, bytes | str], mimetype: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if mimetype:
            zf.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        for name, data in files.items():
            zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def _odf(fmt: str, body: str, extra: dict | None = None) -> bytes:
    kind = {"odt": "text", "ods": "spreadsheet", "odp": "presentation"}[fmt]
    content = (f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {ODF_NS}>'
               f'<office:body><office:{kind}>{body}</office:{kind}></office:body></office:document-content>')
    manifest = ('<?xml version="1.0" encoding="UTF-8"?><manifest:manifest '
                'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">'
                f'<manifest:file-entry manifest:full-path="/" manifest:media-type="{MIMETYPES[fmt]}"/>'
                '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
                + "".join(f'<manifest:file-entry manifest:full-path="{name}" manifest:media-type="image/png"/>'
                          for name in (extra or {}) if name.endswith(".png"))
                + '</manifest:manifest>')
    return _zip({"META-INF/manifest.xml": manifest, "content.xml": content, **(extra or {})}, MIMETYPES[fmt])


def _image(href: str, y: int = 1) -> str:
    return (f'<draw:frame draw:name="f{y}" text:anchor-type="as-char" svg:x="1cm" svg:y="{y}cm" '
            f'svg:width="4cm" svg:height="2cm"><draw:image xlink:href="{href}" xlink:type="simple" '
            'xlink:show="embed" xlink:actuate="onLoad"/></draw:frame>')


def _slide(*frames: str) -> str:
    return f'<draw:page draw:name="s1">{"".join(frames)}</draw:page>'


def _content(package: bytes) -> str:
    return zipfile.ZipFile(io.BytesIO(package)).read("content.xml").decode()


CT_DOCX = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
           'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
           'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" '
           'ContentType="image/png"/><Override PartName="/word/document.xml" ContentType="application/vnd.'
           'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
ROOT_RELS = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/'
             'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
             'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
HYPERLINK_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"


def _blip(rid: str, attr: str, n: int) -> str:
    return (
        '<w:r><w:drawing><wp:inline><wp:extent cx="900000" cy="400000"/>'
        f'<wp:docPr id="{n}" name="p{n}"/><a:graphic><a:graphicData '
        'uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr>'
        f'<pic:cNvPr id="{n}" name="p{n}"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill>'
        f'<a:blip {attr}="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm>'
        '<a:off x="0" y="0"/><a:ext cx="900000" cy="400000"/></a:xfrm><a:prstGeom prst="rect"/></pic:spPr>'
        '</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>')


def _docx(rels: list[tuple[str, str, str, bool]], body: str, media: dict | None = None) -> bytes:
    ns = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
          'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
          'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')
    rel_xml = "".join(
        f'<Relationship Id="{rid}" Type="{typ}" Target="{target}"{" TargetMode=\"External\"" if ext else ""}/>'
        for rid, typ, target, ext in rels)
    return _zip({
        "[Content_Types].xml": CT_DOCX, "_rels/.rels": ROOT_RELS,
        "word/document.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:document {ns}><w:body>{body}</w:body></w:document>',
        "word/_rels/document.xml.rels": ('<?xml version="1.0" encoding="UTF-8"?><Relationships '
                                         f'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rel_xml}'
                                         '</Relationships>'),
        **(media or {}),
    })


def _rtf(field: str) -> bytes:
    return ("{\\rtf1\\ansi\\deff0 {\\fonttbl{\\f0 Arial;}}\\pard before\\par " + field + "\\par after\\par}").encode()


# ---------------------------------------------------------------------------
# Stripping, without LibreOffice
# ---------------------------------------------------------------------------
class TestOdfPackages:

    @pytest.mark.parametrize("href", [
        "file:///usr/share/pixmaps/debian-logo.png",
        "/usr/share/pixmaps/debian-logo.png",
        "../../../../usr/share/pixmaps/debian-logo.png",
        "Pictures/../../../etc/x.png",
        "http://127.0.0.1:4599/favicon.svg",
        "https://example.com/a.png",
        "smb://host/share/a.png",
        "C:\\\\Windows\\\\a.png",
        "%2Fetc%2Fx.png",
        "Pictures/not-in-the-package.png",
    ])
    def test_a_reference_outside_the_package_is_removed(self, href):
        cleaned = strip_external_references(_odf("odp", _slide(_image(href))))
        assert "xlink:href" not in _content(cleaned)
        assert "<draw:image" in _content(cleaned)

    def test_images_stored_in_the_package_are_kept(self):
        doc = _odf("odp", _slide(_image("Pictures/a.png"), _image("./Pictures/b.png", 4)),
                   {"Pictures/a.png": _png(4, 4), "Pictures/b.png": _png(4, 4)})
        assert strip_external_references(doc) is doc

    def test_a_package_entry_named_with_dotdot_does_not_make_a_path_inside(self):
        """Zip entry names are free text: an entry called ../../etc/x.png would match the
        href, but LibreOffice resolves that href against the document's folder on disk."""
        href = "../../../../usr/share/pixmaps/debian-logo.png"
        doc = _odf("odp", _slide(_image(href)), {href: b"x"})
        assert "xlink:href" not in _content(strip_external_references(doc))

    def test_embedded_objects_and_anchors_are_kept(self):
        body = ('<text:p><draw:frame><draw:object xlink:href="./Object 1"/></draw:frame>'
                '<text:a xlink:href="#Sheet1">anchor</text:a></text:p>')
        doc = _odf("odt", body, {"Object 1/content.xml": "<x/>"})
        assert strip_external_references(doc) is doc

    def test_hyperlinks_keep_their_targets(self):
        body = ('<text:p><text:a xlink:href="https://example.com/doc">link</text:a>'
                '<draw:a xlink:href="file:///somewhere">frame link</draw:a></text:p>')
        doc = _odf("odt", body)
        assert strip_external_references(doc) is doc

    def test_the_xlink_namespace_is_matched_whatever_its_prefix(self):
        body = ('<text:p><draw:frame><draw:image xmlns:q="http://www.w3.org/1999/xlink" '
                'q:href="file:///usr/share/pixmaps/debian-logo.png"/></draw:frame></text:p>')
        assert "q:href" not in _content(strip_external_references(_odf("odt", body)))

    def test_an_href_in_another_namespace_is_not_mistaken_for_one(self):
        body = '<text:p><draw:frame><draw:image xmlns:q="urn:other" q:href="file:///x"/></draw:frame></text:p>'
        doc = _odf("odt", body)
        assert strip_external_references(doc) is doc

    @pytest.mark.parametrize("element", [
        '<text:section text:name="S"><text:section-source xlink:href="file:///etc/hostname"/></text:section>',
        '<table:table table:name="L"><table:table-source xlink:href="file:///etc/hostname"/></table:table>',
        '<text:p><form:button form:image-data="file:///usr/share/pixmaps/debian-logo.png"/></text:p>',
    ])
    def test_linked_sections_tables_and_form_images_lose_their_source(self, element):
        cleaned = _content(strip_external_references(_odf("odt", element)))
        assert "file:///" not in cleaned

    def test_xml_base_is_removed(self):
        body = '<text:p xml:base="file:///usr/share/pixmaps/">x</text:p>'
        assert "xml:base" not in _content(strip_external_references(_odf("odt", body)))

    def test_everything_else_is_left_byte_for_byte(self):
        """Prefixes in attribute values (of:=, ooow:) need their declarations; nothing is re-serialised."""
        body = ('<table:table table:name="S"><table:table-row><table:table-cell table:formula="of:=SUM([.A1])"/>'
                '</table:table-row></table:table>' + _image("file:///etc/x.png"))
        original = _content(_odf("ods", body))
        cleaned = _content(strip_external_references(_odf("ods", body)))
        assert cleaned == original.replace(' xlink:href="file:///etc/x.png"', "")

    def test_mimetype_stays_first_and_stored(self):
        cleaned = strip_external_references(_odf("odp", _slide(_image("file:///etc/x.png"))))
        first = zipfile.ZipFile(io.BytesIO(cleaned)).infolist()[0]
        assert (first.filename, first.compress_type) == ("mimetype", zipfile.ZIP_STORED)

    def test_an_entity_declaration_is_refused(self):
        """An entity expands to markup that is not where the parser reports it."""
        content = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY e "x">]>'
                   f'<office:document-content {ODF_NS}><office:body>&e;'
                   '<draw:image xlink:href="file:///etc/x.png"/></office:body></office:document-content>')
        with pytest.raises(UncheckableDocumentError):
            strip_external_references(_zip({"content.xml": content}, MIMETYPES["odt"]))

    def test_malformed_xml_is_refused(self):
        content = f'<office:document-content {ODF_NS}><draw:image xlink:href="file:///etc/x.png">'
        with pytest.raises(UncheckableDocumentError):
            strip_external_references(_zip({"content.xml": content}, MIMETYPES["odt"]))

    def test_a_broken_zip_is_refused(self):
        doc = _odf("odp", _slide(_image("file:///etc/x.png")))
        with pytest.raises(UncheckableDocumentError):
            strip_external_references(doc[:-30])


class TestOoxmlPackages:

    def test_an_external_image_relationship_is_removed(self):
        doc = _docx([("rL1", IMAGE_REL, "file:///usr/share/pixmaps/debian-logo.png", True),
                     ("rL2", IMAGE_REL, "http://127.0.0.1:4599/favicon.svg", True)],
                    _blip("rL1", "r:link", 1) + _blip("rL2", "r:link", 2))
        rels = zipfile.ZipFile(io.BytesIO(strip_external_references(doc))).read("word/_rels/document.xml.rels")
        assert b"rL1" not in rels and b"rL2" not in rels

    def test_hyperlinks_and_stored_parts_are_kept(self):
        doc = _docx([("rH", HYPERLINK_REL, "https://example.com/", True),
                     ("rI", IMAGE_REL, "media/image1.png", False)],
                    _blip("rI", "r:embed", 1), {"word/media/image1.png": _png(4, 4)})
        assert strip_external_references(doc) is doc

    def test_an_internal_relationship_naming_a_url_is_removed(self):
        doc = _docx([("rX", IMAGE_REL, "file:///etc/x.png", False)], _blip("rX", "r:embed", 1))
        rels = zipfile.ZipFile(io.BytesIO(strip_external_references(doc))).read("word/_rels/document.xml.rels")
        assert b"file:///" not in rels

    def test_the_external_mode_is_recognised_in_any_case(self):
        doc = _docx([("rL", IMAGE_REL, "file:///etc/x.png", True)], _blip("rL", "r:link", 1))
        doc = doc.replace(b'TargetMode="External"', b'TargetMode="EXTERNAL"') if b'TargetMode="External"' in doc else doc
        rels = zipfile.ZipFile(io.BytesIO(strip_external_references(doc))).read("word/_rels/document.xml.rels")
        assert b"file:///" not in rels


class TestRtf:
    PATH = "/usr/share/pixmaps/debian-logo.png"

    @pytest.mark.parametrize("field", [
        '{\\field{\\*\\fldinst INCLUDEPICTURE "%s" }{\\fldrslt shown}}',
        '{\\field{\\fldinst INCLUDEPICTURE "%s" }{\\fldrslt shown}}',
        '{\\field{\\*\\fldinst\\b INCLUDEPICTURE "%s" }{\\fldrslt shown}}',
        '{\\field{\\*\\fldinst {INCLUDE}{PICTURE "%s"}}{\\fldrslt shown}}',
        '{\\field{\\*\\fldinst \\uc0\\u73 NCLUDEPICTURE "%s" }{\\fldrslt shown}}',
        '{\\field{\\*\\fldinst \\\'49NCLUDEPICTURE "%s" }{\\fldrslt shown}}',
        '{\\field{\\*\\fldinst INCLUDETEXT "%s" }{\\fldrslt shown}}',
        '{\\field{\\*\\fldinst IMPORT "%s" }{\\fldrslt shown}}',
        '{\\field{\\*\\fldinst LINK Excel.Sheet "%s" "" }{\\fldrslt shown}}',
        '{\\field{\\*\\fldinst IF 1 = 1 "{\\field{\\*\\fldinst INCLUDEPICTURE "%s" }{\\fldrslt r}}" }'
        '{\\fldrslt shown}}',
    ])
    def test_linking_field_instructions_are_emptied(self, field):
        cleaned = strip_external_references(_rtf(field % self.PATH))
        assert self.PATH.encode() not in cleaned
        assert b"{\\fldrslt shown}" in cleaned  # what the field showed stays

    def test_other_fields_are_untouched(self):
        doc = _rtf('{\\field{\\*\\fldinst HYPERLINK "https://example.com/" }{\\fldrslt link}}'
                   '{\\field{\\*\\fldinst PAGE }{\\fldrslt 1}}')
        assert strip_external_references(doc) == doc

    def test_binary_data_with_braces_does_not_derail_the_scan(self):
        doc = _rtf('{\\*\\blob\\bin4 }}{{}' '{\\field{\\*\\fldinst INCLUDEPICTURE "%s" }{\\fldrslt shown}}' % self.PATH)
        cleaned = strip_external_references(doc)
        assert self.PATH.encode() not in cleaned and b"\\bin4 }}{{" in cleaned

    def test_binary_data_is_left_byte_for_byte(self):
        """\\binN is followed by N raw bytes, which are data, not RTF."""
        payload = b'{\\*\\fldinst INCLUDEPICTURE "x"}'
        doc = _rtf("{\\*\\blob\\bin%d " % len(payload) + payload.decode() + "}")
        assert strip_external_references(doc) == doc

    def test_rtf_is_recognised_by_content_not_by_name(self):
        """LibreOffice opens an RTF uploaded as .doc or .html as RTF."""
        doc = b"  \r\n" + _rtf('{\\field{\\*\\fldinst INCLUDEPICTURE "%s" }{\\fldrslt shown}}' % self.PATH)
        assert self.PATH.encode() not in strip_external_references(doc)


def test_a_package_is_recognised_by_content_not_by_name():
    """An ODP uploaded as .pptx, .docx or .xlsx is still opened as ODP."""
    doc = _odf("odp", _slide(_image("file:///etc/x.png")))
    assert "xlink:href" not in _content(strip_external_references(doc))


@pytest.mark.parametrize("data", [b"plain text", b"<html><img src='file:///etc/x'></html>", b"\xd0\xcf\x11\xe0binary"])
def test_other_content_is_returned_as_it_is(data):
    assert strip_external_references(data) is data


# ---------------------------------------------------------------------------
# The engine: profile, environment, stripping
# ---------------------------------------------------------------------------
class TestEngineWiring:

    @pytest.fixture
    def captured(self, monkeypatch, tmp_path):
        seen = {}

        async def fake_run(self, argv, cwd, src, dst, timeout=None, env=None):
            profile = next(a for a in argv if a.startswith("-env:UserInstallation="))
            seen.update(argv=list(argv), env=env, input=(Path(cwd) / argv[-1]).read_bytes(),
                        settings=(Path(profile.split("file://", 1)[1]) / "user" / "registrymodifications.xcu")
                        .read_text())
            (Path(cwd) / f"input.{dst}").write_bytes(b"%PDF-1.4 stub")

        monkeypatch.setattr(LibreOfficeEngine, "_run_command", fake_run)
        monkeypatch.setattr(libreoffice.tempfile, "gettempdir", lambda: str(tmp_path))
        return seen

    async def test_the_profile_blocks_untrusted_links_and_the_network(self, captured):
        await LibreOfficeEngine().convert(b"hello", "txt", "pdf")
        settings = captured["settings"]
        assert '<prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value>' in settings
        assert '<prop oor:name="ooInetProxyType" oor:op="fuse"><value>2</value>' in settings
        for scheme in ("HTTP", "HTTPS", "FTP"):
            assert (f'<prop oor:name="ooInet{scheme}ProxyName" oor:op="fuse"><value>proxy.invalid</value>'
                    in settings)
        assert '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value>' in settings

    async def test_soffice_gets_the_unreachable_proxy_in_its_environment(self, captured, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "*")
        await LibreOfficeEngine().convert(b"hello", "txt", "pdf")
        env = captured["env"]
        for name in ("http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            assert env[name] == "http://proxy.invalid:9"
        assert "NO_PROXY" not in env and "no_proxy" not in env
        assert env.get("PATH")  # the rest of the environment is passed on

    async def test_soffice_is_handed_the_stripped_document(self, captured):
        doc = _odf("odp", _slide(_image("file:///usr/share/pixmaps/debian-logo.png")))
        await LibreOfficeEngine().convert(doc, "pptx", "pdf")  # named pptx: content decides
        assert b"debian-logo" not in zipfile.ZipFile(io.BytesIO(captured["input"])).read("content.xml")

    async def test_a_document_that_cannot_be_checked_is_not_converted(self, captured):
        doc = _odf("odp", _slide(_image("file:///etc/x.png")))[:-30]
        with pytest.raises(ConversionFailedError, match="could not be checked"):
            await LibreOfficeEngine().convert(doc, "odp", "pdf")
        assert "argv" not in captured


# ---------------------------------------------------------------------------
# Real conversions
# ---------------------------------------------------------------------------
@pytest.fixture
def soffice():
    engine = LibreOfficeEngine()
    if not engine.can_handle("odp", "pdf") or not engine.can_handle("docx", "pdf"):
        pytest.skip("LibreOffice (writer + impress) not installed")
    return engine


@pytest.fixture
def web():
    """A local HTTP server that records every request and answers with a PNG."""
    hits: list[str] = []
    body = _png(55, 22)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", hits
    server.shutdown()


@pytest.fixture
def local_image(tmp_path):
    """An image file on the server's disk with a size nothing else has."""
    path = tmp_path / "secret.png"
    path.write_bytes(_png(77, 33))
    return path


def _image_sizes(pdf: bytes) -> set[tuple[int, int]]:
    from pypdf import PdfReader
    sizes = set()
    for page in PdfReader(io.BytesIO(pdf)).pages:
        for image in page.images:
            sizes.add(image.image.size)
    return sizes


def _convert(engine, data: bytes, src: str) -> bytes:
    return asyncio.run(engine.convert(data, src, "pdf"))


class TestRealConversions:

    def test_html_images_and_stylesheets_are_not_loaded(self, soffice, web, local_image):
        base, hits = web
        html = (f'<html><head><link rel="stylesheet" href="{base}/style.css"></head><body><p>x</p>'
                f'<img src="{local_image}"><img src="{local_image.as_uri()}"><img src="{base}/a.png">'
                f'<table background="{base}/bg.png"><tr><td>c</td></tr></table></body></html>')
        pdf = _convert(soffice, html.encode(), "html")
        assert hits == []
        assert not _image_sizes(pdf) & {(77, 33), (55, 22)}

    def test_a_docx_image_linked_to_a_file_or_url_is_not_embedded(self, soffice, web, local_image):
        base, hits = web
        doc = _docx([("rL1", IMAGE_REL, local_image.as_uri(), True), ("rL2", IMAGE_REL, f"{base}/b.png", True)],
                    "<w:p>" + _blip("rL1", "r:link", 1) + _blip("rL2", "r:link", 2) + "</w:p>")
        pdf = _convert(soffice, doc, "docx")
        assert hits == []
        assert not _image_sizes(pdf) & {(77, 33), (55, 22)}

    @pytest.mark.parametrize("fmt, named", [("odp", "odp"), ("odp", "pptx"), ("ods", "ods"), ("odp", "docx")])
    def test_odf_images_linked_to_a_file_or_url_are_not_embedded(self, soffice, web, local_image, fmt, named):
        base, hits = web
        frames = _image(local_image.as_uri(), 1) + _image(f"{base}/c.png", 4) + _image(
            "../" * 12 + str(local_image).lstrip("/"), 7)
        body = _slide(frames) if fmt == "odp" else f'<table:table table:name="S"><table:shapes>{frames}</table:shapes></table:table>'
        pdf = _convert(soffice, _odf(fmt, body), named)
        assert hits == []
        assert not _image_sizes(pdf) & {(77, 33), (55, 22)}

    @pytest.mark.parametrize("named", ["rtf", "doc"])
    def test_rtf_includepicture_does_not_read_the_file(self, soffice, local_image, named):
        doc = _rtf('{\\field{\\*\\fldinst INCLUDEPICTURE "%s" }{\\fldrslt shown}}' % local_image)
        pdf = _convert(soffice, doc, named)
        assert (77, 33) not in _image_sizes(pdf)

    def test_images_stored_in_the_document_are_kept(self, soffice):
        odp = _odf("odp", _slide(_image("Pictures/kept.png")), {"Pictures/kept.png": _png(61, 29)})
        assert (61, 29) in _image_sizes(_convert(soffice, odp, "odp"))
        docx = _docx([("rI", IMAGE_REL, "media/kept.png", False)], "<w:p>" + _blip("rI", "r:embed", 1) + "</w:p>",
                     {"word/media/kept.png": _png(63, 27)})
        assert (63, 27) in _image_sizes(_convert(soffice, docx, "docx"))
