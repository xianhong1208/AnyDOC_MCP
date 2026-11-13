"""How many conversions run at once, and the queue for the rest

Without a limit every request started its engine at once: twenty uploads meant
twenty soffice processes (each a few hundred MB) fighting over the same cores,
and all of them slowed down together until they hit their timeouts.

One limit covers every engine (LibreOffice, Pandoc, Tesseract, Poppler, the
pure-Python ones) rather than one per engine: they all compete for the same CPU
and memory, a multi-hop conversion uses its engines one after another, and four
separate limits would let four times the load in while giving the operator four
numbers to tune instead of one. A request takes one slot for its whole run, all
hops included, so a slot is at most one engine process at a time.

Requests over the limit wait in a first-in, first-out queue. When the queue is
full, or a request has waited longer than the queue timeout, it fails at once
with ServerBusyError instead of piling up memory (each waiting request holds its
upload) or running into the client's own timeout.

The limit is per server process: with several workers each has its own.
"""

import asyncio
import math
import os
from collections import deque
from contextlib import asynccontextmanager

from src.domain.exceptions import DomainException
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

# The default limit is the CPUs this process may use, capped: LibreOffice and
# Tesseract take hundreds of MB each, so on a large machine memory runs out first.
DEFAULT_MAX_CONCURRENT_CAP = 8
# Default queue length, as a multiple of the limit.
DEFAULT_QUEUE_FACTOR = 4
DEFAULT_QUEUE_TIMEOUT_SEC = 30.0


def default_max_concurrent() -> int:
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    return max(1, min(cpus, DEFAULT_MAX_CONCURRENT_CAP))


class ServerBusyError(DomainException):
    """Every conversion slot is taken and the queue is full or was waited on too long."""

    def __init__(self, reason: str, retry_after_sec: int):
        super().__init__(
            f"The server is busy: {reason}. No conversion was started. Wait about "
            f"{retry_after_sec} seconds and call the same tool again with the same "
            "arguments; the file and the request were fine. If it keeps happening, "
            "tell the user the conversion service is overloaded right now.",
            error_code="server_busy",
            details={"retry_after_sec": retry_after_sec},
        )
        self.retry_after_sec = retry_after_sec


class ConversionLimiter:
    """At most `max_concurrent` conversions at once; up to `max_queued` more wait in order."""

    def __init__(self, max_concurrent: int | None = None, max_queued: int | None = None,
                 queue_timeout_sec: float = DEFAULT_QUEUE_TIMEOUT_SEC):
        self.max_concurrent = max_concurrent or default_max_concurrent()
        self.max_queued = (self.max_concurrent * DEFAULT_QUEUE_FACTOR
                           if max_queued is None else max_queued)
        self.queue_timeout_sec = queue_timeout_sec
        self.active = 0
        self._waiters: deque[asyncio.Future] = deque()

    @property
    def queued(self) -> int:
        return sum(1 for f in self._waiters if not f.done())

    def status(self) -> dict:
        return {
            "running": self.active,
            "queued": self.queued,
            "max_concurrent": self.max_concurrent,
            "max_queued": self.max_queued,
            "queue_timeout_sec": self.queue_timeout_sec,
        }

    def _retry_after(self) -> int:
        return max(1, math.ceil(self.queue_timeout_sec / 2))

    async def acquire(self) -> None:
        # A free slot never coexists with waiters: release() hands it straight to
        # the longest waiter, so a newcomer cannot jump the queue.
        if self.active < self.max_concurrent:
            self.active += 1
            return
        if self.queued >= self.max_queued:
            logger.warning(f"[limiter] busy: {self.active} running, {self.queued} queued; refused")
            raise ServerBusyError(
                f"{self.active} conversions are running and {self.queued} are waiting, "
                "the most this server queues", self._retry_after())

        fut = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            async with asyncio.timeout(self.queue_timeout_sec):
                await fut
        except BaseException as e:
            if fut.done() and not fut.cancelled():
                # The slot was handed over just as we gave up: pass it on.
                self.release()
            else:
                fut.cancel()
                try:
                    self._waiters.remove(fut)
                except ValueError:
                    pass
            if isinstance(e, TimeoutError):
                logger.warning(f"[limiter] busy: waited {self.queue_timeout_sec:g}s; refused")
                raise ServerBusyError(
                    f"waited {self.queue_timeout_sec:g} seconds in the queue for a free "
                    f"slot ({self.active} conversions running)", self._retry_after()) from None
            raise
        # A releasing conversion handed its slot straight to us (active unchanged).

    def release(self) -> None:
        while self._waiters:
            fut = self._waiters.popleft()
            if not fut.done():
                fut.set_result(None)  # the slot goes to the longest waiter
                return
        self.active -= 1

    @asynccontextmanager
    async def slot(self):
        await self.acquire()
        try:
            yield
        finally:
            self.release()


_limiter = ConversionLimiter()


def configure(max_concurrent: int | None, max_queued: int | None,
              queue_timeout_sec: float) -> ConversionLimiter:
    """Replace the process-wide limiter (at startup, before any conversion)."""
    global _limiter
    _limiter = ConversionLimiter(max_concurrent, max_queued, queue_timeout_sec)
    return _limiter


def get_limiter() -> ConversionLimiter:
    return _limiter


def conversion_slot():
    """`async with conversion_slot():` around the work of one conversion request."""
    return _limiter.slot()
