"""Pydantic configuration models.

Defines the Pydantic model for every config section. To add a custom section, define
its model here and add it to ConfigModel.
"""


from pydantic import BaseModel, ConfigDict


class ServerConfig(BaseModel):
    """Server settings."""
    host: str
    port: int
    transport: str = "http"


class AuthConfig(BaseModel):
    """Bearer-token verification against an OAuth 2.1 authorization server (MCP Center).

    Tokens are RS256 JWTs verified offline through the issuer's JWKS; no per-request
    network call is needed once the keys are cached. `audience` must match the resource
    URI registered for this server in MCP Center (by default its MCP URL).
    """
    enabled: bool = True
    issuer: str | None = None
    audience: str | None = None
    base_url: str | None = None
    required_scopes: list[str] = []


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
    engine_timeout_sec: int = 180
    max_hops: int = 3


class AppConfig(BaseModel):
    """Application metadata."""
    name: str = "MyMCP"
    version: str = "0.0.0"
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
