# AnyDoc

**A document conversion MCP server.** Upload a file, get it back in the format you need: Office ↔ PDF, Markdown ↔ Word, spreadsheets, presentations, images, OCR of scanned pages, and PDF split / extract / rotate / encrypt — all exposed as MCP tools that any MCP-capable client (Claude Code, Claude Desktop, Cursor, FastMCP clients) can call.

Authentication is delegated to [MCP Center](https://github.com/xianhong1208/MCP_Center): AnyDoc is an OAuth 2.1 resource server that verifies MCP Center's RS256 tokens offline through its JWKS. It never stores users or issues tokens itself.

[繁體中文](README.zh-TW.md) · [Quick start](#quick-start) · [Tools](#tools) · [How it works](#how-it-works) · [Configuration](#configuration) · [Architecture](docs/architecture.md)

## Key features

- **Eight tools, one contract.** `convert_document`, `extract_text`, `inspect_document`, `list_supported_conversions`, `pdf_extract_pages`, `pdf_split`, `pdf_rotate`, `pdf_protect`. Every tool takes the same `file_content` / `file_name` / `mime_type` triple, so a host that injects uploaded files as base64 works with all of them.
- **Six engines, chosen by fidelity.** LibreOffice for layout-faithful Office ↔ PDF, Pandoc for structure-preserving text formats, [firecrawl-anydoc](https://pypi.org/project/firecrawl-anydoc/) for fast document → Markdown extraction, Tesseract for OCR, Pillow for images, and a thin text engine for JSON / XML / TSV. A missing engine disables its paths at startup instead of failing at request time.
- **Multi-hop planning that says what it costs.** When no engine converts A → B directly, the registry searches up to three hops, ranked by fidelity. Lossy hops are allowed by default but flagged in the result so the model can tell the user; pass `allow_quality_loss=false` to refuse any path that would lose layout.
- **Format detection without a file name.** Extension → MIME → container inspection (ZIP directory / OLE2 streams) → magic bytes. Sixteen formats are recognised with no name and no MIME type at all.
- **Standards-based auth.** Bearer tokens are verified against MCP Center's JWKS (issuer, audience, optional scopes); the server publishes `/.well-known/oauth-protected-resource/mcp` so OAuth-aware clients discover where to sign in.
- **Runs anywhere.** One Docker image with all engines baked in, or a plain `uv run`. A Windows desktop build (Tkinter GUI, no server) lives in [`desktop/`](desktop/README.md).

## Install

**Prerequisites:** Python 3.13+ and [uv](https://docs.astral.sh/uv/). The Python engines (`firecrawl-anydoc`, `pypdf`, `pillow`) install with `uv sync`; the others are system packages:

```bash
# Debian / Ubuntu
sudo apt-get install -y libreoffice-writer libreoffice-calc libreoffice-impress \
                        pandoc tesseract-ocr tesseract-ocr-chi-tra tesseract-ocr-chi-sim \
                        tesseract-ocr-eng poppler-utils fonts-noto-cjk fonts-dejavu-core
```

`fonts-noto-cjk` is not optional: without it LibreOffice renders CJK text in PDFs as `□□□` and reports success. Tesseract language packs are not optional either — a missing pack makes Tesseract fail to start rather than degrade.

```bash
git clone https://github.com/xianhong1208/AnyDoc.git
cd AnyDoc
uv sync
```

Or skip the system packages entirely and use the container, which ships every engine:

```bash
docker build -t anydoc .
docker run -p 5055:5055 -e MCP_CENTER_URL=http://mcp-center:4568 anydoc
```

## Quick start

### 1. Start MCP Center and register AnyDoc

Run [MCP Center](https://github.com/xianhong1208/MCP_Center) (default `http://localhost:4568`) and register a service pointing at this server: host `127.0.0.1`, port `5055`, path `/mcp`. MCP Center derives the token audience from that registration (`http://127.0.0.1:5055/mcp`); AnyDoc derives the same value from `ANYDOC_BASE_URL`, so the two only need to agree on host and port.

### 2. Start AnyDoc

```bash
cp .env.example .env          # defaults already point at http://localhost:4568
uv run python main.py
```

The startup log lists which engines are available. Open `http://localhost:5055/` for the landing page and `http://localhost:5055/docs` for the REST API.

### 3. Connect a client

Copy the ready-made snippets from the service page in MCP Center, or by hand:

```bash
# Claude Code (OAuth: completes sign-in through MCP Center on first use)
claude mcp add --transport http anydoc http://localhost:5055/mcp

# Any client with a personal access token issued by MCP Center
claude mcp add --transport http anydoc http://localhost:5055/mcp \
  --header "Authorization: Bearer <token>"
```

### 4. Verify

```bash
curl -s http://localhost:5055/.well-known/oauth-protected-resource/mcp   # points at MCP Center
curl -i -X POST http://localhost:5055/mcp                                  # 401 without a token
```

For a local experiment without MCP Center, run with `config/config.test.yaml` (authentication off) and the protocol smoke test:

```bash
SERVER_PORT=5056 uv run python main.py --config config/config.test.yaml
uv run python scripts/mcp_smoke.py
```

## Tools

| Tool | Purpose | Returns |
|---|---|---|
| `convert_document` | Convert to a target format | File |
| `extract_text` | Extract content as Markdown for the model to read | Text |
| `inspect_document` | Format, size, page count and reachable targets | Text |
| `list_supported_conversions` | Capability matrix (only engines that are installed) | Text |
| `pdf_extract_pages` | Pick or reorder pages | File |
| `pdf_split` | Split into several files | Files |
| `pdf_rotate` | Rotate selected pages | File |
| `pdf_protect` | AES-256 encryption | File |

**Supported formats**

| Category | Formats |
|---|---|
| Documents | pdf, docx, doc, odt, rtf, epub |
| Spreadsheets | xlsx, xls, ods, csv, tsv |
| Presentations | pptx, ppt, odp |
| Text | md, html, txt, rst, tex, json, xml |
| Images | png, jpg, webp, gif, bmp, tiff |

Not supported: audio, video, archives, CAD, executables. The tool descriptions and [`config/instructions.md`](config/instructions.md) tell the model to say so instead of retrying.

## How it works

```
client ──(bearer token)──▶ /mcp ──▶ tools ──▶ service.py ──▶ registry.plan_conversion() ──▶ engines
                             │
                             └── JWTVerifier(jwks_uri = MCP_CENTER_URL/.well-known/jwks.json)
```

1. **Input.** Tools receive `file_content` (base64), `file_name` and `mime_type`. Hosts that manage uploads usually inject the base64 automatically; the parameter descriptions are written so the model puts the upload reference in `file_content` and nothing else.
2. **Detection.** `detect_format()` tries the extension, then MIME, then looks inside the container, then magic bytes — because in practice the file name is often missing.
3. **Planning.** `plan_conversion()` finds a direct engine or a path of up to three hops, ranked by fidelity, hop count and intermediate-format preference. Direct paths are never refused. Multi-hop paths that drop below `structural` fidelity are executed with a warning attached; with `allow_quality_loss=false` they are refused instead.
4. **Execution.** Engines run as subprocesses inside a temporary workspace with a timeout; scanned PDFs fall back from text extraction to OCR automatically.
5. **Output.** Tools return `[summary text, File(...)]`; the file arrives as an MCP `EmbeddedResource`.

| Term | Meaning |
|---|---|
| **Fidelity** | `high` (layout preserved), `structural` (headings / lists / tables preserved), `lossy` (text only). Drives path planning. |
| **Hop** | One engine invocation in a multi-step path. |
| **Audience** | The resource URI in the token; must equal what MCP Center registered for this server. |

The engine table, the planner's rules and the format-detection details are in [docs/architecture.md](docs/architecture.md).

## Configuration

Everything is in [`config/config.yaml`](config/config.yaml) and reads environment variables with `${VAR:-default}`. [`.env.example`](.env.example) lists them:

| Variable | Default | Description |
|---|---|---|
| `SERVER_HOST` / `SERVER_PORT` | `0.0.0.0` / `5055` | Bind address. |
| `MCP_CENTER_URL` | `http://localhost:4568` | The MCP Center that issues tokens (`auth.issuer`). |
| `ANYDOC_BASE_URL` | `http://127.0.0.1:5055` | The address this server is reached at. The token audience is `<ANYDOC_BASE_URL>/mcp` and must equal the service registered in MCP Center; it also appears in the protected-resource metadata. |
| `AUTH_ENABLED` | `true` | Set to `false` only for local experiments. |
| `MAX_INPUT_MB` | `50` | Per-file limit; also raises the MCP transport body limit (×1.5). |
| `ENGINE_TIMEOUT` | `180` | Seconds per engine invocation. |
| `LOG_LEVEL` | `INFO` | |

In the YAML, `auth.required_scopes` lets you demand a scope such as `mcp:tools:invoke` on every token, and `auth.audience` overrides the derived audience for the rare case where the registered resource URI is not `<base_url>/mcp`.

## Testing

```bash
uv run pytest                              # logic, < 1 s, engines are mocked
uv run python scripts/sweep_routes.py      # every advertised path against real engines, ~70 s
uv run python scripts/mcp_smoke.py         # protocol check with a real MCP client (server on :5056)
```

The three layers answer different questions — is the logic right, do the engines actually produce valid files, can a client really use the tools — and each has caught bugs the others cannot.

## Known limits

- One file per call (no `pdf_merge` tool yet; the merge logic exists in `pdfops`).
- 50 MB per file, 30 pages per OCR run.
- PDF → editable formats is always lossy; tables inside PDFs are recovered heuristically.
- Images are kept but appended at the end of extracted Markdown, not at their original position.
- `.xlsb` is not supported.

## Contributing

Bug reports and pull requests are welcome. Keep comments and docstrings in English, run `uv run ruff check .` and `uv run pytest` before opening a PR, and run `scripts/sweep_routes.py` after touching the registry or upgrading an engine.

## License

[MIT](LICENSE)
