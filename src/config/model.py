"""Pydantic configuration models.

Defines the Pydantic model for every config section. To add a custom section, define
its model here and add it to ConfigModel.
"""


from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ServerConfig(BaseModel):
    """Server settings."""
    host: str
    port: int
    transport: str = "http"


class RevocationConfig(BaseModel):
    """MCP Gate's revocation feed (`auth.revocation`).

    AnyDoc verifies tokens offline, so without this a token revoked in MCP Gate keeps
    working until it expires. When enabled, a background task polls
    `<issuer>/oauth/revoked` and the /mcp verifier rejects listed tokens. The
    credentials belong to a confidential client registered in the MCP Gate console and
    bound to this server as its resource server.
    """
    enabled: bool = False
    client_id: str | None = None
    # Must be an ${ENV_VAR} reference; a literal secret in config.yaml is refused at startup.
    client_secret: str | None = Field(None, repr=False)
    poll_interval_sec: float = Field(30, ge=5, le=3600)
    # "open": keep accepting tokens when the feed is unreachable (logged loudly);
    # "closed": reject every token once no poll has succeeded for
    # stale_after_intervals x poll_interval_sec.
    fail_mode: Literal["open", "closed"] = "open"
    stale_after_intervals: int = Field(4, ge=2, le=100)


class AuthConfig(BaseModel):
    """Bearer-token verification against an OAuth 2.1 authorization server (MCP Gate).

    Tokens are RS256 JWTs verified offline through the issuer's JWKS; no per-request
    network call is needed once the keys are cached. `audience` must match the resource
    URI registered for this server in MCP Gate (by default its MCP URL).
    """
    enabled: bool = True
    issuer: str | None = None
    audience: str | None = None
    base_url: str | None = None
    required_scopes: list[str] = []
    # MCP Gate revocation feed (off by default; needs a resource-server client).
    revocation: RevocationConfig = Field(default_factory=RevocationConfig)


class LoggingConfig(BaseModel):
    """Logging settings."""
    level: str = "INFO"
    format: str = "colorized"
    file: str | None = None
    max_size: str = "10MB"
    backup_count: int = 3
    log_dir: str = "logs"


class ConvertConfig(BaseModel):
    """Conversion settings.

    max_input_mb drives two things at once: the file-size check in the service layer
    and the request-body limit of the MCP streamable HTTP transport (see
    _raise_mcp_body_limit in app.py). They must move together: change only one and
    either the transport rejects the file with a bare 413 (the AI never sees a useful
    message) or the service-layer check is never reached.
    """
    max_input_mb: int = 50
    # Seconds allowed per engine call. None (ENGINE_TIMEOUT unset) keeps each engine's own
    # default: LibreOffice 180, Pandoc 120, OCR 300. A value applies to every engine.
    engine_timeout_sec: Optional[int] = Field(default=None, gt=0)
    max_hops: int = 3

    @field_validator("engine_timeout_sec", mode="before")
    @classmethod
    def _blank_timeout_is_unset(cls, v):
        # config.yaml writes ${ENGINE_TIMEOUT:-}, which arrives as "" when the variable is unset
        return None if v is None or (isinstance(v, str) and not v.strip()) else v


class AppConfig(BaseModel):
    """Application metadata."""
    name: str = "MyMCP"
    # Ignored: the reported version comes from pyproject.toml (src/version.py).
    # Kept so an older config that still sets it loads without error.
    version: str | None = None
    title: str = "My MCP Service"
    description: str = ""


class RouterConfig(BaseModel):
    """API router settings."""
    module: str
    router_name: str
    prefix: str


class McpToolsConfig(BaseModel):
    """MCP tools settings."""
    module: str
    function_name: str


class McpPromptsConfig(BaseModel):
    """MCP prompts settings."""
    module: str
    function_name: str


class McpResourcesConfig(BaseModel):
    """MCP resources settings."""
    module: str
    function_name: str


class ModuleConfig(BaseModel):
    """Settings for a single module."""
    model_config = ConfigDict(extra="allow")

    api_router: RouterConfig | None = None
    mcp_tools: McpToolsConfig | None = None
    mcp_prompts: McpPromptsConfig | None = None
    mcp_resources: McpResourcesConfig | None = None


class ModulesConfig(BaseModel):
    """Top-level modules settings."""
    model_config = ConfigDict(extra="allow")

    enabled: list[str]


class ConfigModel(BaseModel):
    """Complete configuration model.

    To add a config section:
    1. Define its Pydantic model above
    2. Add a field here
    3. Add the matching YAML section to config.yaml
    """
    server: ServerConfig
    auth: AuthConfig = AuthConfig()
    logging: LoggingConfig
    app: AppConfig | None = None
    convert: ConvertConfig = ConvertConfig()
    modules: ModulesConfig


__all__ = [
    "AppConfig",
    "AuthConfig",
    "ConfigModel",
    "ConvertConfig",
    "LoggingConfig",
    "McpPromptsConfig",
    "McpResourcesConfig",
    "McpToolsConfig",
    "ModuleConfig",
    "ModulesConfig",
    "RouterConfig",
    "ServerConfig",
]
