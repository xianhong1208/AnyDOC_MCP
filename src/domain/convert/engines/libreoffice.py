"""LibreOffice headless engine: Office-to-Office and Office-to-PDF

This is the only practical way to convert docx/xlsx/pptx to PDF while
**preserving layout**. Pandoc cannot do it (it understands semantic structure,
not layout), so every path that demands visual fidelity comes through here.
"""

import asyncio
import os
import tempfile
from pathlib import Path

from src.domain.convert.engines.base import CliEngine, Workspace
from src.domain.convert.engines.discovery import (
    IS_WINDOWS,
    find_binary,
    path_to_file_url,
)
from src.domain.convert.types import ConversionFailedError
from src.log import get_mcptools_logger

logger = get_mcptools_logger()

# LibreOffice --convert-to target codes. Most match the extension; a few need an
# explicit filter
_CONVERT_TO = {
    "pdf": "pdf",
    "docx": "docx",
    "doc": "doc",
    "odt": "odt",
    "rtf": "rtf",
    "txt": "txt:Text (encoded):UTF8",
    "html": "html",
    "xlsx": "xlsx",
    "xls": "xls",
    "ods": "ods",
    "csv": "csv:Text - txt - csv (StarCalc):44,34,76",  # comma, double quote, UTF-8
    "pptx": "pptx",
    "ppt": "ppt",
    "odp": "odp",
    "png": "png",
    "jpg": "jpg",
}

# One LibreOffice user profile cannot be used by two processes at once; it
# deadlocks outright. Under concurrent conversions each process gets its own
# profile, and this lock keeps the profile directory names from colliding.
_profile_counter = 0
_profile_lock = asyncio.Lock()


# Format -> required LibreOffice component. Debian splits writer/calc/impress
# into separate packages, and installing only one is a common deployment (e.g.
# calc is skipped when only document-to-PDF is needed).
_COMPONENT_FOR_FORMAT = {
    "docx": "swriter", "doc": "swriter", "odt": "swriter",
    "rtf": "swriter", "txt": "swriter", "html": "swriter",
    "xlsx": "scalc", "xls": "scalc", "ods": "scalc", "csv": "scalc",
    "pptx": "simpress", "ppt": "simpress", "odp": "simpress",
}


class LibreOfficeEngine(CliEngine):
    """soffice --headless --convert-to"""

    name = "libreoffice"
    binary = "soffice"

    def _program_dir(self) -> Path | None:
        """The program directory holding the soffice binary; component launchers live there"""
        path = find_binary(self.binary)
        return Path(path).resolve().parent if path else None

    def has_component(self, component: str) -> bool:
        """Check whether swriter / scalc / simpress is actually installed

        `soffice --version` is no use: it only reports the main program's
        version and answers just the same for a partial install. Checking that
        the component launcher file exists is the real test.
        """
        program_dir = self._program_dir()
        if not program_dir:
            return False
        # On Windows the component launchers carry an .exe extension
        candidates = (component, f"{component}.exe") if IS_WINDOWS else (component,)
        return any((program_dir / name).exists() for name in candidates)

    def can_handle(self, src: str, dst: str) -> bool:
        """The components needed on both the source and target side must be present

        pdf is output-only; any component can produce it, so it maps to no
        specific component.
        """
        if not self.is_available():
            return False
        needed = {
            _COMPONENT_FOR_FORMAT.get(fmt)
            for fmt in (src, dst)
            if fmt != "pdf" and fmt in _COMPONENT_FOR_FORMAT
        }
        return all(self.has_component(c) for c in needed if c)

    async def _next_profile_dir(self) -> str:
        global _profile_counter
        async with _profile_lock:
            _profile_counter += 1
            n = _profile_counter
        path = Path(tempfile.gettempdir()) / f"lo_profile_{os.getpid()}_{n}"
        # Must be a valid file:// URL. String concatenation happens to be right
        # on Linux (/tmp/x -> file:///tmp/x), but on Windows C:\Users\x would
        # become file://C:\Users\x: one slash short, backslashes unconverted,
        # and LibreOffice rejects it.
        return path_to_file_url(path)

    async def convert(self, data: bytes, src: str, dst: str, **opts) -> bytes:
        target = _CONVERT_TO.get(dst)
        if not target:
            raise ConversionFailedError(
                self.name, src, dst, f"LibreOffice has no filter for '{dst}'"
            )

        timeout = int(opts.get("timeout", 180))
        profile_url = await self._next_profile_dir()

        with Workspace() as ws:
            ws.write(f"input.{src}", data)
            argv = [
                self.resolved_binary(),
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--nodefault",
                f"-env:UserInstallation={profile_url}",
                "--convert-to", target,
                "--outdir", str(ws.path),
                f"input.{src}",
            ]
            await self._run(argv, cwd=ws.path, src=src, dst=dst, timeout=timeout)

            # soffice does not accept a custom output name; pick it up by extension
            ext = dst if dst != "txt" else "txt"
            try:
                produced = ws.find_one(f"input.{ext}")
            except FileNotFoundError:
                raise ConversionFailedError(
                    self.name, src, dst,
                    "LibreOffice reported success but produced no output "
                    "(typically a corrupt or password-protected source file)",
                )
            result = produced.read_bytes()

        if not result:
            raise ConversionFailedError(self.name, src, dst, "empty output file")
        logger.info(f"[libreoffice] {src} → {dst}: {len(data)} → {len(result)} bytes")
        return result
