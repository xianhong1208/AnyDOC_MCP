"""Conversions beyond a limit wait in a queue, then are refused as busy (issue #23).

There was no limit: every request started its engine at once. Now at most
max_concurrent conversions run, up to max_queued more wait first-in first-out
for at most queue_timeout_sec, and the rest get a clear "busy" error.
"""

import asyncio
import base64
import io

import pytest
from fastapi import FastAPI
from fastmcp import Client, FastMCP
from starlette.testclient import TestClient

from src.config.model import ConvertConfig
from src.domain.convert import limiter as limiter_mod
from src.domain.convert import service
from src.domain.convert.limiter import ConversionLimiter, ServerBusyError
from src.domain.convert.registry import ENGINES
from src.fastmcp_tools.morphyard_tools import register_morphyard_tools


@pytest.fixture(autouse=True)
def _restore_limiter():
    saved = limiter_mod._limiter
    yield
    limiter_mod._limiter = saved


async def _hold(lim: ConversionLimiter, started: list, gate: asyncio.Event, tag):
    async with lim.slot():
        started.append(tag)
        await gate.wait()


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# The limiter on its own
# ---------------------------------------------------------------------------

async def test_no_more_than_the_limit_run_at_once():
    """TC-LIM-001"""
    lim = ConversionLimiter(2, 10, 5)
    started, gate = [], asyncio.Event()
    tasks = [asyncio.create_task(_hold(lim, started, gate, i)) for i in range(5)]
    await _settle()
    assert len(started) == 2
    assert lim.status()["running"] == 2 and lim.status()["queued"] == 3
    gate.set()
    await asyncio.gather(*tasks)
    assert started == [0, 1, 2, 3, 4]
    assert lim.active == 0 and lim.queued == 0


async def test_a_newcomer_does_not_jump_the_queue():
    """TC-LIM-020: when a slot frees up it goes to the waiter, not to a request arriving now."""
    lim = ConversionLimiter(1, 10, 5)
    order = []

    async def job(tag):
        async with lim.slot():
            order.append(tag)
            await asyncio.sleep(0)

    first = asyncio.create_task(job("first"))
    await _settle()
    waiter = asyncio.create_task(job("waiter"))
    await _settle()
    await first  # frees the slot...
    late = asyncio.create_task(job("late"))  # ...and someone new arrives at once
    await asyncio.gather(waiter, late)
    assert order == ["first", "waiter", "late"]


async def test_waiting_requests_are_served_first_in_first_out():
    """TC-LIM-002"""
    lim = ConversionLimiter(1, 10, 5)
    order = []

    async def job(tag):
        async with lim.slot():
            order.append(tag)
            await asyncio.sleep(0.01)

    tasks = []
    for i in range(6):
        tasks.append(asyncio.create_task(job(i)))
        await asyncio.sleep(0)  # arrive in this order
    await asyncio.gather(*tasks)
    assert order == [0, 1, 2, 3, 4, 5]


async def test_a_full_queue_refuses_at_once():
    """TC-LIM-003"""
    lim = ConversionLimiter(1, 2, 30)
    started, gate = [], asyncio.Event()
    tasks = [asyncio.create_task(_hold(lim, started, gate, i)) for i in range(3)]
    await _settle()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(ServerBusyError) as err:
        await lim.acquire()
    assert loop.time() - t0 < 0.5
    assert err.value.error_code == "server_busy"
    assert err.value.retry_after_sec == 15
    assert "busy" in err.value.message and "same arguments" in err.value.message
    gate.set()
    await asyncio.gather(*tasks)
    assert lim.active == 0


async def test_a_request_that_waits_too_long_is_refused_and_leaves_no_trace():
    """TC-LIM-004"""
    lim = ConversionLimiter(1, 5, 0.1)
    started, gate = [], asyncio.Event()
    holder = asyncio.create_task(_hold(lim, started, gate, "holder"))
    await _settle()
    with pytest.raises(ServerBusyError, match="waited 0.1 seconds"):
        await lim.acquire()
    assert lim.queued == 0 and lim.active == 1
    assert not lim._waiters  # its place is gone, not just marked
    gate.set()
    await holder
    assert lim.active == 0
    async with lim.slot():  # the slot is still usable
        assert lim.active == 1


async def test_no_queue_means_refuse_when_every_slot_is_taken():
    """TC-LIM-005: max_queued 0"""
    lim = ConversionLimiter(1, 0, 30)
    started, gate = [], asyncio.Event()
    holder = asyncio.create_task(_hold(lim, started, gate, "holder"))
    await _settle()
    with pytest.raises(ServerBusyError):
        await lim.acquire()
    gate.set()
    await holder


async def test_a_cancelled_waiter_gives_up_its_place():
    """TC-LIM-006: a client that goes away while queued does not keep a place or a slot."""
    lim = ConversionLimiter(1, 5, 30)
    started, gate = [], asyncio.Event()
    holder = asyncio.create_task(_hold(lim, started, gate, "holder"))
    await _settle()
    waiter = asyncio.create_task(lim.acquire())
    await _settle()
    assert lim.queued == 1
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert lim.queued == 0
    assert not lim._waiters
    gate.set()
    await holder
    assert lim.active == 0


async def test_a_slot_handed_over_as_the_waiter_gives_up_is_passed_on():
    """TC-LIM-007: the slot is not lost when the handover and the cancel cross."""
    lim = ConversionLimiter(1, 5, 30)
    await lim.acquire()
    first = asyncio.create_task(lim.acquire())
    await _settle()
    second = asyncio.create_task(lim.acquire())
    await _settle()
    lim.release()       # hands the slot to `first`...
    first.cancel()      # ...which is cancelled before it runs again
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(second, 1)  # so the slot went on to `second`
    assert lim.active == 1
    lim.release()
    assert lim.active == 0


async def test_a_failing_conversion_frees_its_slot():
    """TC-LIM-008"""
    lim = ConversionLimiter(1, 0, 30)
    with pytest.raises(RuntimeError):
        async with lim.slot():
            raise RuntimeError("engine failed")
    assert lim.active == 0
    async with lim.slot():
        pass


@pytest.mark.parametrize("cpus, expected", [(1, 1), (4, 4), (8, 8), (64, 8)])
def test_the_default_limit_follows_the_cpus_up_to_eight(monkeypatch, cpus, expected):
    """TC-LIM-009"""
    monkeypatch.setattr(limiter_mod.os, "sched_getaffinity", lambda pid: set(range(cpus)),
                        raising=False)
    lim = ConversionLimiter()
    assert lim.max_concurrent == expected
    assert lim.max_queued == 4 * expected
    assert lim.queue_timeout_sec == 30


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [("", None), ("  ", None), (None, None), ("3", 3), (3, 3)])
def test_blank_settings_mean_the_defaults(raw, expected):
    """TC-LIM-010"""
    cfg = ConvertConfig(max_concurrent_conversions=raw, max_queued_conversions=raw)
    assert cfg.max_concurrent_conversions == expected
    assert cfg.max_queued_conversions == expected


@pytest.mark.parametrize("field, value", [
    ("max_concurrent_conversions", 0), ("max_queued_conversions", -1), ("queue_timeout_sec", 0),
])
def test_settings_out_of_range_are_refused(field, value):
    """TC-LIM-011"""
    with pytest.raises(ValueError):
        ConvertConfig(**{field: value})


def test_the_queue_may_be_switched_off():
    """TC-LIM-012"""
    assert ConvertConfig(max_queued_conversions=0).max_queued_conversions == 0


def test_startup_applies_the_settings():
    """TC-LIM-013"""
    from app import _apply_convert_config

    class Cfg:
        convert = ConvertConfig(max_concurrent_conversions=3, max_queued_conversions=7,
                                queue_timeout_sec=12)

    _apply_convert_config(Cfg())
    status = limiter_mod.get_limiter().status()
    assert (status["max_concurrent"], status["max_queued"], status["queue_timeout_sec"]) == (3, 7, 12)


def test_health_reports_the_queue():
    """TC-LIM-014"""
    from src.api.router.health import router

    limiter_mod.configure(2, 5, 10)
    app = FastAPI()
    app.include_router(router)
    body = TestClient(app).get("/health").json()
    assert body["conversions"] == {"running": 0, "queued": 0, "max_concurrent": 2,
                                   "max_queued": 5, "queue_timeout_sec": 10}


def test_over_http_busy_is_503_with_retry_after():
    """TC-LIM-015"""
    from src.middleware.error_handler import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)

    @app.get("/x")
    async def x():
        raise ServerBusyError("test", 7)

    r = TestClient(app).get("/x")
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "7"
    assert r.json()["error"] == "server_busy"


# ---------------------------------------------------------------------------
# Through the conversion service and the MCP tools
# ---------------------------------------------------------------------------

XML = "<?xml version='1.0'?><note>hello</note>"


async def test_conversions_never_run_more_than_the_limit(monkeypatch):
    """TC-LIM-016: 3 x the limit at once; the engine never sees more than the limit."""
    limiter_mod.configure(2, 10, 30)
    engine = ENGINES["text"]
    running = peak = 0
    original = engine.convert

    async def slow_convert(data, src, dst, **opts):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return await original(data, src, dst, **opts)

    monkeypatch.setattr(engine, "convert", slow_convert)
    results = await asyncio.gather(*(
        service.convert_artifact(XML, "n.xml", None, "md") for _ in range(6)))
    assert len(results) == 6
    assert peak == 2


def _pdf() -> str:
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    w.write(buf)
    return base64.b64encode(buf.getvalue()).decode()


@pytest.mark.parametrize("tool, args", [
    ("convert_document", {"target_format": "md", "output_name": "n",
                          "file_content": XML, "file_name": "n.xml"}),
    ("extract_text", {"file_content": XML, "file_name": "n.xml"}),
    ("pdf_extract_pages", {"pages": "1", "file_name": "a.pdf"}),
    ("pdf_split", {"file_name": "a.pdf"}),
    ("pdf_rotate", {"degrees": 90, "file_name": "a.pdf"}),
    ("pdf_protect", {"password": "pw", "file_name": "a.pdf"}),
])
async def test_every_conversion_tool_answers_busy_as_a_tool_error(tool, args):
    """TC-LIM-017"""
    if args.get("file_name") == "a.pdf":
        args = {**args, "file_content": _pdf()}
    lim = limiter_mod.configure(1, 0, 30)
    mcp = FastMCP("busy-test")
    register_morphyard_tools(mcp)
    async with Client(mcp) as client:
        ok = await client.call_tool(tool, args, raise_on_error=False)
        assert not ok.is_error, ok.content
        await lim.acquire()  # every slot taken, no queue
        try:
            busy = await client.call_tool(tool, args, raise_on_error=False)
        finally:
            lim.release()
    assert busy.is_error
    assert "server is busy" in busy.content[0].text


async def test_a_bad_request_is_answered_even_when_busy():
    """TC-LIM-018: validation comes before the queue."""
    lim = limiter_mod.configure(1, 0, 30)
    mcp = FastMCP("busy-test")
    register_morphyard_tools(mcp)
    await lim.acquire()
    try:
        async with Client(mcp) as client:
            r = await client.call_tool("convert_document", {
                "target_format": "wav", "output_name": "n", "file_content": XML,
                "file_name": "n.xml"}, raise_on_error=False)
    finally:
        lim.release()
    assert r.is_error
    assert "not a target format" in r.content[0].text


async def test_list_and_inspect_take_no_slot():
    """TC-LIM-019"""
    lim = limiter_mod.configure(1, 0, 30)
    mcp = FastMCP("busy-test")
    register_morphyard_tools(mcp)
    await lim.acquire()
    try:
        async with Client(mcp) as client:
            listed = await client.call_tool("list_supported_conversions", {}, raise_on_error=False)
            inspected = await client.call_tool("inspect_document", {
                "file_content": _pdf(), "file_name": "a.pdf"}, raise_on_error=False)
    finally:
        lim.release()
    assert not listed.is_error and not inspected.is_error
