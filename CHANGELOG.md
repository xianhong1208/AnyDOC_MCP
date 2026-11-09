# Changelog

All notable changes to AnyDoc are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/xianhong1208/AnyDOC_MCP/compare/v1.1.1...dev
[1.1.1]: https://github.com/xianhong1208/AnyDOC_MCP/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/xianhong1208/AnyDOC_MCP/releases/tag/v1.1.0
[1.0.0]: https://github.com/xianhong1208/AnyDOC_MCP/releases/tag/v1.0.0
