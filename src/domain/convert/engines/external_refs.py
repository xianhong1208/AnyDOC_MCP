"""Remove references to outside files and URLs from documents LibreOffice opens

Documents are untrusted. LibreOffice loads what a document links to while it
opens it: an image linked by path or URL is read from the server's disk or
fetched over the network and embedded in the output, so a conversion could hand
back any image file the service can read and make requests to anything it can
reach.

Most of that is stopped by the profile every conversion runs with (see
libreoffice.py: links from untrusted documents blocked, network through a proxy
that does not exist). Two importers ignore the link block, though, and still
read local files:

    ODF packages    images linked by path (draw:image xlink:href) in Calc and
                    Impress documents
    RTF             INCLUDEPICTURE fields

So before LibreOffice sees a document, this module takes out every reference
that points outside it. It goes by what the bytes are, not by the format the
caller named: LibreOffice detects the type from the content, so an ODP package
uploaded as .pptx or an RTF uploaded as .doc is opened as ODP or RTF. Every zip
package gets the ODF and the OOXML treatment, and anything holding an RTF field
instruction gets the RTF one. Images stored inside the document are untouched.
The binary formats (doc, xls, ppt) cannot be rewritten here; for them the
profile's block is what holds, and the tests check that it does.
"""

import io
import re
import zipfile
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from xml.parsers import expat
from xml.sax.saxutils import quoteattr


class UncheckableDocumentError(ValueError):
    """The document could not be read far enough to remove its outside references."""


# ---------------------------------------------------------------------------
# ODF
# ---------------------------------------------------------------------------
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
_FORM_IMAGE_DATA = "{urn:oasis:names:tc:opendocument:xmlns:form:1.0}image-data"
# Hyperlinks are only followed when someone clicks them, never while loading, so
# their targets stay.
_HYPERLINK_ELEMENTS = frozenset({
    "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}a",
    "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}a",
})
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_PACKAGE_SCHEME = "vnd.sun.star.package:"
# Parts that cannot hold a reference are copied as they are, without parsing.
_ODF_REFERENCE_MARKERS = (b"href", b"base", b"image-data")


def _inside_package(href: str, entries: frozenset[str]) -> bool:
    """Whether href names something stored in the package itself.

    Anything else is outside: a URL, an absolute path, or a relative path, which
    ODF resolves against the document's location on disk (so ../../etc/x leaves
    the package)."""
    if href.startswith("#"):
        return True  # an anchor in the document
    value = unquote(href)
    if value.lower().startswith(_PACKAGE_SCHEME):
        value = value[len(_PACKAGE_SCHEME):]
    if _URL_SCHEME.match(value) or value.startswith("/") or "\\" in value:
        return False
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return False
    path = "/".join(parts)
    return path in entries or any(e.startswith(path + "/") for e in entries)


def _qualified(name: str, scope: dict[str, str], attribute: bool) -> str:
    """{namespace}local for a prefixed name, as the parser would resolve it. An
    attribute without a prefix has no namespace; an element takes the default one."""
    prefix, sep, local = name.partition(":")
    if sep:
        uri = scope.get(prefix)
        return f"{{{uri}}}{local}" if uri else name
    if not attribute and scope.get(""):
        return f"{{{scope['']}}}{name}"
    return name


def _start_tag_end(xml: bytes, start: int) -> int:
    """Index just past the '>' closing the start tag that begins at start."""
    quote = None
    for i in range(start, len(xml)):
        c = xml[i]
        if quote:
            if c == quote:
                quote = None
        elif c in (0x22, 0x27):
            quote = c
        elif c == 0x3E:
            return i + 1
    raise UncheckableDocumentError("unterminated start tag")


def _clean_odf_part(xml: bytes, entries: frozenset[str]) -> bytes | None:
    """The part without outside references, or None when it had none.

    The attributes are cut out of the original bytes rather than the part being
    re-serialised: ODF refers to namespace prefixes inside attribute values
    (`table:formula="of:=SUM(A1)"`), and a serialiser drops declarations it sees
    no use for."""
    if not any(marker in xml for marker in _ODF_REFERENCE_MARKERS):
        return None
    edits: list[tuple[int, list[str]]] = []
    scopes: list[dict[str, str]] = [{"xml": "http://www.w3.org/XML/1998/namespace"}]
    parser = expat.ParserCreate()

    def refuse(*_):
        # An entity expands to markup that is not where the parser says it is, so
        # the cut below could miss it. ODF and OOXML never declare one.
        raise UncheckableDocumentError("XML part declares an entity")

    def start(name, attrs):
        declared = {key[6:] if key.startswith("xmlns:") else "": value
                    for key, value in attrs.items() if key == "xmlns" or key.startswith("xmlns:")}
        scope = {**scopes[-1], **declared} if declared else scopes[-1]
        scopes.append(scope)
        tag = _qualified(name, scope, attribute=False)
        drop = []
        for qname, value in attrs.items():
            full = _qualified(qname, scope, attribute=True)
            if full == _XML_BASE:  # would move what relative references resolve against
                drop.append(qname)
            elif full in (_XLINK_HREF, _FORM_IMAGE_DATA):
                if full == _XLINK_HREF and tag in _HYPERLINK_ELEMENTS:
                    continue
                if not _inside_package(value.strip(), entries):
                    drop.append(qname)
        if drop:
            edits.append((parser.CurrentByteIndex, drop))

    parser.EntityDeclHandler = refuse
    parser.StartElementHandler = start
    parser.EndElementHandler = lambda name: scopes.pop()
    try:
        parser.Parse(xml, True)
    except expat.ExpatError as e:
        raise UncheckableDocumentError(f"malformed XML part: {e}") from e
    if not edits:
        return None
    out = bytearray(xml)
    for start_at, names in reversed(edits):
        end_at = _start_tag_end(xml, start_at)
        tag = bytes(out[start_at:end_at])
        for qname in names:
            pattern = rb"\s+" + re.escape(qname.encode("utf-8")) + rb"\s*=\s*(\"[^\"]*\"|'[^']*')"
            tag, count = re.subn(pattern, b"", tag, count=1)
            if count != 1:
                raise UncheckableDocumentError("attribute not found where the parser reported it")
        out[start_at:end_at] = tag
    return bytes(out)


# ---------------------------------------------------------------------------
# OOXML
# ---------------------------------------------------------------------------
_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_RELATIONSHIP = f"{{{_RELS_NS}}}Relationship"


def _keeps(rel: ET.Element) -> bool:
    """Whether a relationship stays: anything stored in the package, or a hyperlink."""
    if (rel.get("TargetMode") or "").strip().lower() != "external":
        target = unquote(rel.get("Target") or "")
        return not (_URL_SCHEME.match(target) or "\\" in target)
    return (rel.get("Type") or "").rstrip("/").endswith("/hyperlink")


def _clean_rels_part(xml: bytes) -> bytes | None:
    """The .rels part without relationships that point outside the package, or
    None when it had none. Rebuilt from scratch: a relationship has only Id,
    Type, Target and TargetMode."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise UncheckableDocumentError(f"malformed relationships part: {e}") from e
    rels = list(root.iter(_RELATIONSHIP))
    kept = [el for el in rels if _keeps(el)]
    if len(kept) == len(rels):
        return None
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             f'<Relationships xmlns="{_RELS_NS}">']
    for el in kept:
        attrs = " ".join(f"{name}={quoteattr(el.get(name))}"
                         for name in ("Id", "Type", "Target", "TargetMode") if el.get(name) is not None)
        lines.append(f"<Relationship {attrs}/>")
    lines.append("</Relationships>")
    return "".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# Zip packages (ODF and OOXML alike)
# ---------------------------------------------------------------------------
def _clean_package_part(name: str, raw: bytes, entries: frozenset[str]) -> bytes | None:
    lower = name.lower()
    if lower.endswith(".rels"):
        return _clean_rels_part(raw)
    if lower.endswith(".xml"):
        return _clean_odf_part(raw, entries)
    return None


def _package(data: bytes) -> bytes:
    """The package with every part cleaned; rebuilt only when some part changed.

    A package that cannot be read is refused rather than passed on: LibreOffice
    repairs broken zips and reads XML this parser rejects, so passing it on would
    hand it a document nobody checked."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zin:
            infos = zin.infolist()
            entries = frozenset(info.filename.rstrip("/") for info in infos)
            parts, changed = [], False
            for info in infos:
                raw = zin.read(info)
                cleaned = _clean_package_part(info.filename, raw, entries)
                changed |= cleaned is not None
                parts.append((info, raw if cleaned is None else cleaned))
    except UncheckableDocumentError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError, EOFError,
            NotImplementedError, RuntimeError) as e:
        raise UncheckableDocumentError(f"unreadable package: {e}") from e
    if not changed:
        return data
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zout:
        for info, raw in parts:
            # ODF wants `mimetype` first and stored: every entry keeps its place and method.
            zout.writestr(info, raw, compress_type=info.compress_type)
    return out.getvalue()


# ---------------------------------------------------------------------------
# RTF
# ---------------------------------------------------------------------------
# Field types that load another file or URL into the document.
_LINKING_FIELD = re.compile(
    r"^[\s\"]*(INCLUDEPICTURE|INCLUDETEXT|INCLUDE|IMPORT|LINK|DDEAUTO|DDE|DATABASE)\b", re.IGNORECASE)
_CONTROL = re.compile(rb"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?|\\'([0-9a-fA-F]{2})|\\(.)", re.DOTALL)
_EMPTY_FIELD_INSTRUCTION = b"{\\*\\fldinst }"


def _rtf_text(group: bytes) -> str:
    """The text an RTF group spells, the way a reader would see it: control words
    dropped, \\'hh and \\uN decoded (with their \\ucN fallback characters skipped)."""
    out: list[str] = []
    uc_stack = [1]
    skip = 0
    i, n = 0, len(group)
    while i < n:
        c = group[i:i + 1]
        if c == b"{":
            uc_stack.append(uc_stack[-1])
            i += 1
            continue
        if c == b"}":
            if len(uc_stack) > 1:
                uc_stack.pop()
            skip = 0
            i += 1
            continue
        if c == b"\\":
            m = _CONTROL.match(group, i)
            if not m:
                i += 1
                continue
            i = m.end()
            word, param, hexbyte, symbol = m.groups()
            if word == b"bin":
                i += max(int(param or 0), 0)
                continue
            if word == b"uc":
                uc_stack[-1] = max(int(param or 0), 0)
                continue
            if word == b"u" and param is not None:
                out.append(chr(int(param) % 0x10000))
                skip = uc_stack[-1]
                continue
            if skip:
                if hexbyte or symbol:
                    skip -= 1
                continue
            if hexbyte:
                out.append(bytes.fromhex(hexbyte.decode()).decode("latin-1"))
            elif symbol and symbol in b"\\{}":
                out.append(symbol.decode())
            elif word in (b"tab", b"par", b"line"):
                out.append(" ")
            continue
        i += 1
        if c in (b"\r", b"\n"):
            continue
        if skip:
            skip -= 1
            continue
        out.append(c.decode("latin-1"))
    return "".join(out)


def _rtf(data: bytes) -> bytes:
    """The RTF with every linking field's instruction emptied; the field keeps the
    result Word stored with it, so the document still shows what it showed."""
    out = bytearray()
    stack: list[list] = []  # [start in data, start in out, holds \fldinst]
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c == 0x7B:  # {
            stack.append([i, len(out), False])
            out.append(c)
            i += 1
        elif c == 0x7D:  # }
            out.append(c)
            i += 1
            if stack:
                start, out_start, is_instruction = stack.pop()
                if is_instruction and _LINKING_FIELD.match(_rtf_text(data[start + 1:i - 1])):
                    del out[out_start:]
                    out += _EMPTY_FIELD_INSTRUCTION
        elif c == 0x5C:  # backslash
            m = _CONTROL.match(data, i)
            end = m.end() if m else i + 1
            if m and m.group(1) == b"bin":
                end += max(int(m.group(2) or 0), 0)
            elif m and m.group(1) == b"fldinst" and stack:
                stack[-1][2] = True
            out += data[i:end]
            i = end
        else:
            out.append(c)
            i += 1
    return bytes(out)


def strip_external_references(data: bytes) -> bytes:
    """data, minus references to files and URLs outside the document.

    Raises UncheckableDocumentError for a zip package that cannot be read."""
    if data[:4] == b"PK\x03\x04" or zipfile.is_zipfile(io.BytesIO(data)):
        return _package(data)
    if re.search(rb"\\fldinst", data, re.IGNORECASE):
        return _rtf(data)
    return data
