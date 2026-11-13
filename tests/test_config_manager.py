"""Env-var expansion tests for config loading."""

import pytest

from src.config.config_manager import _expand_env_vars


class TestExpandEnvVars:
    """${VAR} / ${VAR:-default} expansion rules."""

    def test_env_var_overrides_default(self, monkeypatch):
        """A set env var overrides the default."""
        monkeypatch.setenv("MY_HOST", "prod.example.com")
        assert _expand_env_vars("${MY_HOST:-localhost}") == "prod.example.com"

    def test_default_used_when_unset(self, monkeypatch):
        """The default is used when the env var is unset."""
        monkeypatch.delenv("MY_HOST", raising=False)
        assert _expand_env_vars("${MY_HOST:-localhost}") == "localhost"

    def test_recurses_into_nested_structures(self, monkeypatch):
        """Placeholders inside nested dicts and lists are expanded recursively."""
        monkeypatch.setenv("DB_PASS", "s3cret")
        config = {
            "database": {"url": "postgresql://user:${DB_PASS}@localhost/db"},
            "hosts": ["${DB_PASS}", "static"],
        }
        result = _expand_env_vars(config)
        assert result["database"]["url"] == "postgresql://user:s3cret@localhost/db"
        assert result["hosts"] == ["s3cret", "static"]

    def test_non_string_values_passthrough(self):
        """Non-string types (int / bool / None) pass through unchanged."""
        assert _expand_env_vars(5050) == 5050
        assert _expand_env_vars(True) is True
        assert _expand_env_vars(None) is None

    def test_missing_required_var_raises(self, monkeypatch):
        """A required placeholder with no default and no env var raises ValueError."""
        monkeypatch.delenv("REQUIRED_VAR", raising=False)
        with pytest.raises(ValueError):
            _expand_env_vars("${REQUIRED_VAR}")

    def test_pre_rename_mcp_center_url_is_ignored(self, monkeypatch):
        """MCP_CENTER_URL (MCP Gate's old name) is no longer read: with MCP_GATE_URL unset
        the default applies, and an explicit MCP_GATE_URL is not overridden by it."""
        monkeypatch.delenv("MCP_GATE_URL", raising=False)
        monkeypatch.setenv("MCP_CENTER_URL", "http://old:4568")
        assert _expand_env_vars("${MCP_GATE_URL:-http://localhost:4568}") == "http://localhost:4568"
        with pytest.raises(ValueError):
            _expand_env_vars("${MCP_GATE_URL}")
        monkeypatch.setenv("MCP_GATE_URL", "http://new:4568")
        assert _expand_env_vars("${MCP_GATE_URL:-http://localhost:4568}") == "http://new:4568"


class TestConvertConfigIsWired:
    """The convert section of config.yaml must actually take effect.

    This section used to be decorative: ConfigModel had no convert field, pydantic
    dropped the whole section, and the code used three hard-coded constants.
    A config change with no effect is the hardest kind of config bug to notice:
    no error, no warning, just silently ignored.
    """

    def test_config_model_has_convert_section(self):
        from src.config.model import ConfigModel, ConvertConfig

        assert "convert" in ConfigModel.model_fields
        defaults = ConvertConfig()
        assert defaults.max_input_mb > 0
        assert defaults.max_hops >= 1

    def test_apply_convert_config_updates_the_modules(self):
        """Settings must be written into the module constants, not just read and kept."""
        from app import _apply_convert_config
        from src.config.model import ConvertConfig
        from src.domain.convert import registry, service

        original = (service.MAX_INPUT_MB, registry.MAX_HOPS)
        try:
            class Cfg:
                convert = ConvertConfig(max_input_mb=7, engine_timeout_sec=11, max_hops=2)

            _apply_convert_config(Cfg())
            assert service.MAX_INPUT_MB == 7
            assert registry.MAX_HOPS == 2
        finally:
            service.MAX_INPUT_MB, registry.MAX_HOPS = original

    def test_body_limit_exceeds_the_service_limit(self):
        """The transport limit must exceed the service limit, or huge files get a bare 413.

        The MCP SDK transport defaults to 4 MiB and answers 413 before parsing
        anything beyond it; the AI cannot make sense of that response and just
        retries. Only with the limit raised does "file too large" come from the
        service layer as a clear error that explicitly says not to retry.
        """
        from mcp.server.streamable_http_manager import DEFAULT_MAX_REQUEST_BODY_SIZE

        from app import _raise_mcp_body_limit
        from src.domain.convert.service import MAX_INPUT_MB

        # base64 inflates by 4/3, so the body for a file the service allows is larger
        max_body_from_service = MAX_INPUT_MB * 4 / 3 * 1024 * 1024
        assert DEFAULT_MAX_REQUEST_BODY_SIZE < max_body_from_service, (
            "if the SDK default is already enough, this patch should be removed"
        )
        _raise_mcp_body_limit(MAX_INPUT_MB)  # must be idempotent when called twice
        _raise_mcp_body_limit(MAX_INPUT_MB)


class TestMorphyardEnvNames:
    """The shipped config reads MORPHYARD_* only; the pre-rename ANYDOC_* names are ignored."""

    OLD_TO_NEW = {
        "ANYDOC_BASE_URL": "MORPHYARD_BASE_URL",
        "ANYDOC_REVOCATION_ENABLED": "MORPHYARD_REVOCATION_ENABLED",
        "ANYDOC_REVOCATION_CLIENT_ID": "MORPHYARD_REVOCATION_CLIENT_ID",
        "ANYDOC_REVOCATION_CLIENT_SECRET": "MORPHYARD_REVOCATION_CLIENT_SECRET",
        "ANYDOC_REVOCATION_POLL_INTERVAL": "MORPHYARD_REVOCATION_POLL_INTERVAL",
        "ANYDOC_REVOCATION_FAIL_MODE": "MORPHYARD_REVOCATION_FAIL_MODE",
    }

    @staticmethod
    def _shipped_config():
        from pathlib import Path

        import yaml

        path = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
        return _expand_env_vars(yaml.safe_load(path.read_text(encoding="utf-8")))

    def test_config_names_only_morphyard_vars(self):
        """Every renamed variable appears in config.yaml under its new name and never the old one."""
        from pathlib import Path

        text = (Path(__file__).resolve().parent.parent / "config" / "config.yaml").read_text()
        assert "ANYDOC_" not in text
        for new in self.OLD_TO_NEW.values():
            assert "${" + new in text

    def test_morphyard_vars_are_read(self, monkeypatch):
        for old in self.OLD_TO_NEW:
            monkeypatch.delenv(old, raising=False)
        monkeypatch.setenv("MORPHYARD_BASE_URL", "http://new.example:5055")
        monkeypatch.setenv("MORPHYARD_REVOCATION_FAIL_MODE", "closed")
        cfg = self._shipped_config()
        assert cfg["auth"]["base_url"] == "http://new.example:5055"
        assert cfg["auth"]["revocation"]["fail_mode"] == "closed"

    def test_old_anydoc_vars_are_ignored(self, monkeypatch):
        """With only the old names set, the defaults apply (no legacy fallback)."""
        for old, new in self.OLD_TO_NEW.items():
            monkeypatch.delenv(new, raising=False)
        monkeypatch.setenv("ANYDOC_BASE_URL", "http://old.example:5055")
        monkeypatch.setenv("ANYDOC_REVOCATION_FAIL_MODE", "closed")
        monkeypatch.setenv("ANYDOC_REVOCATION_POLL_INTERVAL", "99")
        cfg = self._shipped_config()
        assert cfg["auth"]["base_url"] == "http://127.0.0.1:5055"
        assert cfg["auth"]["revocation"]["fail_mode"] == "open"
        assert str(cfg["auth"]["revocation"]["poll_interval_sec"]) == "30"
