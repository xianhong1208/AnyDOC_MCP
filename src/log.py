"""
Log configuration module.

Features:
- Console output (colorized, written to stderr)
- Per-module log files (adapter, api, auth, db, mcptools, server)
- Automatic request ID injection (via contextvars, links the whole request chain)

Troubleshooting:
    # Follow the full chain of one request
    grep "rid=abc12345" logs/*/info_*.log

    # Every file handed to a user, with its final name and size
    grep -F "[DELIVER]" logs/mcptools/info_*.log

This module must not import fastmcp / starlette: every module under src/domain/
logs through it, and the conversion core should stay usable without a web framework.
"""

import os
import sys
import uuid
from contextvars import ContextVar
from typing import Literal

from loguru import logger


# ============================================================================
# Request ID (generated per HTTP request, carried through every layer via contextvars)
# ============================================================================

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

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


def get_api_logger():
    return get_logger("api")

def get_mcptools_logger():
    return get_logger("mcptools")

def get_server_logger():
    return get_logger("server")
