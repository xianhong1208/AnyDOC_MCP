"""convert.engine_timeout_sec (ENGINE_TIMEOUT) reaches the engines; unset keeps their defaults."""
import pytest

from src.config.model import ConvertConfig
from src.domain.convert.engines import base


@pytest.fixture(autouse=True)
def _restore():
    saved = base.ENGINE_TIMEOUT_SEC
    yield
    base.ENGINE_TIMEOUT_SEC = saved


def test_unset_keeps_each_engine_default():
    base.ENGINE_TIMEOUT_SEC = None
    assert base.resolve_timeout({}, 300) == 300
    assert base.resolve_timeout({}, 120) == 120


def test_configured_value_applies_to_every_engine():
    base.ENGINE_TIMEOUT_SEC = 45
    assert base.resolve_timeout({}, 300) == 45
    assert base.resolve_timeout({}, 120) == 45


def test_per_call_timeout_wins():
    base.ENGINE_TIMEOUT_SEC = 45
    assert base.resolve_timeout({"timeout": 7}, 300) == 7


@pytest.mark.parametrize("raw, expected", [("", None), ("  ", None), (None, None), ("90", 90), (90, 90)])
def test_config_blank_means_unset(raw, expected):
    assert ConvertConfig(engine_timeout_sec=raw).engine_timeout_sec == expected


def test_config_rejects_non_positive():
    with pytest.raises(ValueError):
        ConvertConfig(engine_timeout_sec=0)


def test_apply_convert_config_sets_the_engine_timeout():
    from app import _apply_convert_config

    class Cfg:
        convert = ConvertConfig(engine_timeout_sec=33)

    _apply_convert_config(Cfg())
    assert base.ENGINE_TIMEOUT_SEC == 33


@pytest.mark.parametrize("module, cls, default", [
    ("src.domain.convert.engines.pandoc", "PandocEngine", 120),
    ("src.domain.convert.engines.libreoffice", "LibreOfficeEngine", 180),
])
async def test_engine_passes_the_configured_timeout(monkeypatch, module, cls, default):
    import importlib
    eng_cls = getattr(importlib.import_module(module), cls)
    seen = {}

    async def fake_run(self, argv, cwd, src, dst, timeout=None, **_):
        seen["timeout"] = timeout
        raise RuntimeError("stop after the command is built")

    monkeypatch.setattr(eng_cls, "_run_command", fake_run)
    monkeypatch.setattr(eng_cls, "available", lambda self: True, raising=False)
    for configured, want in ((None, default), (41, 41)):
        base.ENGINE_TIMEOUT_SEC = configured
        seen.clear()
        with pytest.raises(Exception):
            await eng_cls().convert(b"hello", "md" if cls == "PandocEngine" else "docx",
                                    "docx" if cls == "PandocEngine" else "pdf")
        assert seen.get("timeout") == want, (cls, configured, seen)
