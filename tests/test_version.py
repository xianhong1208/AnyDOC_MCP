"""The version Morphyard reports comes from pyproject.toml, whatever the config says.

1.1.0 shipped with config.yaml's app.version still at 1.0.0 and reported 1.0.0
everywhere; these pin the single source of truth (src/version.py).
"""

import tomllib
from pathlib import Path

from src.version import get_version

PYPROJECT_VERSION = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())["project"]["version"]


def _client(config_version: str | None):
    from starlette.testclient import TestClient

    from app import create_app
    from src.config.model import AppConfig, AuthConfig, ConfigModel, LoggingConfig, ModulesConfig, ServerConfig

    cfg = ConfigModel(
        server=ServerConfig(host="127.0.0.1", port=5050, transport="http"),
        auth=AuthConfig(enabled=False),
        logging=LoggingConfig(level="WARNING"),
        app=AppConfig(name="TestMCP", version=config_version, title="Test MCP Service", description="desc"),
        modules=ModulesConfig(enabled=[]),
    )
    return TestClient(create_app(cfg, transport="http"))


class TestReportedVersion:
    def test_version_is_pyproject_version(self):
        """get_version() is [project].version from pyproject.toml."""
        assert get_version() == PYPROJECT_VERSION

    def test_config_version_is_ignored(self):
        """A stale app.version in the config does not leak into /openapi.json or the landing page."""
        client = _client("1.0.0" if PYPROJECT_VERSION != "1.0.0" else "0.0.1")
        assert client.get("/openapi.json").json()["info"]["version"] == PYPROJECT_VERSION
        assert PYPROJECT_VERSION in client.get("/").text

    def test_config_without_version_loads(self):
        """A config with no app.version (the shipped one) is valid."""
        assert _client(None).get("/openapi.json").status_code == 200
