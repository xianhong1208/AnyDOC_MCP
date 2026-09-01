"""OAuth discovery endpoints for clients that predate path-specific resource metadata.

FastMCP publishes RFC 9728 metadata at `/.well-known/oauth-protected-resource/mcp` only. Many MCP
clients (and the 2025-03-26 revision of the MCP authorization spec) instead probe:

- `/.well-known/oauth-protected-resource`          (root form of RFC 9728)
- `/.well-known/oauth-authorization-server`        (RFC 8414 metadata on the MCP server's own origin)
- `/.well-known/openid-configuration`

Without these they report "cannot discover OAuth metadata". The first is served here directly; the
other two are fetched from the authorization server (MCP Center), cached briefly and re-served, so
the client sees the real issuer, endpoints and JWKS URI.
"""

import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from src.auth import AuthSettings
from src.log import get_api_logger

logger = get_api_logger()
router = APIRouter(tags=["OAuth discovery"], include_in_schema=False)

_CACHE_TTL_SEC = 300
_cache: dict[str, tuple[float, dict]] = {}


def _settings(request: Request) -> AuthSettings:
    settings = getattr(request.app.state, "auth_settings", None)
    if settings is None:
        raise HTTPException(status_code=404, detail="authentication is disabled")
    return settings


def protected_resource_metadata(settings: AuthSettings) -> dict:
    """RFC 9728 document, identical to the path-specific one FastMCP serves."""
    return {
        "resource": settings.audience,
        "authorization_servers": [settings.issuer],
        "scopes_supported": settings.required_scopes,
        "bearer_methods_supported": ["header"],
    }


async def fetch_issuer_document(settings: AuthSettings, well_known: str) -> dict:
    """Fetch `<issuer>/.well-known/<name>` from MCP Center with a short cache."""
    url = f"{settings.issuer}/.well-known/{well_known}"
    cached = _cache.get(url)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            doc = r.json()
    except Exception as e:  # noqa: BLE001 - any failure means the AS is unreachable
        logger.warning(f"Could not fetch {url}: {e}")
        raise HTTPException(status_code=502, detail=f"authorization server unreachable: {url}") from e
    _cache[url] = (time.monotonic() + _CACHE_TTL_SEC, doc)
    return doc


def clear_cache() -> None:
    _cache.clear()


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource(request: Request) -> JSONResponse:
    return JSONResponse(protected_resource_metadata(_settings(request)))


@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server(request: Request) -> JSONResponse:
    return JSONResponse(await fetch_issuer_document(_settings(request), "oauth-authorization-server"))


@router.get("/.well-known/openid-configuration")
async def openid_configuration(request: Request) -> JSONResponse:
    return JSONResponse(await fetch_issuer_document(_settings(request), "openid-configuration"))
