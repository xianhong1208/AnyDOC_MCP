# AnyDoc architecture

This document describes how AnyDoc is built: the module layout, the conversion engines and the fidelity model that selects between them, the input/output contract that MCP tools expose, format detection, conversion planning, the test layers, and the limits that follow from these choices. For installation and configuration see the [README](../README.md).

## Module layout

```
src/
├── domain/convert/
│   ├── types.py            Format identifiers, four-layer detection, exceptions
│   ├── registry.py         Route table and conversion planning (the capability boundary)
│   ├── service.py          base64 input → planned path → output
│   ├── pdfops.py           PDF-only operations (split / extract / rotate / encrypt / images)
│   └── engines/
│       ├── base.py           Subprocess execution, temporary workspace, timeout, component discovery
│       ├── discovery.py      Cross-platform executable lookup (PATH plus known install locations)
│       ├── libreoffice.py    Office ↔ PDF, layout fidelity
│       ├── pandoc.py         Text formats, structural fidelity
│       ├── anydoc_engine.py  Document → Markdown (firecrawl-anydoc)
│       ├── ocr.py            Image / scanned PDF → text (Tesseract)
│       ├── image.py          Image ↔ image (Pillow)
│       └── text.py           JSON / XML / TSV → Markdown
├── auth/                   OAuth 2.1 resource-server wiring (JWKS verification)
└── fastmcp_tools/
    └── anydoc_tools.py     MCP tool layer
```

Dependencies point strictly downward. The tool layer never touches base64 payloads or engines; it talks only to `service.py`. `registry.py` is the single place that declares what the service can do: every route the capability matrix advertises is a row in its table, and `plan_conversion()` consults nothing else.

## Engines

Six engines, each with a fixed fidelity level. Fidelity is not a descriptive label: `plan_conversion()` uses it to decide whether a multi-hop path is acceptable (see [Conversion planning](#conversion-planning)).

| Engine | Fidelity | Preserves | Used for |
|---|---|---|---|
| LibreOffice | `high` | Layout, fonts, positions | Office ↔ PDF; any conversion that must "look the same" |
| Pillow | `high` | Pixels | Image format conversion, resizing, compression |
| Pandoc | `structural` | Heading hierarchy, lists, table structure | Semantic conversion between Markdown / HTML / Word / PPTX |
| anydoc | `lossy` | Text and tables only | Extracting PDF, Excel, PowerPoint and EPUB content for a model to read |
| Tesseract | `lossy` | Text only | OCR of images and scanned PDFs |
| TextEngine | `lossy` | Raw text | Lightweight wrapping of JSON / XML / TSV |

Engine availability is probed once at startup. A missing `pandoc` or `soffice` does not prevent the server from starting; it removes the corresponding routes from the capability matrix. `discovery.py` looks beyond `PATH` because on Windows LibreOffice, Tesseract and Pandoc install to fixed directories that are not added to `PATH`; without this layer four of the six engines report as unavailable on a machine where they are installed.

### Why firecrawl-anydoc replaced MarkItDown

The document → Markdown slot originally used Microsoft MarkItDown. It was replaced by [firecrawl-anydoc](https://pypi.org/project/firecrawl-anydoc/) (Rust, MIT) after measurement:

| Format | Speed | Quality |
|---|---|---|
| docx | 22× faster | anydoc keeps `> blockquote` and `[^1]` footnote syntax; MarkItDown loses both |
| xlsx | 86× faster | Equivalent |
| pptx | 17× faster | anydoc keeps `**bold**`; MarkItDown flattens to plain text |
| epub | 17× faster | MarkItDown injects metadata noise such as `**Language:** C` |
| pdf | 2× faster | Equivalent |

The dependency footprint differs more than the speed: `markitdown[all]` pulls in 411 MB (including onnxruntime, torch and youtube-transcript-api) and pins the Python version because onnxruntime only ships wheels up to cp313. `firecrawl-anydoc` is an abi3 wheel; the virtual environment shrank from 411 MB to 144 MB.

### Intermediate-format choices

- **`epub → md` is routed through anydoc explicitly.** Pandoc's EPUB reader emits internal anchors such as `<span id="ch001.xhtml">`, which are pure noise for a model.
- **`docx → md` and `odt → md` stay on Pandoc.** Its output is equally clean, and because the route is `structural` (reversible) rather than `lossy`, it is a better intermediate for multi-hop paths.
- **anydoc does not process images.** Images always go to Tesseract.

### Scanned-PDF fallback

When anydoc meets a PDF without a text layer it raises `UnsupportedError: ... OCR is required`. The service catches this and hands the file to the OCR engine: `pdftoppm` rasterises pages to PNG at 150 DPI (`RASTER_DPI`), Tesseract recognises each page, and output stops at `MAX_OCR_PAGES = 30` with an explicit truncation notice in the returned text.

## Input / output contract

### Input

Every tool accepts the same three optional parameters:

```python
file_content: Optional[str]   # base64; hosts that manage uploads inject this
file_name:    Optional[str]   # primary input to format detection
mime_type:    Optional[str]
```

The names are part of the public contract and must not change. Hosts that inject uploaded files match on these exact names.

### Output

File-producing tools return `[summary text, File(...)]`, where `File` comes from `fastmcp.utilities.types`. On the wire the `File` is serialised as an MCP `EmbeddedResource`: the bytes are base64 in `block.resource.blob`, not in `block.data` (that field is used only by `ImageContent`).

Tools that return a list must be declared with `@mcp.tool(output_schema=None)`. FastMCP otherwise derives an `outputSchema` from the `-> list` annotation and demands structured output, and clients receive `Output validation error: outputSchema defined but no structured output returned`. The tool functions themselves return normally; the error only appears when a real MCP client calls the server, which is why `scripts/mcp_smoke.py` exists.

### Hosts that inject uploaded files (artifact injection)

Some MCP hosts manage file uploads on the user's behalf. The model receives a placeholder reference such as `[Uploaded Artifact: "report.docx"]`, and a host-side plugin resolves that reference to bytes before the tool is called. AnyDoc is written to survive the injection behaviour observed in such hosts, which is not intuitive:

1. **Matching is by parameter value, not parameter name.** The host scans every string parameter for an upload reference. When it finds one it writes the base64 into `file_content` (overwriting any existing value), fills `file_name` with the original name only if `file_name` is not already set, and blanks the parameter that held the reference (unless that parameter was `file_content` itself).
2. **`mime_type` is never injected on the MCP path.** Format detection therefore depends on `file_name` in practice, which is why `detect_format()` tries the extension before the MIME type. The parameter is kept for REST callers.
3. **Non-string parameters are skipped.** A `list[str]` parameter can never receive a file, so a multi-file tool such as `pdf_merge` cannot be expressed under this contract.
4. **The parameter holding the reference is blanked.** The consequences depend on where the model put the reference:

   | Model puts the reference in | Result |
   |---|---|
   | `file_content` | Not blanked; `file_name` is filled with the original name. Correct. |
   | `file_name` | `file_name` is already set, so the original name is not applied; then `file_name` is blanked to `""`. |

   The second case was observed in production logs (`Artifact resolved: (unnamed) → format=pptx`) and produced two symptoms: the first call failed because no format could be detected (the model retried with `mime_type` and succeeded, wasting one round trip), and the returned file lost its original name.

Two defences address this:

- `detect_format()` inspects the container so that recognition no longer depends on the file name (next section).
- The `file_content` parameter description explicitly invites the model to place the upload reference there, and the `file_name` description says not to. The wording of these descriptions determines whether the service works under such a host.

## Format detection

`detect_format()` in `types.py` applies four layers in order:

```
extension → MIME type → container inspection → magic bytes
```

The first two layers exist for the common case. The last two exist for the case where no file name is available. The third layer is decisive, because magic bytes alone cannot separate the Office families: `docx`, `xlsx`, `pptx`, `odt`, `ods`, `odp` and `epub` all begin with `PK\x03\x04`, and `doc`, `xls` and `ppt` all begin with `\xd0\xcf\x11\xe0`. Opening the container resolves the ambiguity.

- **ZIP-based formats.** ODF and EPUB contain a `mimetype` entry whose content is the full MIME string. OOXML is identified by its directory prefix (`word/`, `xl/`, `ppt/`). `zipfile` reads only the central directory and decompresses nothing, so the cost is negligible.
- **OLE2-based formats.** The format is identified by stream names in the OLE2 directory: `WordDocument`, `Workbook`, `PowerPoint Document` (stored as UTF-16LE; real generators vary, e.g. `Workbook` / `WORKBOOK` / `Book`). The directory cannot be found by scanning the start of the file, because OLE2 places it anywhere; LibreOffice writes the directory at the end of `.ppt` files (in one measured file of 499,200 bytes the directory started at offset 498,560). The location is computed from the header: offset `0x1E` holds the sector size as a power of two, offset `0x30` holds the first directory sector number.

Measured result: sixteen formats are recognised correctly with neither `file_name` nor `mime_type` supplied (16/16).

`_EXT_ALIASES` maps extension variants (`.docm`, `.xlsm`, `.pptm`, `.ppsx`) onto their base format; each alias in the table has been verified against real files.

## Tools

| Tool | Purpose | Returns |
|---|---|---|
| `convert_document` | Primary conversion to a target format | File |
| `extract_text` | Extract content as Markdown for the model to read | Text |
| `inspect_document` | Format, size, page count, reachable targets | Text |
| `list_supported_conversions` | Capability matrix (installed engines only) | Text |
| `pdf_extract_pages` | Pick or reorder pages | File |
| `pdf_split` | Split into several files | Files |
| `pdf_rotate` | Rotate selected pages | File |
| `pdf_protect` | AES-256 encryption | File |

`convert_document` returns a file; `extract_text` returns text. The guidance that tells a model which to choose lives in `config/instructions.md`.

## Conversion planning

`registry.plan_conversion(src, dst, prefer_fidelity)` is the decision core. It searches the route graph for paths of up to `MAX_HOPS = 3` engine invocations and ranks candidates by:

1. **Quality**: the fidelity of the weakest hop (a path is only as good as its worst step).
2. **Hop count**: fewer is better.
3. **Intermediate-format preference**: `PIVOT_FORMATS` ranks intermediates by measured suitability. `md → docx → pdf` and `md → html → pdf` score identically on the first two criteria, but LibreOffice's HTML import handles tables and layout far worse than its DOCX import, so `docx` is preferred.

### Quality floor

Two rules govern lossy paths:

1. **Direct paths have no floor.** When the caller asks for `pdf → md`, a lossy result is what was requested. The floor constrains only multi-hop paths that the planner composed on its own; without this exception `extract_text` would stop working.
2. **Multi-hop paths must be at least `structural` when `prefer_fidelity` is set.** This rejects every path that silently passes through content extraction. The tool layer maps `allow_quality_loss` to `prefer_fidelity=not allow_quality_loss`.

`allow_quality_loss` defaults to `true` at the tool layer. The original default was the opposite (reject lossy paths so the user never receives a file that "succeeded" but contains only text), and was reversed after observation: a rejected `pdf → docx` led the model to detour through `pdf → md` and then guess `md → docx` parameters eight times over 2.5 minutes, reaching the same result the direct plan would have produced. The floor did not prevent the outcome; it only made it expensive. The current behaviour is "convert, and say clearly what was lost": every lossy hop is annotated in `Route.note`, `_fidelity_warning()` turns the annotations into a warning for the model, and the model relays it to the user. `allow_quality_loss=false` remains available for callers who state that layout must not be lost.

When the floor rejects a path, the error message names the lossy hop and the escape hatch (`allow_quality_loss=true`) instead of reporting a bare failure.

### Example plans

Plans computed with `allow_quality_loss=false` (`prefer_fidelity=True`):

| Request | Plan | Notes |
|---|---|---|
| `md → pdf` | `md →(pandoc)→ docx →(soffice)→ pdf` | `docx` chosen over `html` as intermediate (see pivot preference) |
| `md → pptx` | `md →(pandoc)→ pptx` | Pandoc direct; one slide per H2 |
| `xlsx → docx` | `xlsx →(soffice)→ html →(pandoc)→ docx` | |
| `csv → pdf` | `csv →(soffice)→ xlsx →(soffice)→ pdf` | |
| `doc → md` | `doc →(soffice)→ docx →(pandoc)→ md` | |
| `pdf → md` | `pdf →(anydoc)→ md` | Direct path; floor does not apply |
| `pdf → docx` | Rejected | Only path passes through content extraction; layout would be lost. Allowed with a warning when `allow_quality_loss=true` (the default) |
| `png → xlsx` | Rejected | No combination of engines reaches the target |

## Authentication

AnyDoc is an OAuth 2.1 resource server. It issues no tokens and stores no users. `src/auth/` configures FastMCP's `JWTVerifier` with `jwks_uri = <MCP_CENTER_URL>/.well-known/jwks.json`, the expected issuer and audience, and optional `required_scopes`; RS256 bearer tokens issued by [MCP Center](https://github.com/xianhong1208/MCP_Center) are verified offline against that JWKS. The server publishes `/.well-known/oauth-protected-resource/mcp` so OAuth-aware MCP clients can discover the authorisation server.

## Testing

Three layers with deliberately separate responsibilities:

```bash
uv run pytest                              # logic, < 1 s
uv run python scripts/sweep_routes.py      # engines, ~70 s

# Protocol check: start the server in another terminal first (config.test.yaml has auth disabled)
SERVER_PORT=5056 uv run python main.py --config config/config.test.yaml
uv run python scripts/mcp_smoke.py
```

| Layer | Question it answers | Notes |
|---|---|---|
| `pytest` | Is the logic right? | Engine availability is pinned with `patch.dict`. Otherwise the same suite passes on a developer machine with Pandoc installed and fails on a clean CI runner for reasons unrelated to the code under test. |
| `scripts/sweep_routes.py` | Do the engines actually produce valid output? | Executes every route the capability matrix advertises and validates output by magic bytes and content checks. Run after upgrading `firecrawl-anydoc`, Pandoc or LibreOffice, after changing the route table, and after deploying to a new environment. |
| `scripts/mcp_smoke.py` | Can a client really use the tools? | Drives six user scenarios (read a document, convert to PDF, extract pages, a rejected request, an explicit quality downgrade, a capability query) through a real MCP client. |

Each layer has caught a class of bug the others cannot:

- The route sweep found on its first run that six `→ rtf` routes produced RTF fragments without the `{\rtf1\ansi` header, which Word cannot open. Byte counts were plausible, no exception was raised, and every layer reported success; only structural validation of the output revealed it.
- The protocol smoke test found that every list-returning tool lacked `output_schema=None`, so clients received `Output validation error`. The tool functions returned normally, so unit tests structurally could not detect it.

### Validation status

Measured with Pandoc, LibreOffice (writer / calc / impress), Tesseract (chi_tra / eng) and Poppler all installed:

| Item | Result |
|---|---|
| Unit tests | All passing |
| Full route sweep | 162 / 162 |
| anydoc input formats | 13 / 13 (pdf, docx, doc, odt, rtf, epub, xlsx, xls, ods, pptx, ppt, odp, csv) |
| OCR routes (image → text) | 12 / 12; Chinese and English correct |
| Scanned-PDF automatic fallback | Pass |

Known quality gaps (conversion succeeds; result is imperfect):

- **Tables in `.ppt` (1997–2003 binary) are flattened to plain text**, not Markdown tables. The other twelve anydoc input formats preserve table structure; `.pptx` is unaffected.
- **Table recovery from PDF is heuristic** and depends on how the source drew the table. A PDF produced from DOCX usually recovers as a table; a complex layout may not.

## Known limits

### 50 MB per file

`convert.max_input_mb` in `config/config.yaml` (default 50) drives two limits from one setting:

```
convert.max_input_mb = 50
    ├── service-level check      50 MB    → over the limit: "file too large" plus guidance
    └── MCP transport body limit 75 MiB   → ×1.5, covering base64's 4/3 expansion and JSON overhead
```

The transport limit must exceed the service limit. Otherwise a large upload is rejected by the MCP SDK with a bare HTTP 413 before the service sees it; the model cannot interpret that response and retries (five consecutive retries were observed in logs). The SDK default is 4 MiB, which in practice accepts files of only about 3 MB. FastMCP does not expose this parameter, so `_raise_mcp_body_limit()` in `app.py` patches the default on the MCP SDK base class. The function can be removed once FastMCP exposes an official setting.

### 30 OCR pages

`MAX_OCR_PAGES` in `engines/ocr.py`. Longer scanned PDFs are truncated and the output says so; use `pdf_extract_pages` first to select the range that matters.

### PDF → editable formats is lossy

This is a property of PDF, not of the service. Whether a table inside a PDF can be recovered as a Markdown table is heuristic and depends on how it was drawn; heading hierarchy is usually flattened to body text.

### Images are kept, but not their positions

Extracted images are appended at the end of the Markdown; for PDF sources the page number is noted. The two source families use different pipelines:

| Source | Pipeline | Position information |
|---|---|---|
| PDF | pypdf extracts XObjects, de-duplicated by content hash | Does not exist: PDF has drawing coordinates, not reading order |
| docx / pptx / odt / epub | Assets from anydoc's `to_document()` | Exists but unused (see below) |

Non-PDF sources do know where an image belongs: in anydoc's document model an image is an inline element of `kind="image"` between the correct paragraphs, with `source.asset_id` pointing at `doc.assets[id]`. This is not used because `to_markdown()` drops images (keeping only alt text), and inserting them precisely would require reimplementing the whole Markdown serialiser (headings, tables, lists, footnotes, styles). Rewriting something that already works well for one feature is not worthwhile. If it is done later, the entry point is `Document.blocks`, not `to_markdown()`.

Images under 2 KB (`MIN_IMAGE_BYTES`) are treated as decorative (rules, icons, textures) and skipped; extraction stops at 40 images (`MAX_IMAGES`) or 15 MB in total (`MAX_TOTAL_IMAGE_BYTES`).

### One file per call, no `pdf_merge`

Hosts that inject uploaded files skip non-string parameters, so a `list[str]` of files can never be populated (see [artifact injection](#hosts-that-inject-uploaded-files-artifact-injection)). This is a structural constraint of the injection contract, not a design choice. The merge logic is implemented and tested (`pdfops.merge_pdfs`); a thin tool wrapper can be added once multi-file injection is available.

### firecrawl-anydoc maturity

`firecrawl-anydoc` is new (first released 2026-08; this project pins 0.1.8) and its API may still change. After upgrading, run `scripts/sweep_routes.py`, not only `pytest`. It performs one-way document → Markdown extraction only; file generation still relies on LibreOffice and Pandoc.

### `.xlsb` is not supported

anydoc's extension table maps `.xlsb` to its xlsx handler, but XLSB stores BIFF12 binary (`xl/workbook.bin`) rather than XML, and a synthesised BIFF12 container is rejected with `MalformedError`. `_EXT_ALIASES` therefore deliberately excludes it. LibreOffice cannot export XLSB, so the behaviour could not be confirmed with a real file; before adding support, verify with an `.xlsb` produced by Excel rather than trusting the extension mapping. By contrast `.docm`, `.xlsm`, `.pptm` and `.ppsx` have been verified and are in the alias table.
