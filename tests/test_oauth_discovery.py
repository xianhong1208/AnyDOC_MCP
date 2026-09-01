"""Root and legacy OAuth discovery endpoints (clients that do not know the path-specific form)."""

import pytest
from starlette.testclient import TestClient

from src.api.router import oauth_discovery
from src.config.model import AppConfig, AuthConfig, ConfigModel, LoggingConfig, ModulesConfig, ServerConfig


def _client(auth_enabled: bool = True) -> TestClient:
    from app import create_app

    cfg = ConfigModel(
        server=ServerConfig(host="127.0.0.1", port=5050, transport="http"),
        auth=AuthConfig(enabled=auth_enabled, issuer="http://center.test:4568",
                        base_url="http://anydoc.test:5050"),
        logging=LoggingConfig(level="WARNING"),
        app=AppConfig(name="T", version="1", title="T", description=""),
        modules=ModulesConfig(enabled=[]),
    )
    return TestClient(create_app(cfg, transport="http"))


@pytest.fixture(autouse=True)
def _clear_cache():
    oauth_discovery.clear_cache()
    yield
    oauth_discovery.clear_cache()


def test_root_protected_resource_matches_path_specific_one():
    c = _client()
    root = c.get("/.well-known/oauth-protected-resource").json()
    specific = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert root["resource"] == specific["resource"] == "http://anydoc.test:5050/mcp"
    assert [u.rstrip("/") for u in root["authorization_servers"]] == \
        [u.rstrip("/") for u in specific["authorization_servers"]] == ["http://center.test:4568"]


def test_authorization_server_metadata_is_proxied_and_cached(monkeypatch):
    calls = []

    async def fake_fetch(settings, well_known):
        calls.append(well_known)
        return {"issuer": settings.issuer, "token_endpoint": f"{settings.issuer}/oauth/token"}

    doc = {"issuer": "http://center.test:4568", "token_endpoint": "http://center.test:4568/oauth/token"}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            calls.append("http")
            return doc

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            assert url == "http://center.test:4568/.well-known/oauth-authorization-server"
            return _Resp()

    monkeypatch.setattr(oauth_discovery.httpx, "AsyncClient", _Client)
    c = _client()
    assert c.get("/.well-known/oauth-authorization-server").json() == doc
    assert c.get("/.well-known/oauth-authorization-server").json() == doc
    assert calls == ["http"]  # second call served from cache


def test_unreachable_authorization_server_is_a_502(monkeypatch):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise ConnectionError("down")

    monkeypatch.setattr(oauth_discovery.httpx, "AsyncClient", _Client)
    assert _client().get("/.well-known/openid-configuration").status_code == 502


def test_discovery_absent_when_auth_disabled():
    c = _client(auth_enabled=False)
    assert c.get("/.well-known/oauth-protected-resource").status_code == 404
    assert c.get("/.well-known/oauth-authorization-server").status_code == 404
