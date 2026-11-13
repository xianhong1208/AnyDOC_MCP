# Roadmap

[繁體中文](ROADMAP.zh-TW.md)

Where Morphyard is going after 1.2. Today it is a document-conversion MCP server; over the next
versions it grows into a general file converter: a full PDF toolkit, multi-file jobs, a web app
with a page for every tool, then images, audio, video and more formats. Each line names what
users get, not how it is built. The order was agreed on 2026-11-13. Each version has a
[milestone](https://github.com/xianhong1208/morphyard/milestones) with one issue per item.

**Ground rules.** Morphyard stays MIT-licensed, so it uses no AGPL components: no PyMuPDF and no
Ghostscript. PDF compression uses qpdf/pikepdf, and OCR uses OCRmyPDF's path that needs no
Ghostscript. FFmpeg is an LGPL build, run as a separate program.

## Shipped

**1.0.0 (2026-09-10), the first release.** A document-conversion MCP server: Office ↔ PDF,
Markdown ↔ Word, spreadsheets, presentations, images, OCR of scanned pages, and PDF split,
extract, rotate and encrypt, as eight MCP tools. Sign-in through MCP Gate (OAuth 2.1).

**1.1.0 (2026-11-05), revocation.** A token revoked in MCP Gate stops working within one poll
interval (30 s by default) instead of at its expiry, through the opt-in revocation feed. MCP
Center was renamed MCP Gate, fastmcp 4, and every known dependency advisory fixed.

**1.1.1 (2026-11-09).** Morphyard reports its real version in `/openapi.json`, the landing page and
MCP serverInfo.

**1.2.0 (2026-11-09), safe Pandoc and CI.** Pandoc conversions can no longer read local files or
fetch URLs a document points at. Every pull request runs lint, unit tests, and every advertised
conversion against the real engines inside the Docker image.

**1.2.1 (2026-11-09), safe LibreOffice.** LibreOffice conversions can no longer read local files
or fetch URLs a document links to, and never run macros.

**1.2.2 (2026-11-13), new name, steady under load.** Renamed from AnyDoc to Morphyard; settings are read from `MORPHYARD_*` variables. LibreOffice profiles are deleted after every conversion, and stale ones are swept at startup. Conversions beyond a set number wait in a queue instead of all running at once.

See [CHANGELOG.md](CHANGELOG.md) for the details.

## Next

| Version | What you get |
|---|---|
| **1.3: PDF toolkit** | **Merge** PDFs; add a text or image **watermark**; **delete and reorder** pages; **compress**; add **page numbers, a header and a footer**; **unlock** a PDF and set its **permissions**; **read or strip metadata**; turn **PDF pages into images** and **images into a PDF**. |
| **1.4: bigger jobs** | Results come back as **download links** that expire, instead of large base64 in the reply, so big files no longer flood the model's context. Long conversions run as **jobs with progress**. Files can be given **by URL**, with a guard that refuses internal addresses. **Several files in one call.** A **pipeline** tool chains steps, e.g. merge, then compress, then watermark. |
| **1.5: web app** | A **page for every tool** in the browser: drop files, pick options, download the result. **Sign-in through MCP Gate**, which can be switched off for use on a trusted LAN. In **English and Traditional Chinese**. |
| **1.6: what agents ask for most** | **Searchable PDFs** from scans (OCR text layer). **Tables in a PDF to XLSX or CSV.** **Fill in and flatten PDF forms.** **HEIC and AVIF** images, and image **resize, compress and EXIF removal**. **Sanitise** a document (scripts, embedded files, links). **Repair** a damaged PDF. |
| **1.7: audio and video** | **Convert and compress** audio and video, **trim** them, **extract the audio** from a video, make a **GIF** from a clip, and convert **subtitles** between SRT, VTT and ASS. |
| **1.8: more formats** | **HTML to PDF** with full page layout; **emails** (EML, MSG); **e-books** (through Calibre); **data formats**; **archives**; **SVG**. |

## Later

No version yet:

- **Redaction**: remove text and areas from a PDF for good.
- **Digital signatures** on PDFs.
- **PDF/A** for archiving.
- **CAD** drawings.
- **3D** models.
- **Fonts.**
- **Geodata.**
- **HWP** (Hangul word processor) documents.
