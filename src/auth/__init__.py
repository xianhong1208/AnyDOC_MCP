"""Resource-server side of OAuth 2.1: verify MCP Center access tokens.

AnyDoc never issues tokens. It trusts one authorization server (MCP Center), fetches its
JWKS once, and verifies every bearer token offline. FastMCP's `RemoteAuthProvider` also
publishes `/.well-known/oauth-protected-resource/mcp`, which is how OAuth-capable MCP clients
discover where to sign in.
"""

from dataclasses import dataclass, field

from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

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
        raise RuntimeError("auth.issuer is required when auth.enabled is true (set MCP_CENTER_URL)")
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
        token_verifier=JWTVerifier(
            jwks_uri=settings.jwks_uri,
            issuer=settings.issuer,
            audience=settings.audience,
            required_scopes=settings.required_scopes,
        ),
        authorization_servers=[AnyHttpUrl(settings.issuer)],
        base_url=settings.base_url,
    )
    logger.info(f"Authentication enabled: issuer={settings.issuer} audience={settings.audience}")
    return provider
