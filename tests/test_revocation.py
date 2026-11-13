"""MCP Gate revocation feed: the poller, its configuration, the /mcp verifier and /health.

The feed is faked with httpx.MockTransport (no network); tokens are signed with an RSA key
generated here and verified by the real FastMCP JWT verifier.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from types import SimpleNamespace

import httpx
import pytest
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from src.auth import revocation as rv
from src.auth.revocation import (
    FAIL_CLOSED,
    FAIL_OPEN,
    REVOKED,
    UNAVAILABLE,
    RevocationFeedError,
    RevocationList,
    RevocationSettings,
)
ISSUER = "http://gate.test:4568"
BASE_URL = "http://morphyard.test:5050"
AUDIENCE = f"{BASE_URL}/mcp"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key, *, jti="keep", exp_in=3600, aud=AUDIENCE):
    now = int(time.time())
    return jwt.encode({"iss": ISSUER, "aud": aud, "sub": "alice", "iat": now, "exp": now + exp_in,
                       "jti": jti, "scope": "mcp:tools", "token_use": "access"},
                      key, algorithm="RS256")


class FakeFeed:
    """A scripted /oauth/revoked endpoint that records every request."""

    def __init__(self):
        self.requests: list[dict] = []
        self.responses: list = []  # httpx.Response | Exception | callable(form) -> Response
        self.now = 1_000_000

    def queue(self, revoked=(), now=None, status=200, body=None):
        payload = body if body is not None else {
            "revoked": [{"jti": j, "revoked_at": self.now, "exp": e} for j, e in revoked],
            "now": now if now is not None else self.now,
        }
        self.responses.append(httpx.Response(status, json=payload))

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        self.requests.append({"url": str(request.url), "form": form})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _settings(**kw):
    base = dict(issuer="http://gate.test/", client_id="rs-1", client_secret="s3cret",
                poll_interval=30.0, fail_mode=FAIL_OPEN, stale_after_intervals=4)
    base.update(kw)
    return RevocationSettings(**base)


def _rl(feed, clock=None, rng=lambda a, b: 0.0, **kw):
    return RevocationList(_settings(**kw), http_client=feed.client(),
                          clock=clock or Clock(), rng=rng)


@pytest.fixture(autouse=True)
def _no_global_list():
    rv.set_revocation_list(None)
    yield
    rv.set_revocation_list(None)


# -- the poller ---------------------------------------------------------------------


async def test_first_poll_is_full_then_incremental_with_overlap():
    feed, clock = FakeFeed(), Clock()
    rl = _rl(feed, clock)
    feed.queue([("a", 1_000_500)], now=1_000_000)
    feed.queue([("b", 1_000_900)], now=1_000_030)

    assert await rl.poll_once() == 1
    clock.t += 30
    assert await rl.poll_once() == 1

    first, second = feed.requests
    assert first["url"] == "http://gate.test/oauth/revoked"
    assert first["form"] == {"client_id": "rs-1", "client_secret": "s3cret"}
    # since = MCP Gate's own clock minus the overlap, not ours
    assert second["form"]["since"] == str(1_000_000 - rv.SINCE_OVERLAP_SEC)
    assert "a" in rl and "b" in rl and len(rl) == 2


async def test_periodic_full_resync_replaces_the_list():
    feed, clock = FakeFeed(), Clock()
    rl = _rl(feed, clock)
    feed.queue([("a", 2_000_000), ("b", 2_000_000)])
    await rl.poll_once()
    clock.t += rv.FULL_RESYNC_SEC
    feed.queue([("b", 2_000_000)])
    await rl.poll_once()
    assert "since" not in feed.requests[1]["form"]
    assert "a" not in rl and "b" in rl


async def test_entries_are_pruned_after_exp():
    feed, clock = FakeFeed(), Clock()
    rl = _rl(feed, clock)
    feed.queue([("short", 1_000_010), ("long", 1_009_999)], now=1_000_000)
    await rl.poll_once()
    assert "short" in rl
    # still inside the skew grace
    clock.t = 1_000_010 + rv.PRUNE_GRACE_SEC
    feed.queue([], now=int(clock.t))
    await rl.poll_once()
    assert "short" in rl
    clock.t += 1 + rv.PRUNE_GRACE_SEC
    feed.queue([], now=int(clock.t))
    await rl.poll_once()
    assert "short" not in rl and "long" in rl


async def test_malformed_items_are_skipped_and_missing_exp_is_kept():
    feed = FakeFeed()
    rl = _rl(feed)
    feed.queue(body={"revoked": [{"jti": 5}, "junk", {"jti": "noexp"}, {"jti": "ok", "exp": "x"}],
                     "now": "bad"})
    assert await rl.poll_once() == 2
    assert "noexp" in rl and "ok" in rl and 5 not in rl


@pytest.mark.parametrize("response, match", [
    (httpx.Response(401, json={"error": "invalid_client", "error_description": "client authentication failed"}),
     "HTTP 401: invalid_client \\(client authentication failed\\)"),
    (httpx.Response(403, json={"error": "unauthorized_client"}), "HTTP 403: unauthorized_client"),
    (httpx.Response(502, text="bad gateway"), "HTTP 502: bad gateway"),
    (httpx.Response(200, text="not json"), "not JSON"),
    (httpx.Response(200, json={"items": []}), "no 'revoked' list"),
])
async def test_feed_errors_raise(response, match):
    feed = FakeFeed()
    feed.responses.append(response)
    rl = _rl(feed)
    with pytest.raises(RevocationFeedError, match=match):
        await rl.poll_once()
    assert rl.status()["last_success"] is None


async def test_run_loop_backs_off_on_errors_and_recovers():
    feed, clock = FakeFeed(), Clock()
    delays: list[float] = []

    async def fake_sleep(d):
        delays.append(d)
        clock.t += d
        if len(delays) == 7:
            raise asyncio.CancelledError

    rl = RevocationList(_settings(), http_client=feed.client(), clock=clock,
                        sleep=fake_sleep, rng=lambda a, b: 0.0)
    feed.queue([("a", 9_999_999)])
    for _ in range(5):
        feed.responses.append(httpx.ConnectError("refused"))
    feed.queue([("b", 9_999_999)])

    with pytest.raises(asyncio.CancelledError):
        await rl.run()
    # ok, then 5 failures doubling up to the cap, then back to the interval
    assert delays == [30, 30, 60, 120, 240, 300, 30]
    assert "a" in rl and "b" in rl
    st = rl.status()
    assert st["consecutive_failures"] == 0 and st["last_error"] is None


def test_jitter_stays_within_ten_percent():
    rl = RevocationList(_settings(), http_client=httpx.AsyncClient(), rng=lambda a, b: b)
    assert rl.next_delay() == pytest.approx(33.0)
    rl = RevocationList(_settings(), http_client=httpx.AsyncClient(), rng=lambda a, b: a)
    assert rl.next_delay() == pytest.approx(27.0)
    rl = RevocationList(_settings(poll_interval=600), http_client=httpx.AsyncClient(), rng=lambda a, b: 0)
    rl._failures = 10
    assert rl.next_delay() == 600  # the cap never undercuts the interval


async def test_fail_open_keeps_accepting_when_the_feed_is_down():
    feed, clock = FakeFeed(), Clock()
    rl = _rl(feed, clock, fail_mode=FAIL_OPEN)
    assert rl.check("x") is None  # nothing fetched yet: accepted
    feed.queue([("gone", 9_999_999)])
    await rl.poll_once()
    clock.t += 10_000
    feed.responses.append(httpx.ConnectError("down"))
    with pytest.raises(httpx.ConnectError):
        await rl.poll_once()
    rl._record_failure(httpx.ConnectError("down"))
    assert rl.is_stale()
    assert rl.check("x") is None
    assert rl.check("gone") == REVOKED  # what we already know still applies
    assert rl.status()["status"] == "degraded"


async def test_fail_closed_rejects_once_stale():
    feed, clock = FakeFeed(), Clock()
    rl = _rl(feed, clock, fail_mode=FAIL_CLOSED)
    assert rl.check("x") == UNAVAILABLE  # before the first successful poll
    feed.queue([])
    await rl.poll_once()
    assert rl.check("x") is None
    clock.t += 4 * 30  # exactly stale_after: still fresh
    assert rl.check("x") is None
    clock.t += 1
    assert rl.check("x") == UNAVAILABLE
    rl._record_failure(RuntimeError("down"))
    assert rl.status()["stale"] is True
    feed.queue([])
    await rl.poll_once()
    assert rl.check("x") is None


def test_check_ignores_missing_jti_and_non_strings():
    rl = RevocationList(_settings(), http_client=httpx.AsyncClient())
    rl._revoked = {"a": 1}
    assert rl.check(None) is None
    assert 1 not in rl
    assert rl.check("a") == REVOKED


# -- lifecycle ----------------------------------------------------------------------


async def test_start_stop_lifecycle():
    feed = FakeFeed()
    for _ in range(3):
        feed.queue([("a", 9_999_999_999)], now=int(time.time()))
    rl = RevocationList(_settings(poll_interval=3600), http_client=feed.client())
    rl.start()
    task = rl._task
    rl.start()  # idempotent
    assert rl._task is task and rl.running
    for _ in range(50):
        if "a" in rl:
            break
        await asyncio.sleep(0.01)
    assert "a" in rl
    await rl.stop()
    assert not rl.running and task.cancelled()
    await rl.stop()  # idempotent


async def test_owned_client_is_closed_on_stop_and_recreated_on_start(monkeypatch):
    rl = RevocationList(_settings())
    first = rl._http

    async def never(self):
        await asyncio.sleep(3600)
    monkeypatch.setattr(RevocationList, "run", never)
    rl.start()
    await rl.stop()
    assert first.is_closed
    rl.start()
    assert rl._http is not first and not rl._http.is_closed
    await rl.stop()


async def test_revocation_lifespan_starts_and_stops_the_global_list(monkeypatch):
    started = []

    async def never(self):
        started.append(self)
        await asyncio.sleep(3600)
    monkeypatch.setattr(RevocationList, "run", never)
    rl = RevocationList(_settings(), http_client=httpx.AsyncClient())
    rv.set_revocation_list(rl)
    async with rv.revocation_lifespan():
        await asyncio.sleep(0)
        assert rl.running and started == [rl]
    assert not rl.running


async def test_revocation_lifespan_is_a_noop_when_disabled():
    async with rv.revocation_lifespan():
        pass
    assert rv.revocation_status() == {"status": "disabled", "enabled": False}
    assert rv.check_revoked("anything") is None


# -- configuration ------------------------------------------------------------------


def _config(auth_enabled=True, **rev):
    from src.config.model import RevocationConfig
    return SimpleNamespace(auth=SimpleNamespace(enabled=auth_enabled,
                                                revocation=RevocationConfig(**rev)))


REFS = {"auth.revocation.client_secret": "MORPHYARD_REVOCATION_CLIENT_SECRET"}


def test_build_disabled_by_default():
    assert rv.build_revocation_list(_config(), issuer=ISSUER, env_refs=REFS) is None
    assert rv.build_revocation_list(SimpleNamespace(), issuer=ISSUER) is None


def test_build_ignored_when_auth_disabled():
    cfg = _config(auth_enabled=False, enabled=True, client_id="c", client_secret="s")
    assert rv.build_revocation_list(cfg, issuer=None, env_refs=REFS) is None


@pytest.mark.parametrize("rev", [dict(client_id="c"), dict(client_secret="s"),
                                 dict(client_id="", client_secret="")])
def test_build_requires_credentials(rev):
    with pytest.raises(RuntimeError, match="client_id / client_secret"):
        rv.build_revocation_list(_config(enabled=True, **rev), issuer=ISSUER, env_refs=REFS)


def test_build_rejects_a_literal_secret():
    cfg = _config(enabled=True, client_id="c", client_secret="literal")
    with pytest.raises(RuntimeError, match="ENV_VAR"):
        rv.build_revocation_list(cfg, issuer=ISSUER, env_refs={})


def test_build_rejects_unknown_fail_mode():
    cfg = SimpleNamespace(auth=SimpleNamespace(enabled=True, revocation=SimpleNamespace(
        enabled=True, client_id="c", client_secret="s", fail_mode="maybe")))
    with pytest.raises(RuntimeError, match="fail_mode"):
        rv.build_revocation_list(cfg, issuer=ISSUER, env_refs=REFS)


async def test_build_resolves_settings():
    cfg = _config(enabled=True, client_id=" c ", client_secret="s", poll_interval_sec=15,
                  fail_mode="closed", stale_after_intervals=3)
    rl = rv.build_revocation_list(cfg, issuer=ISSUER + "/", env_refs=REFS)
    s = rl.settings
    assert (s.feed_url, s.client_id, s.poll_interval, s.fail_mode, s.stale_after) == (
        f"{ISSUER}/oauth/revoked", "c", 15.0, "closed", 45.0)
    assert "s'" not in repr(s)  # the secret is not in the repr
    await rl.stop()


def test_config_model_defaults_and_validation():
    from pydantic import ValidationError
    from src.config.model import AuthConfig, RevocationConfig
    rc = AuthConfig(enabled=True).revocation
    assert (rc.enabled, rc.poll_interval_sec, rc.fail_mode, rc.stale_after_intervals) == (
        False, 30, "open", 4)
    with pytest.raises(ValidationError):
        RevocationConfig(fail_mode="sometimes")
    with pytest.raises(ValidationError):
        RevocationConfig(poll_interval_sec=1)


def _app_config(auth_enabled=True, **rev):
    from src.config.model import (AppConfig, AuthConfig, ConfigModel, LoggingConfig, ModulesConfig,
                                  RevocationConfig, ServerConfig)
    return ConfigModel(
        server=ServerConfig(host="127.0.0.1", port=5050, transport="http"),
        auth=AuthConfig(enabled=auth_enabled, issuer=ISSUER, base_url=BASE_URL,
                        revocation=RevocationConfig(**rev)),
        logging=LoggingConfig(level="WARNING"),
        app=AppConfig(name="T", version="1", title="T", description=""),
        modules=ModulesConfig(enabled=[]),
    )


def test_init_revocation_installs_and_clears_the_list():
    from src.auth import init_revocation
    rl = init_revocation(_app_config(enabled=True, client_id="c", client_secret="s"), env_refs=REFS)
    assert rv.get_revocation_list() is rl and rl.settings.feed_url == f"{ISSUER}/oauth/revoked"
    assert init_revocation(_app_config(), env_refs=REFS) is None
    assert rv.get_revocation_list() is None
    rv.set_revocation_list(rl)
    assert init_revocation(_app_config(auth_enabled=False, enabled=True, client_id="c",
                                       client_secret="s"), env_refs=REFS) is None
    assert rv.get_revocation_list() is None


def test_init_revocation_refuses_a_literal_secret():
    from src.auth import init_revocation
    with pytest.raises(RuntimeError, match="MORPHYARD_REVOCATION_CLIENT_SECRET"):
        init_revocation(_app_config(enabled=True, client_id="c", client_secret="s"), env_refs={})


def test_config_manager_records_env_refs(tmp_path, monkeypatch):
    from src.config.config_manager import Config
    monkeypatch.chdir(tmp_path)
    (tmp_path / "c.yaml").write_text(
        'auth:\n  revocation:\n    client_id: "literal"\n'
        '    client_secret: "${T_SECRET:-}"\n', encoding="utf-8")
    monkeypatch.setattr(Config, "_env_refs", {})
    Config._load_config("c.yaml")
    assert Config.get_env_refs() == {"auth.revocation.client_secret": "T_SECRET"}


def test_config_yaml_template_ships_disabled_with_env_refs():
    import pathlib
    import yaml
    from src.config.config_manager import env_refs
    raw = yaml.safe_load((pathlib.Path(__file__).parent.parent / "config" / "config.yaml")
                         .read_text(encoding="utf-8"))
    refs = env_refs(raw)
    assert refs["auth.revocation.client_secret"] == "MORPHYARD_REVOCATION_CLIENT_SECRET"
    assert refs["auth.revocation.client_id"] == "MORPHYARD_REVOCATION_CLIENT_ID"
    assert raw["auth"]["revocation"]["enabled"].endswith(":-false}")


# -- the /mcp verifier ----------------------------------------------------------------


def _install(revoked=(), fail_mode=FAIL_OPEN, fresh=True):
    rl = RevocationList(_settings(fail_mode=fail_mode), http_client=httpx.AsyncClient())
    rl._revoked = {j: 9_999_999_999 for j in revoked}
    if fresh:
        rl._last_success = time.time()
    rv.set_revocation_list(rl)
    return rl


def _mcp_verifier(rsa_key):
    from src.auth.fastmcp_verifier import RevocationAwareJWTVerifier
    pem = rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return RevocationAwareJWTVerifier(public_key=pem, issuer=ISSUER, audience=AUDIENCE)


async def test_mcp_verifier_rejects_a_revoked_jti(rsa_key):
    v = _mcp_verifier(rsa_key)
    _install(revoked={"gone"})
    assert await v.verify_token(_token(rsa_key, jti="keep")) is not None
    assert await v.verify_token(_token(rsa_key, jti="gone")) is None
    assert await v.verify_token("garbage") is None
    assert await v.verify_token(_token(rsa_key, jti="keep", exp_in=-10)) is None


async def test_mcp_verifier_fail_closed(rsa_key):
    v = _mcp_verifier(rsa_key)
    _install(fail_mode=FAIL_CLOSED, fresh=False)
    assert await v.verify_token(_token(rsa_key)) is None
    rv.set_revocation_list(None)
    assert await v.verify_token(_token(rsa_key)) is not None


def test_auth_provider_uses_the_revocation_aware_verifier():
    from src.auth import build_auth_provider
    from src.auth.fastmcp_verifier import RevocationAwareJWTVerifier
    assert isinstance(build_auth_provider(_app_config()).token_verifier, RevocationAwareJWTVerifier)


async def test_mcp_transport_answers_401_invalid_token(rsa_key):
    from fastmcp import FastMCP
    from fastmcp.server.auth import RemoteAuthProvider
    provider = RemoteAuthProvider(token_verifier=_mcp_verifier(rsa_key),
                                  authorization_servers=[ISSUER], base_url=BASE_URL)
    app = FastMCP(name="t", auth=provider).http_app(transport="http")
    _install(revoked={"gone"})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as c:
            ok = await c.post("/mcp", headers={"Authorization": f"Bearer {_token(rsa_key)}"}, json={})
            r = await c.post("/mcp", headers={"Authorization": f"Bearer {_token(rsa_key, jti='gone')}"},
                             json={})
    assert ok.status_code != 401  # a live token gets past auth
    assert r.status_code == 401 and r.json()["error"] == "invalid_token"


# -- the app: lifespan + /health --------------------------------------------------------


def test_create_app_runs_the_poller_for_the_app_lifetime(monkeypatch):
    from app import create_app
    from src.config.config_manager import Config
    started = []

    async def never(self):
        started.append(self)
        await asyncio.sleep(3600)
    monkeypatch.setattr(RevocationList, "run", never)
    monkeypatch.setattr(Config, "_env_refs", dict(REFS))
    app = create_app(_app_config(enabled=True, client_id="c", client_secret="s"), transport="http")
    rl = rv.get_revocation_list()
    assert rl is not None and not rl.running
    with TestClient(app) as c:
        assert rl.running and started == [rl]
        body = c.get("/health").json()
        # never polled yet -> stale -> degraded
        assert body["status"] == "degraded"
        assert body["revocation"]["enabled"] is True and body["revocation"]["last_success"] is None
    assert not rl.running


def test_health_without_revocation(monkeypatch):
    from app import create_app
    with TestClient(create_app(_app_config(), transport="http")) as c:
        body = c.get("/health").json()
    assert body["status"] == "healthy"
    assert body["revocation"] == {"status": "disabled", "enabled": False}


# -- observability ------------------------------------------------------------------


async def test_status_reports_last_poll_and_size():
    feed, clock = FakeFeed(), Clock()
    rl = _rl(feed, clock)
    rv.set_revocation_list(rl)
    feed.queue([("a", 9_999_999), ("b", 9_999_999)])
    await rl.poll_once()
    st = rv.revocation_status()
    assert st["status"] == "healthy" and st["revoked_count"] == 2 and st["enabled"] is True
    assert st["last_success"].startswith("1970-01-12T13:46:40")
    assert st["fail_mode"] == "open" and st["poll_interval_sec"] == 30.0 and st["stale"] is False
