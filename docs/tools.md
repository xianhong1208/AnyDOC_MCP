# Tools and formats

[繁體中文](tools.zh-TW.md) · [README](../README.md)

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

Not supported: audio, video, archives, CAD, executables. The tool descriptions and [`config/instructions.md`](../config/instructions.md) tell the model to say so instead of retrying.

## How it works

```
client ──(bearer token)──▶ /mcp ──▶ tools ──▶ service.py ──▶ registry.plan_conversion() ──▶ engines
                             │
                             └── JWTVerifier(jwks_uri = MCP_GATE_URL/.well-known/jwks.json)
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
| **Audience** | The resource URI in the token; must equal what MCP Gate registered for this server. |

The engine table, the planner's rules and the format-detection details are in [docs/architecture.md](architecture.md).

## Known limits

- One file per call (no `pdf_merge` tool yet; the merge logic exists in `pdfops`).
- 50 MB per file, 30 pages per OCR run.
- A limited number of conversions run at once (by default the CPU count, at most 8); more wait in a queue, and past its length or wait time a tool answers "the server is busy" and should be called again a little later. See [Concurrent conversions](configuration.md#concurrent-conversions).
- PDF → editable formats is always lossy; tables inside PDFs are recovered heuristically.
- Images are kept but appended at the end of extracted Markdown, not at their original position.
- `.xlsb` is not supported.
- Pandoc conversions never read files or URLs a document points at: LaTeX `\input` / `\lstinputlisting` / `\includegraphics` and images given as a path or `http(s)` URL are dropped (an image keeps its alt text); images embedded in the document or given as `data:` URIs are kept.
- LibreOffice conversions never read files or URLs a document links to either: an image linked by path or URL (an HTML `<img>`, a DOCX/XLSX/PPTX relationship marked external, an ODF `xlink:href`, an RTF `INCLUDEPICTURE` field) is left out of the output, and stylesheets are not fetched. Every conversion runs with a LibreOffice profile that blocks links from untrusted documents and sends all HTTP/HTTPS/FTP traffic to a proxy that cannot exist, and ODF, OOXML and RTF documents are stripped of outside references first (by content, so an ODP renamed to `.pptx` is treated as ODP). Images stored inside the document are kept. A zip-based document that cannot be read far enough to check it is refused instead of converted.
- Revocation is not instant: with the [revocation feed](configuration.md#token-revocation) a revoked token stops working within one poll interval (30 s by default); without it, at expiry.
