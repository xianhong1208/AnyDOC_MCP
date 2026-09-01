"""Shared engine infrastructure

Every engine shares two things:
1. a uniform interface: convert(data, src, dst, **opts) -> bytes
2. a sandboxed temp directory plus subprocess execution

Conversion engines are almost all CLI tools (pandoc / soffice) that only take
files, not bytes, so the "bytes -> temp file -> run CLI -> read bytes back ->
clean up" boilerplate lives here. Individual engines only need to assemble the
right command line.
"""

import asyncio
import shutil
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path

from src.domain.convert.engines.discovery import find_binary
from src.domain.convert.types import ConversionFailedError
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

DEFAULT_TIMEOUT_SEC = 120


class ConversionEngine(ABC):
    """Conversion engine interface

    Subclasses must set `name` and implement convert().
    is_available() detects system dependencies at startup (is pandoc/soffice
    installed), so the capability matrix reflects "what this machine can
    actually do" rather than "what is supported in theory".
    """

    name: str = "base"

    @abstractmethod
    async def convert(self, data: bytes, src: str, dst: str, **opts) -> bytes:
        """Convert data from format src to format dst and return the result bytes"""

    def is_available(self) -> bool:
        """Whether this engine's system dependencies are ready. Default: pure Python, always."""
        return True

    def can_handle(self, src: str, dst: str) -> bool:
        """Whether this engine can really do src -> dst on this machine

        Defaults to is_available(). Override it for engines where "the main
        binary is installed" does not imply "every feature is present".
        LibreOffice is the classic case: with libreoffice-writer installed but
        not calc, the soffice binary exists and is_available() returns True, yet
        every spreadsheet conversion fails. A capability claim that is wrong at
        this granularity gives users "it said it was supported, but it failed".
        """
        return self.is_available()


class CliEngine(ConversionEngine):
    """Base class for engines that run an external CLI"""

    binary: str = ""

    def is_available(self) -> bool:
        return find_binary(self.binary) is not None

    def resolved_binary(self) -> str:
        """Full path to use at execution time

        self.binary cannot be used directly: on Windows LibreOffice is not on
        PATH, so the probed absolute path must be used."""
        return find_binary(self.binary) or self.binary

    async def _run(
        self,
        argv: Sequence[str],
        cwd: Path,
        src: str,
        dst: str,
        timeout: int = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        """Run the CLI in cwd; raise ConversionFailedError on failure

        The timeout is mandatory: LibreOffice hangs indefinitely on corrupt
        files, and without a timeout the worker stays blocked until the service
        restarts.
        """
        logger.debug(f"[{self.name}] exec: {' '.join(argv)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise ConversionFailedError(
                self.name, src, dst,
                f"'{self.binary}' not installed in this container",
            )

        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ConversionFailedError(
                self.name, src, dst, f"timed out after {timeout}s",
            )

        if proc.returncode != 0:
            detail = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise ConversionFailedError(
                self.name, src, dst,
                detail[:500] or f"exit code {proc.returncode}",
            )


class _Workspace:
    """Self-cleaning temporary working directory

    Usage:
        with _Workspace() as ws:
            src = ws.write("input.docx", data)
            ...
            return ws.read("output.pdf")
    """

    def __init__(self):
        self._dir: Path | None = None

    def __enter__(self) -> "_Workspace":
        self._dir = Path(tempfile.mkdtemp(prefix="anydoc_"))
        return self

    def __exit__(self, *exc_info) -> None:
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)

    @property
    def path(self) -> Path:
        assert self._dir is not None, "_Workspace used outside its context manager"
        return self._dir

    def write(self, filename: str, data: bytes) -> Path:
        target = self.path / filename
        target.write_bytes(data)
        return target

    def read(self, filename: str) -> bytes:
        return (self.path / filename).read_bytes()

    def find_one(self, pattern: str) -> Path:
        """Find the single output file matching pattern

        LibreOffice does not let you choose the output file name (only the
        directory), so the result has to be picked up with a glob.
        """
        matches = sorted(self.path.glob(pattern))
        if not matches:
            raise FileNotFoundError(f"No output matching '{pattern}' in workspace")
        return matches[0]


Workspace = _Workspace
