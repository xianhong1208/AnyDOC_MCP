"""App factory: assemble FastAPI + FastMCP.

This file only wires things together; behaviour lives in the individual modules.
Add features in those modules, not here.
"""
import importlib
from pathlib import Path

from fastapi import FastAPI
from fastmcp import FastMCP
from pydantic import BaseModel

from src.auth import build_auth_provider
from src.config.config_manager import Config
from src.log import get_api_logger
from src.middleware.request_id import RequestIdMiddleware
from src.middleware.request_logging import RequestLogMiddleware

api_logger = get_api_logger()

_INSTRUCTIONS_PATH = Path(__file__).parent / "config" / "instructions.md"


def _load_instructions(path: Path = _INSTRUCTIONS_PATH) -> str:
    """Load the MCP `instructions` text from a Markdown file."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        api_logger.warning(f"Instructions file not found: {path}, using default")
        return "MCP Service"


def _apply_convert_config(config: BaseModel) -> None:
    """Push `config.convert` into the modules that own the corresponding constants.

    These used to be three hard-coded constants, which made the `convert:` section of
    config.yaml silently ineffective - the hardest kind of configuration bug to notice.
    """
    convert = getattr(config, "convert", None)
    if convert is None:
        return

    from src.domain.convert import registry, service

    service.MAX_INPUT_MB = convert.max_input_mb
    registry.MAX_HOPS = convert.max_hops
    api_logger.info(
        f"Convert config: max_input={convert.max_input_mb}MB, "
        f"max_hops={convert.max_hops}, engine_timeout={convert.engine_timeout_sec}s"
    )


def _raise_mcp_body_limit(max_input_mb: int) -> None:
    """Raise the MCP streamable-HTTP request body limit.

    The MCP SDK transport accepts only 4 MiB by default (`DEFAULT_MAX_REQUEST_BODY_SIZE`)
    and answers a bare 413 before parsing anything larger. Base64 inflates files by 4/3, so
    the default effectively caps uploads at ~3 MB - an order of magnitude below the 50 MB
    this service advertises.

    Why a monkey patch: FastMCP's `http_app()` does not expose the parameter, its session
    manager subclass does not forward it, and the manager is only constructed inside the
    lifespan, so there is no instance to configure. Adding a default in the base class
    `__init__` is the least invasive option.

    The transport limit is deliberately set above the service-level check (x1.5 covers the
    base64 overhead plus JSON framing) so that "file too large" is reported by the service
    with a helpful message rather than by the transport as a bare 413 - which an AI client
    cannot interpret and will simply retry.
    """
    limit_bytes = int(max_input_mb * 1.5 * 1024 * 1024)

    from mcp.server import streamable_http_manager as shm

    original = shm.StreamableHTTPSessionManager.__init__
    if getattr(original, "_anydoc_patched", False):
        return

    def patched(self, *args, **kwargs):
        kwargs.setdefault("max_request_body_size", limit_bytes)
        original(self, *args, **kwargs)

    patched._anydoc_patched = True
    shm.StreamableHTTPSessionManager.__init__ = patched
    api_logger.info(
        f"MCP request body limit raised to {limit_bytes / 1024 / 1024:.0f} MiB "
        f"(default is 4 MiB, which caps uploads at ~3MB after base64)"
    )


def create_app(config: BaseModel, transport: str):
    """Build and configure the combined FastAPI + FastMCP application."""
    if not isinstance(config, BaseModel):
        raise RuntimeError("create_app requires a Pydantic ConfigModel instance.")

    # --- App metadata ---
    app_config = getattr(config, 'app', None)
    app_name = getattr(app_config, 'name', 'AnyDoc')
    app_version = getattr(app_config, 'version', '0.0.0')
    app_title = getattr(app_config, 'title', 'AnyDoc')
    app_description = getattr(app_config, 'description', '')
    instructions = _load_instructions()

    # --- Conversion settings (must run before the MCP app is built: the body limit
    #     affects transport construction) ---
    _apply_convert_config(config)
    _raise_mcp_body_limit(getattr(getattr(config, 'convert', None), 'max_input_mb', 50))

    # --- MCP instance. FastMCP wraps the /mcp endpoint with its own bearer-token check and
    #     publishes /.well-known/oauth-protected-resource when an auth provider is set. ---
    auth = build_auth_provider(config)
    mcp = FastMCP(name=app_name, version=app_version, instructions=instructions, auth=auth)
    mcp_app = mcp.http_app(transport=transport)

    # --- FastAPI instance ---
    fastapi_app = FastAPI(
        title=app_title,
        description=app_description,
        version=app_version,
        lifespan=mcp_app.lifespan,
    )
    fastapi_app.state.config = config
    fastapi_app.state.instructions = instructions

    # --- Load modules ---
    modules_conf = getattr(config, 'modules', None)
    if modules_conf is None:
        raise RuntimeError("Modules configuration is missing.")

    for module_name in (modules_conf.enabled or []):
        module_config = Config.get_module_model(module_name)
        if not module_config:
            api_logger.warning(f"No configuration found for module '{module_name}'")
            continue

        try:
            for attr, label in [
                ('api_router', 'API router'),
                ('mcp_tools', 'MCP tools'),
                ('mcp_prompts', 'MCP prompts'),
                ('mcp_resources', 'MCP resources'),
            ]:
                cfg = getattr(module_config, attr, None)
                if not cfg:
                    continue
                module_path = getattr(cfg, 'module', None)
                func_name = (
                    getattr(cfg, 'router_name', None) if attr == 'api_router'
                    else getattr(cfg, 'function_name', None)
                )
                if not module_path or not func_name:
                    continue
                try:
                    mod = importlib.import_module(module_path)
                    obj = getattr(mod, func_name)
                    if attr == 'api_router':
                        fastapi_app.include_router(obj, prefix=getattr(cfg, 'prefix', None))
                    else:
                        obj(mcp)
                    api_logger.info(f"{label} loaded: {module_name}")
                except Exception as e:
                    api_logger.error(f"Failed to load {label} {module_name}: {e}")
        except Exception as e:
            api_logger.error(f"Error loading module '{module_name}': {e}")

    # --- Health ---
    try:
        from src.api.router.health import router as health_router
        fastapi_app.include_router(health_router)
    except Exception as e:
        api_logger.error(f"Failed to load health router: {e}")

    # --- Index (landing page at /) ---
    try:
        from src.api.router.index import router as index_router
        fastapi_app.include_router(index_router)
    except Exception as e:
        api_logger.error(f"Failed to load index router: {e}")

    # --- Error handlers ---
    try:
        from src.middleware.error_handler import register_error_handlers
        register_error_handlers(fastapi_app)
    except Exception as e:
        api_logger.error(f"Failed to register error handlers: {e}")

    # --- Mount the MCP app last. Mounting (rather than copying its routes) keeps FastMCP's own
    #     middleware stack, which is where bearer tokens are verified and the request context
    #     for tools is set up. Starlette matches routes in order, so the FastAPI routes above
    #     (/, /health, /docs) still win; everything else (/mcp, /.well-known/*) goes to FastMCP. ---
    fastapi_app.mount("/", mcp_app)

    # --- Middleware stack (outermost runs first) ---
    log_wrapped = RequestLogMiddleware(app=fastapi_app)
    return RequestIdMiddleware(app=log_wrapped)
