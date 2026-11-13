"""Resource-server side of OAuth 2.1: verify MCP Gate access tokens.

Morphyard never issues tokens. It trusts one authorization server (MCP Gate), fetches its
JWKS once, and verifies every bearer token offline. FastMCP's `RemoteAuthProvider` also
publishes `/.well-known/oauth-protected-resource/mcp`, which is how OAuth-capable MCP clients
discover where to sign in.

With `auth.revocation` enabled the verifier also consults MCP Gate's revocation feed
(`src/auth/revocation.py`), so a token revoked in MCP Gate stops working here within a poll
interval instead of at its expiry.
"""

from dataclasses import dataclass, field

from fastmcp.server.auth import RemoteAuthProvider
from src.auth.fastmcp_verifier import RevocationAwareJWTVerifier
from src.auth.revocation import (
    RevocationList,
    build_revocation_list,
    check_revoked,
    get_revocation_list,
    revocation_lifespan,
    revocation_status,
    set_revocation_list,
)
from src.log import get_api_logger

logger = get_api_logger()


@dataclass(frozen=True)
class AuthSettings:
    """Resolved authentication settings (issuer, audience and public base URL)."""
    issuer: str
    base_url: str
    audience: str
    required_scopes: list[str] = field(default_factory=list)

    @property
    def jwks_uri(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"


def resolve_auth_settings(config) -> AuthSettings | None:
    """Return the effective auth settings for `config.auth`, or None when auth is disabled."""
    auth = getattr(config, "auth", None)
    if auth is None or not auth.enabled:
        return None
    if not auth.issuer:
        raise RuntimeError("auth.issuer is required when auth.enabled is true (set MCP_GATE_URL)")
    server = config.server
    base_url = (auth.base_url or f"http://{server.host}:{server.port}").rstrip("/")
    return AuthSettings(
        issuer=auth.issuer.rstrip("/"),
        base_url=base_url,
        audience=auth.audience or f"{base_url}/mcp",
        required_scopes=list(auth.required_scopes or []),
    )


def build_auth_provider(config) -> RemoteAuthProvider | None:
    """Return the FastMCP auth provider for `config.auth`, or None when auth is disabled."""
    settings = resolve_auth_settings(config)
    if settings is None:
        logger.warning("Authentication is DISABLED - every client can call the tools")
        return None

    provider = RemoteAuthProvider(
        # JWTVerifier + the revocation list (a no-op while auth.revocation is disabled).
        token_verifier=RevocationAwareJWTVerifier(
            jwks_uri=settings.jwks_uri,
            issuer=settings.issuer,
            audience=settings.audience,
            required_scopes=settings.required_scopes,
        ),
        # A plain string is published as-is; AnyHttpUrl would add a trailing slash that mcp 2 clients
        # reject as an issuer mismatch (RFC 8414 3.3 compares issuers as exact strings).
        authorization_servers=[settings.issuer],
        base_url=settings.base_url,
    )
    logger.info(f"Authentication enabled: issuer={settings.issuer} audience={settings.audience}")
    return provider


def init_revocation(config, env_refs=None) -> RevocationList | None:
    """Build (or clear) the revocation list for `config` and say once whether it is on."""
    settings = resolve_auth_settings(config)
    rl = build_revocation_list(config, issuer=settings.issuer if settings else None,
                               env_refs=env_refs)
    set_revocation_list(rl)
    if settings is None:
        return None
    if rl is None:
        logger.warning(
            "MCP Gate revocation feed: DISABLED (auth.revocation.enabled: false) - a token "
            "revoked in MCP Gate stays valid here until it expires")
        return None
    s = rl.settings
    logger.info(
        f"MCP Gate revocation feed: ENABLED (feed: {s.feed_url}, client: {s.client_id}, "
        f"poll every {s.poll_interval:g}s, fail_mode: {s.fail_mode})")
    return rl


__all__ = [
    "AuthSettings",
    "RevocationList",
    "build_auth_provider",
    "check_revoked",
    "get_revocation_list",
    "init_revocation",
    "resolve_auth_settings",
    "revocation_lifespan",
    "revocation_status",
]
