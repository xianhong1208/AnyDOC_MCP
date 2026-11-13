# Morphyard

[![License: MIT](https://img.shields.io/github/license/xianhong1208/morphyard)](LICENSE)
[![CI](https://github.com/xianhong1208/morphyard/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/xianhong1208/morphyard/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/xianhong1208/morphyard)](https://github.com/xianhong1208/morphyard/releases/latest)

**A document-conversion MCP server: hand it a file, get it back in the format you need — Office ↔ PDF, Markdown ↔ Word, spreadsheets, presentations, images, OCR and PDF page tools — from any MCP client.**

Formerly AnyDoc; see [renamed variables](docs/configuration.md#renamed-variables) if you are upgrading.

[繁體中文](README.zh-TW.md) · [Tools and formats](docs/tools.md) · [Configuration](docs/configuration.md) · [Architecture](docs/architecture.md) · [Roadmap](ROADMAP.md) · [Changelog](CHANGELOG.md)

Morphyard exposes its conversions as MCP tools that any MCP-capable client (Claude Code, Claude Desktop, Cursor, FastMCP clients) can call. Authentication is delegated to [MCP Gate](https://github.com/xianhong1208/MCP_Gate): Morphyard is an OAuth 2.1 resource server that verifies MCP Gate's RS256 tokens offline through its JWKS. It never stores users or issues tokens itself.

## What a call looks like

A Word file goes in as base64; a PDF comes back as an MCP embedded resource, with a summary the model can relay. From a real run against a local server (FastMCP client):

```python
await client.call_tool("convert_document", {
    "file_content": "<base64 of report.docx>",
    "file_name": "report.docx",
    "target_format": "pdf",
    "output_name": "report",
})
```

```text
TEXT  Conversion complete: report.pdf
      Path: docx → pdf (engines: libreoffice)
      Size: 23.4 KB
FILE  file:///report.pdf  application/pdf  23963 bytes
```

And `extract_text` on the same file returns Markdown for the model to read:

```markdown
# Quarterly report

| Region | Sales |
|--------|-------|
| North  | 120   |
| South  | 95    |

- North leads this quarter
```

## What's new

**1.2.1** (2026-11-09): LibreOffice conversions can no longer read local files or fetch URLs a document links to, and never run macros — the same guarantee Pandoc conversions got in 1.2.0, which also added CI that runs every advertised conversion inside the Docker image. See the [CHANGELOG](CHANGELOG.md), and the [Roadmap](ROADMAP.md) for what comes next.

## Key features

- **Eight tools, one contract.** `convert_document`, `extract_text`, `inspect_document`, `list_supported_conversions`, `pdf_extract_pages`, `pdf_split`, `pdf_rotate`, `pdf_protect`. Every tool takes the same `file_content` / `file_name` / `mime_type` triple, so a host that injects uploaded files as base64 works with all of them.
- **Six engines, chosen by fidelity.** LibreOffice for layout-faithful Office ↔ PDF, Pandoc for structure-preserving text formats, [firecrawl-anydoc](https://pypi.org/project/firecrawl-anydoc/) for fast document → Markdown extraction, Tesseract for OCR, Pillow for images, and a thin text engine for JSON / XML / TSV. A missing engine disables its paths at startup instead of failing at request time.
- **Multi-hop planning that says what it costs.** When no engine converts A → B directly, the registry searches up to three hops, ranked by fidelity. Lossy hops are allowed by default but flagged in the result so the model can tell the user; pass `allow_quality_loss=false` to refuse any path that would lose layout.
- **Format detection without a file name.** Extension → MIME → container inspection (ZIP directory / OLE2 streams) → magic bytes. Sixteen formats are recognised with no name and no MIME type at all.
- **Documents cannot reach out.** Conversions never read local files or fetch URLs a document points at, and LibreOffice never runs macros.
- **Standards-based auth.** Bearer tokens are verified against MCP Gate's JWKS (issuer, audience, optional scopes); the server publishes `/.well-known/oauth-protected-resource/mcp` so OAuth-aware clients discover where to sign in. With the optional revocation feed, a token revoked in MCP Gate stops working here within one poll interval (30 s by default).
- **Runs anywhere.** One Docker image with all engines baked in, or a plain `uv run`.

Supported: pdf, docx, doc, odt, rtf, epub; xlsx, xls, ods, csv, tsv; pptx, ppt, odp; md, html, txt, rst, tex, json, xml; png, jpg, webp, gif, bmp, tiff. The full tool and format tables are in [docs/tools.md](docs/tools.md).

## Installation

**Prerequisites:** Python 3.13+ and [uv](https://docs.astral.sh/uv/). The Python engines (`firecrawl-anydoc`, `pypdf`, `pillow`) install with `uv sync`; the others are system packages:

```bash
# Debian / Ubuntu
sudo apt-get install -y libreoffice-writer libreoffice-calc libreoffice-impress \
                        pandoc tesseract-ocr tesseract-ocr-chi-tra tesseract-ocr-chi-sim \
                        tesseract-ocr-eng poppler-utils fonts-noto-cjk fonts-dejavu-core
```

`fonts-noto-cjk` is not optional: without it LibreOffice renders CJK text in PDFs as `□□□` and reports success. Tesseract language packs are not optional either — a missing pack makes Tesseract fail to start rather than degrade.

```bash
git clone https://github.com/xianhong1208/morphyard.git
cd morphyard
uv sync
```

## Quick start

### 1. Start MCP Gate and register Morphyard

Run [MCP Gate](https://github.com/xianhong1208/MCP_Gate) (default `http://localhost:4568`) and register a service pointing at this server: host `127.0.0.1`, port `5055`, path `/mcp`. MCP Gate derives the token audience from that registration (`http://127.0.0.1:5055/mcp`); Morphyard derives the same value from `MORPHYARD_BASE_URL`, so the two only need to agree on host and port.

### 2. Start Morphyard

```bash
cp .env.example .env          # defaults already point at http://localhost:4568
uv run python main.py
```

The startup log lists which engines are available. Open `http://localhost:5055/` for the landing page and `http://localhost:5055/docs` for the REST API.

### 3. Connect a client

Copy the ready-made snippets from the service page in MCP Gate, or by hand:

```bash
# Claude Code (OAuth: completes sign-in through MCP Gate on first use)
claude mcp add --transport http morphyard http://localhost:5055/mcp

# Any client with a personal access token issued by MCP Gate
claude mcp add --transport http morphyard http://localhost:5055/mcp \
  --header "Authorization: Bearer <token>"
```

Then ask your assistant to convert a file, e.g. "turn this DOCX into a PDF".

### 4. Verify

```bash
curl -s http://localhost:5055/.well-known/oauth-protected-resource/mcp   # points at MCP Gate
curl -s http://localhost:5055/.well-known/oauth-authorization-server      # MCP Gate's metadata, re-served for older clients
curl -i -X POST http://localhost:5055/mcp                                  # 401 without a token
```

For a local experiment without MCP Gate, start it with authentication off and run the protocol smoke test:

```bash
AUTH_ENABLED=false SERVER_PORT=5056 MORPHYARD_REVOCATION_ENABLED=false uv run python main.py
uv run python scripts/mcp_smoke.py
```

## Deployment options

- **Docker** — skip the system packages; the image ships every engine and font:

  ```bash
  docker build -t morphyard .
  docker run -p 5055:5055 -e MCP_GATE_URL=http://mcp-gate:4568 \
    -e MORPHYARD_BASE_URL=http://127.0.0.1:5055 morphyard
  ```

- **Directly on the host** with `uv run python main.py`, as in the quick start.

Either way, `MORPHYARD_BASE_URL` must be the address clients use, because the token audience is derived from it. Every setting, and how to turn on the revocation feed, is in [docs/configuration.md](docs/configuration.md).

## Documentation

| Document | What is in it |
|---|---|
| [Tools and formats](docs/tools.md) | The eight tools, supported formats, how a conversion runs, known limits |
| [Configuration](docs/configuration.md) | Environment variables, concurrent conversions and the MCP Gate revocation feed |
| [Architecture](docs/architecture.md) | Module layout, engines and fidelity, format detection, conversion planning, test layers |
| [Development](docs/development.md) | Setting up, the three test layers, CI, how to contribute |
| [Roadmap](ROADMAP.md) | What shipped and what comes next |
| [Changelog](CHANGELOG.md) | Every release |

## Contributing

Bug reports and pull requests are welcome. Pull requests go into `dev`; run `uv run ruff check .` and `uv run pytest` first. See [docs/development.md](docs/development.md).

## Licence

[MIT](LICENSE)
