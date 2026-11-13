"""MIME types of the files convert_document returns.

fastmcp's File labels anything that is not a text type as "application/<format>",
so a Word document went out as "application/docx" and a PNG as "application/png".
Clients use the MIME type to decide how to open or save the file, so each one is
checked here against the registered IANA type, through a real MCP call.
"""

import base64

import pytest
from fastmcp import Client, FastMCP

from src.domain.convert.types import ALL_FORMATS, FORMAT_MIME_TYPES, mime_type_for
from src.fastmcp_tools.morphyard_tools import register_morphyard_tools

# Written out independently of FORMAT_MIME_TYPES so a typo there is caught.
EXPECTED_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "odt": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf",
    "epub": "application/epub+zip",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppt": "application/vnd.ms-powerpoint",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "md": "text/markdown",
    "html": "text/html",
    "txt": "text/plain",
    "rst": "text/x-rst",
    "tex": "application/x-tex",
    "json": "application/json",
    "xml": "application/xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
}


def test_every_format_has_a_mime_type():
    assert set(FORMAT_MIME_TYPES) == ALL_FORMATS
    assert set(EXPECTED_MIME) == ALL_FORMATS


@pytest.mark.parametrize("fmt", sorted(ALL_FORMATS))
def test_mime_type_for_matches_iana(fmt):
    assert mime_type_for(fmt) == EXPECTED_MIME[fmt]


@pytest.mark.parametrize("fmt", sorted(ALL_FORMATS))
async def test_convert_document_returns_the_registered_mime_type(fmt):
    """Same-format requests need no engine, so every output format can be checked here."""
    mcp = FastMCP("mime-test")
    register_morphyard_tools(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool("convert_document", {
            "target_format": fmt,
            "output_name": "report",
            "file_content": base64.b64encode(b"sample").decode(),
            "file_name": f"report.{fmt}",
        })
    resource = result.content[1].resource
    assert resource.mime_type == EXPECTED_MIME[fmt]
    assert str(resource.uri).endswith(f"report.{fmt}")
