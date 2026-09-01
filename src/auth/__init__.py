"""Resource-server side of OAuth 2.1: verify MCP Center access tokens.

AnyDoc never issues tokens. It trusts one authorization server (MCP Center), fetches its
JWKS once, and verifies every bearer token offline. FastMCP's `RemoteAuthProvider` also
publishes `/.well-known/oauth-protected-resource/mcp`, which is how OAuth-capable MCP clients
discover where to sign in.
"""

from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

from src.log import get_api_logger

logger = get_api_logger()


def build_auth_provider(config) -> RemoteAuthProvider | None:
    """Return the FastMCP auth provider for `config.auth`, or None when auth is disabled."""
    auth = getattr(config, "auth", None)
    if auth is None or not auth.enabled:
        logger.warning("Authentication is DISABLED - every client can call the tools")
        return None
    if not auth.issuer:
        raise RuntimeError("auth.issuer is required when auth.enabled is true (set MCP_CENTER_URL)")

    issuer = auth.issuer.rstrip("/")
    server = config.server
    base_url = (auth.base_url or f"http://{server.host}:{server.port}").rstrip("/")
    audience = auth.audience or f"{base_url}/mcp"

    provider = RemoteAuthProvider(
        token_verifier=JWTVerifier(
            jwks_uri=f"{issuer}/.well-known/jwks.json",
            issuer=issuer,
            audience=audience,
            required_scopes=list(auth.required_scopes or []),
        ),
        authorization_servers=[AnyHttpUrl(issuer)],
        base_url=base_url,
    )
    logger.info(f"Authentication enabled: issuer={issuer} audience={audience}")
    return provider
