#!/usr/bin/env python3
"""
Log configuration module.

Features:
- Console output (colorized, written to stderr)
- Per-module log files (adapter, api, auth, db, mcptools, server)
- Automatic request ID injection (via contextvars, links the whole request chain)
- Structured log helpers (uniform format, easy to grep)
- HTTP request logging (FastAPI and FastMCP)

Troubleshooting:
    # Follow the full chain of one request
    grep "rid=abc12345" logs/*/info_*.log

    # All errors of one operation
    grep "INDEX_FAIL" logs/*/error_*.log

    # All operations of one token
    grep "tok=abcd1234" logs/*/info_*.log
"""

import json
import os
import sys
import uuid
from contextvars import ContextVar
from typing import Any, Literal

from loguru import logger

# fastmcp / starlette are deliberately NOT imported at module level.
#
# This logger is used by every module under src/domain/, so a module-level import
# would tie the whole conversion core to the MCP framework through the logger — a
# non-HTTP caller would still pull fastmcp, uvicorn, starlette and opentelemetry in.
#
# Only log_request_info() actually needs them, so it imports lazily. This also lets
# the conversion core run standalone without any web framework.


# ============================================================================
# Request ID (generated per HTTP request, carried through every layer via contextvars)
# ============================================================================

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

def get_request_id() -> str:
    """Return the current request_id (callable from any layer)."""
    return _request_id_var.get()

def set_request_id(rid: str) -> None:
    """Set the current request_id (called by the middleware)."""
    _request_id_var.set(rid)

def generate_request_id() -> str:
    """Generate a short request_id (first 8 hex chars of a UUID; unique enough per day)."""
    return uuid.uuid4().hex[:8]


# ============================================================================
# Module definitions
# ============================================================================

LogModule = Literal["adapter", "api", "auth", "db", "mcptools", "server"]
LOG_MODULES: list[LogModule] = ["adapter", "api", "auth", "db", "mcptools", "server"]


# ============================================================================
# Logger Setup
# ============================================================================

def _format_file_log(record) -> str:
    """File log format: request_id is appended automatically."""
    rid = _request_id_var.get()
    return (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
        f"rid={rid} | "
        "{function}:{line} | {message}\n"
    )

def _format_console_log(record) -> str:
    """Console log format: colorized, with request_id."""
    rid = _request_id_var.get()
    return (
        "<green>{time:HH:mm:ss.SSS}</green> | <level>{level:<8}</level> | "
        f"<dim>rid={rid}</dim> | "
        "<cyan>[{extra[module]}]</cyan> | {message}\n"
    )


def setup_logger(
    console_level: str = "INFO",
    file_level: str = "DEBUG",
    log_base_dir: str = "logs",
    rotation: str = "5 MB",
    encoding: str = "utf-8"
) -> None:
    """Configure the loguru logging system.

    Creates one log directory per module with separate info/error files.
    Every record carries the request_id so a request chain can be followed.

    Layout:
        logs/adapter/info_2026-03-20.log   <- DEBUG/INFO/WARNING
        logs/adapter/error_2026-03-20.log  <- ERROR/CRITICAL
        logs/api/info_2026-03-20.log
        ...

    Args:
        console_level: Console log level.
        file_level: File log level.
        log_base_dir: Base directory for log files.
        rotation: Rotation size.
        encoding: File encoding.
    """
    logger.remove()

    LOG_LEVELS = {
        "info": lambda record: record["level"].name in ["DEBUG", "INFO", "SUCCESS", "WARNING"],
        "error": lambda record: record["level"].name in ["ERROR", "CRITICAL"],
    }

    for module in LOG_MODULES:
        module_log_dir = os.path.join(log_base_dir, module)
        os.makedirs(module_log_dir, exist_ok=True)

        for level_name, level_filter in LOG_LEVELS.items():
            logger.add(
                f"{module_log_dir}/{level_name}_{{time:YYYY-MM-DD}}.log",
                rotation=rotation,
                level=file_level,
                encoding=encoding,
                format=_format_file_log,
                filter=lambda record, m=module, lf=level_filter: (
                    record["extra"].get("module") == m and lf(record)
                )
            )

    # Console log (colorized on stderr so it does not mix with uvicorn stdout)
    logger.add(
        sys.stderr,
        level=console_level,
        format=_format_console_log,
        filter=lambda record: record["extra"].get("module") in LOG_MODULES,
        colorize=True
    )


# ============================================================================
# Logger Getters
# ============================================================================

def get_logger(module: LogModule):
    """Return the logger for a module (request_id attached automatically)."""
    return logger.bind(module=module, rid=_request_id_var.get())


def get_adapter_logger():
    return get_logger("adapter")

def get_api_logger():
    return get_logger("api")

def get_auth_logger():
    return get_logger("auth")

def get_db_logger():
    return get_logger("db")

def get_mcptools_logger():
    return get_logger("mcptools")

def get_server_logger():
    return get_logger("server")


# ============================================================================
# Structured log helpers (uniform format, easy to grep)
# ============================================================================

def mask_token(token: str | None) -> str:
    """Mask a token, showing only the first 8 chars."""
    if not token:
        return "-"
    return f"{token[:8]}..." if len(token) > 8 else token


def _build_context(**kwargs) -> str:
    """Join key=value pairs into a log context string, skipping None."""
    parts = []
    for key, value in kwargs.items():
        if value is not None:
            parts.append(f"{key}={value}")
    return " ".join(parts)


def log_op(log, op: str, *, token=None, msg: str = "", **extra):
    """Log an operation (INFO level).

    Usage:
        log_op(logger, "CONVERT", engine="pandoc", msg="docx -> md")

    Output:
        [CONVERT] engine=pandoc | docx -> md
    """
    ctx = _build_context(tok=mask_token(token), **extra)
    log.info(f"[{op}] {ctx} | {msg}" if msg else f"[{op}] {ctx}")


def log_err(log, op: str, error, *, token=None, **extra):
    """Log an error (ERROR level).

    Usage:
        log_err(logger, "CONVERT_FAIL", e, engine="pandoc")
    """
    ctx = _build_context(tok=mask_token(token), **extra)
    log.error(f"[{op}] {ctx} | {error}")


def log_warn(log, op: str, *, token=None, msg: str = "", **extra):
    """Log a warning (WARNING level)."""
    ctx = _build_context(tok=mask_token(token), **extra)
    log.warning(f"[{op}] {ctx} | {msg}" if msg else f"[{op}] {ctx}")


# ============================================================================
# HTTP request info logger (FastAPI and FastMCP)
# ============================================================================

async def log_request_info(
    tool_name: str = "",
    module: LogModule = "mcptools",
    request: Any = None
) -> dict[str, Any]:
    """Log and return information about the current HTTP request.

    Two call styles are supported:
    - FastAPI: pass the request explicitly
    - FastMCP: the request is taken from the MCP context

    Args:
        tool_name: Name of the tool calling this function.
        module: Module to log under (default "mcptools").
        request: FastAPI Request (optional; FastMCP resolves it automatically).

    Returns:
        Dict with the request information.
    """
    module_logger = get_logger(module)

    try:
        mcp_session_id = None

        if request:
            module_logger.info(f"[API_CALL] tool={tool_name}")
        else:
            # Lazy import: fastmcp is only needed to read the request from MCP context
            from fastmcp.server.dependencies import get_http_request

            request = get_http_request()
            mcp_session_id = request.headers.get("mcp-session-id", "N/A")
            module_logger.info(f"[MCP_CALL] tool={tool_name} session={mcp_session_id}")

        json_data = None
        content_type = request.headers.get("content-type", "")

        if request.method in ["POST", "PUT", "PATCH"]:
            try:
                if "application/json" in content_type:
                    json_data = await request.json()
            except Exception as e:
                module_logger.warning(f"[REQ_PARSE] JSON parse failed: {e}")

        request_info = {"json": json_data}
        if mcp_session_id:
            request_info["mcp_session_id"] = mcp_session_id

        if json_data:
            module_logger.debug(f"[REQ_BODY] {json.dumps(json_data, ensure_ascii=False)}")

        return request_info

    except Exception as e:
        module_logger.warning(f"[REQ_INFO] unavailable: {e}")
        return {}
