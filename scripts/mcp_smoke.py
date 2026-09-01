"""MCP protocol-level smoke test: receive file -> convert -> return file.

    # Start the server in another terminal (config.test.yaml disables auth)
    SERVER_PORT=5056 uv run python main.py --config config/config.test.yaml
    # then
    uv run python scripts/mcp_smoke.py

Simulates a host that injects uploaded files, end to end:
  1. Connect to /mcp and list the tools (exactly what the AI sees)
  2. Call tools with base64 in file_content (playing the role of the host
     application's file-injection (artifact) mechanism)
  3. Inspect the returned content blocks: text summary + downloadable files

**Why pytest cannot replace this script.**
It caught a bug that is structurally invisible to unit tests: every tool returning a
list was missing `output_schema=None`, so the client received

    ToolError: Output validation error: outputSchema defined but no
    structured output returned

The tool functions themselves returned fine; FastMCP rejected the response only while
serializing it. It shows up only with a real MCP connection, a real tool call and a
real parse of the response.

It also pins down the real wire shape of File: it serializes as an EmbeddedResource
with the bytes in `block.resource.blob` (base64), not `block.data`.

When to run: after changing tool signatures or return shapes, after upgrading fastmcp,
and as post-deployment acceptance.

The sample document is deliberately Chinese so CJK fonts and text extraction get
exercised along the way.
"""
import asyncio
import base64
import subprocess
import tempfile
from pathlib import Path

from fastmcp import Client

URL = "http://127.0.0.1:5056/mcp"
WORK = Path(tempfile.mkdtemp(prefix="mcp_e2e_"))


def make_docx() -> bytes:
    """Build a Word file like one a user would upload."""
    md = """# 2026 年度供應商評估報告

## 評分結果

| 供應商 | 品質 | 交期 | 總分 |
|---|---|---|---|
| 甲公司 | 92 | 88 | 90 |
| 乙公司 | 85 | 95 | 90 |

## 結論

- 甲公司品質領先，建議續約
- 乙公司交期穩定，可作為第二來源
"""
    (WORK / "s.md").write_text(md, encoding="utf-8")
    subprocess.run(["pandoc", "-f", "markdown", "-t", "docx", "--standalone",
                    "-o", "report.docx", "s.md"], cwd=WORK, check=True)
    return (WORK / "report.docx").read_bytes()


def unwrap_file(block):
    """Extract (name, mime, bytes) from a content block; None if it is not a file.

    A FastMCP File serializes as an EmbeddedResource on the wire: the real bytes live
    in block.resource.blob (a base64 string), not block.data. Only ImageContent uses
    block.data. Both shapes must be handled.
    """
    resource = getattr(block, "resource", None)
    if resource is not None and getattr(resource, "blob", None):
        name = str(getattr(resource, "uri", "file")).rsplit("/", 1)[-1]
        return name, getattr(resource, "mimeType", "") or "", \
            base64.b64decode(resource.blob)
    data = getattr(block, "data", None)
    if data is not None:
        raw = base64.b64decode(data) if isinstance(data, str) else data
        return "inline", getattr(block, "mimeType", "") or "", raw
    return None


def describe(block) -> str:
    kind = type(block).__name__
    if getattr(block, "text", None):
        return f"[{kind}] {block.text[:200]}"
    got = unwrap_file(block)
    if got:
        name, mime, raw = got
        return f"[{kind}] {name} mime={mime} {len(raw)}B head={raw[:6]!r}"
    return f"[{kind}] {block!r}"[:200]


def save_files(result, prefix: str) -> list:
    """Save every returned file block; returns a list of (path, bytes)."""
    saved = []
    for i, block in enumerate(result.content):
        got = unwrap_file(block)
        if not got:
            continue
        name, mime, raw = got
        ext = name.rsplit(".", 1)[-1] if "." in name else "bin"
        p = WORK / f"{prefix}_{i}.{ext}"
        p.write_bytes(raw)
        saved.append((p, raw))
    return saved


async def main():
    docx = make_docx()
    b64 = base64.b64encode(docx).decode()
    print(f"User uploads: report.docx ({len(docx)}B)\n")

    async with Client(URL) as client:
        tools = await client.list_tools()
        print(f"=== Tools visible to the AI ({len(tools)}) ===")
        for t in tools:
            first_line = (t.description or "").strip().split("\n")[0]
            print(f"  {t.name:<28} {first_line[:56]}")

        # -- Scenario 1: the user asks "what is this document about" --
        print("\n=== Scenario 1: 'what is this document about' -> extract_text ===")
        r = await client.call_tool("extract_text", {
            "file_content": b64, "file_name": "report.docx",
            "mime_type": "application/vnd.openxmlformats-officedocument"
                         ".wordprocessingml.document",
        })
        text = r.content[0].text
        print(text[:280])
        assert "供應商" in text and "甲公司" in text, "text extraction failed"

        # -- Scenario 2: the user says "convert it to PDF" --
        print("\n=== Scenario 2: 'convert it to PDF' -> convert_document ===")
        r = await client.call_tool("convert_document", {
            "target_format": "pdf", "output_name": "report",
            "file_content": b64, "file_name": "report.docx", "mime_type": None,
        })
        for b in r.content:
            print("  " + describe(b))
        files = save_files(r, "converted")
        assert files, "no file returned"
        pdf_path, pdf_bytes = files[0]
        assert pdf_bytes.startswith(b"%PDF-"), "returned file is not a valid PDF"
        print(f"  -> user can download: {pdf_path.name}")

        # -- Scenario 3: the user says "only page 1" --
        print("\n=== Scenario 3: 'only page 1' -> pdf_extract_pages ===")
        r = await client.call_tool("pdf_extract_pages", {
            "pages": "1", "file_content": base64.b64encode(pdf_bytes).decode(),
            "file_name": "converted.pdf", "mime_type": "application/pdf",
        })
        for b in r.content:
            print("  " + describe(b))

        # -- Scenario 4: the user asks for something unsupported --
        print("\n=== Scenario 4: 'turn this PDF back into Word' -> should refuse "
              "and guide the AI ===")
        try:
            await client.call_tool("convert_document", {
                "target_format": "docx", "output_name": "report",
                "file_content": base64.b64encode(pdf_bytes).decode(),
                "file_name": "converted.pdf", "mime_type": "application/pdf",
            })
            print("  !! was not refused")
        except Exception as e:
            print(f"  error seen by the AI: {str(e)[:260]}")

        # -- Scenario 5: the user says "I know the layout will suffer, do it anyway" --
        print("\n=== Scenario 5: same as above with allow_quality_loss=true ===")
        r = await client.call_tool("convert_document", {
            "target_format": "docx", "output_name": "report",
            "allow_quality_loss": True,
            "file_content": base64.b64encode(pdf_bytes).decode(),
            "file_name": "converted.pdf", "mime_type": "application/pdf",
        })
        print("  " + describe(r.content[0]))
        got = save_files(r, "downgraded")
        assert got and got[0][1].startswith(b"PK\x03\x04"), "returned file is not a valid docx"
        print(f"  -> user can download: {got[0][0].name} (valid docx)")

        # -- Scenario 6: capability query --
        print("\n=== Scenario 6: 'which formats do you support' -> list_supported_conversions ===")
        r = await client.call_tool("list_supported_conversions",
                                   {"source_format": "docx"})
        print("  " + r.content[0].text[:200])

    print(f"\nAll scenarios passed. Output files are in {WORK}")


asyncio.run(main())
