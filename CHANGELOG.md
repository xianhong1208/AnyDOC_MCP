# Changelog

All notable changes to Morphyard (formerly AnyDoc) are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Docs
- Docs: roadmap and README rewritten. A new `ROADMAP.md` / `ROADMAP.zh-TW.md` lists what shipped and the plan to 1.8, with a GitHub milestone per version. Both READMEs follow a shorter layout (badges, an example tool call from a real run, quick start, deployment options); the tool and format tables, known limits, configuration (with the renamed variables) and revocation-feed setup, and testing moved to `docs/tools`, `docs/configuration` and `docs/development` (English and Traditional Chinese). The local-experiment command no longer points at a `config/config.test.yaml` that does not exist.

### Changed
- Renamed from AnyDoc to Morphyard; environment variables are now `MORPHYARD_*` (the `ANYDOC_*` names are no longer read): `MORPHYARD_BASE_URL`, `MORPHYARD_REVOCATION_ENABLED`, `MORPHYARD_REVOCATION_CLIENT_ID`, `MORPHYARD_REVOCATION_CLIENT_SECRET`, `MORPHYARD_REVOCATION_POLL_INTERVAL`, `MORPHYARD_REVOCATION_FAIL_MODE`. The Python project is `morphyard`, MCP serverInfo reports `Morphyard`, and the config section and tool module are `morphyard` / `src.fastmcp_tools.morphyard_tools`. The document → Markdown engine is now reported as `firecrawl` (it was `anydoc`, after the firecrawl-anydoc library it wraps). Port 5055 and the token audience `<base_url>/mcp` are unchanged.
- `MCP_CENTER_URL`, MCP Gate's name before it was renamed, is no longer read as a fallback for `MCP_GATE_URL`; set `MCP_GATE_URL` instead.

## [1.2.1] — 2026-11-09

### Security
- LibreOffice conversions read files and fetched URLs a document linked to: an HTML `<img src="/usr/share/…">` or a DOCX image linked to `file:///…` came back embedded in the PDF, and an `<img>` or stylesheet naming `http://…` made AnyDoc request it (anything the server can reach). Every LibreOffice conversion now runs with a profile that blocks links from untrusted documents (`BlockUntrustedRefererLinks`), sends HTTP, HTTPS and FTP to an unreachable proxy (`proxy.invalid`, also set as `*_proxy` in soffice's environment) and never runs macros or updates links. Calc and Impress still loaded ODF images linked by path, and the RTF importer still read `INCLUDEPICTURE` files, despite the block, so documents are also stripped of outside references before LibreOffice opens them: external relationships in OOXML packages, `xlink:href`s (and `xml:base`, `form:image-data`) that do not point into the package in ODF, and linking fields (`INCLUDEPICTURE`, `INCLUDETEXT`, `IMPORT`, `LINK`, `DDE`, …) in RTF, whose stored result is shown instead. This goes by content, not by the declared format, because LibreOffice detects the type itself. Hyperlinks and images stored in the document are kept. A zip-based document that cannot be read far enough to check it (broken zip, malformed XML, an XML entity declaration) is now refused with a conversion error rather than passed on. Images linked by path or URL are no longer embedded; the same call as Pandoc's in 1.2.0.

## [1.2.0] — 2026-11-09

### Added
- **Continuous integration** (`.github/workflows/ci.yml`): ruff and pytest on every push and pull request to `dev` and `main`, and a job that builds the Docker image, starts it, and runs `scripts/sweep_routes.py --strict` and `scripts/mcp_smoke.py` inside the container against the engines and fonts that ship.

### Fixed
- `scripts/sweep_routes.py` exited 0 when routes failed, so nothing scripted could notice; it now exits 1, and with `--strict` also when a route was skipped for a missing engine.
- The Docker image's `CMD` ran `uv run`, which syncs the default dependency groups: every new container downloaded pytest and ruff before starting, or failed to start without network. It now runs `uv run --no-sync`.

### Security
- Pandoc conversions could read any file the service can, and fetch URLs: a `.tex` upload with `\lstinputlisting{/path}` came back with that file's contents, and an image naming a local path or URL was pulled into a DOCX, ODT, PPTX, RTF or EPUB. A conversion now reads the document with `--sandbox` into pandoc's AST, then writes from an AST holding no file references: an image stays only when it is embedded in the source or a `data:` URI (otherwise it becomes its alt text), metadata is limited to title, author, date and similar fields (the EPUB writer opens `cover-image`, `css` and `stylesheet`), and raw HTML is dropped from EPUB output. The whole conversion cannot run with `--sandbox`: Debian's pandoc reads its DOCX/PPTX/EPUB/LaTeX templates from disk, which the sandbox blocks. A conversion now takes about 65 ms longer (two pandoc runs).

## [1.1.1] — 2026-11-09

### Fixed
- AnyDoc reported **1.0.0** in `/openapi.json`, the landing page and MCP serverInfo after the 1.1.0 release: the version came from `app.version` in `config/config.yaml`, which the release did not bump. The version is now read from `pyproject.toml` (`src/version.py`), the one place it is written; `app.version` in a config is ignored.

## [1.1.0] — 2026-11-05

### Added
- **MCP Gate revocation feed** (opt-in: `ANYDOC_REVOCATION_ENABLED`, off by default). AnyDoc polls MCP Gate's `POST /oauth/revoked` with a confidential client bound to this server and rejects a revoked token within one poll interval (30 s by default) instead of at its expiry. Fail-open by default; feed health is reported under `revocation` in `GET /health`. See *Token revocation* in the README.
- The issuer is read from `MCP_GATE_URL` (`MCP_CENTER_URL` still works as a fallback).

### Changed
- **Renamed MCP Center to MCP Gate** in docs, configuration and identifiers.
- **fastmcp 4.** The OAuth discovery documents advertise the issuer exactly as configured.
- Converted files are labelled with their registered MIME types.

### Fixed
- `ENGINE_TIMEOUT` now applies to every conversion engine, so a hung engine can no longer block a request indefinitely.

### Security
- **Dependency advisories fixed: pip-audit 96 → 0.** pyjwt 2.13.0 → 2.15.1, authlib 1.7.2 → 1.9.4, joserfc 1.7.4 → 1.8.0, pypdf 6.15.0 → 6.21.4, dnspython 2.8.0 → 2.9.2, opentelemetry-api 1.44.0 → 1.45.2, and pillow 12.3.0 → 13.0.0 (major version; the floor in `pyproject.toml` is now `>=13.0.0`).

## [1.0.0] — 2026-09-10

First release: a document conversion MCP server (Office ↔ PDF, Markdown ↔ Word, spreadsheets, presentations, images, OCR, PDF split / extract / rotate / encrypt) secured by MCP Gate OAuth 2.1.

[Unreleased]: https://github.com/xianhong1208/morphyard/compare/v1.2.1...dev
[1.2.1]: https://github.com/xianhong1208/morphyard/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/xianhong1208/morphyard/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/xianhong1208/morphyard/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/xianhong1208/morphyard/releases/tag/v1.1.0
[1.0.0]: https://github.com/xianhong1208/morphyard/releases/tag/v1.0.0
