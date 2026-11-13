"""The version Morphyard reports: pyproject.toml's, the one place it is written.

1.1.0 was released with ``app.version`` in config.yaml still at 1.0.0, so
/openapi.json, the landing page and MCP serverInfo kept saying 1.0.0. Reading the
version from pyproject.toml (next to the source, and copied into the Docker image)
leaves a single place to bump. Installed package metadata is only the fallback for
an install without the source tree: it lags behind pyproject.toml until ``uv sync``.
"""

import tomllib
from functools import lru_cache
from importlib import metadata
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


@lru_cache(maxsize=1)
def get_version() -> str:
    """The running Morphyard's version, e.g. ``"1.1.1"``."""
    try:
        return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return metadata.version("morphyard")
    except metadata.PackageNotFoundError:
        return "0.0.0"
