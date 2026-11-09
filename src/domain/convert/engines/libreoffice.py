"""LibreOffice headless engine: Office-to-Office and Office-to-PDF

This is the only practical way to convert docx/xlsx/pptx to PDF while
**preserving layout**. Pandoc cannot do it (it understands semantic structure,
not layout), so every path that demands visual fidelity comes through here.
"""

import asyncio
import os
import tempfile
from pathlib import Path

from src.domain.convert.engines import base
from src.domain.convert.engines.base import CliEngine, Workspace
from src.domain.convert.engines.discovery import (
    IS_WINDOWS,
    find_binary,
    path_to_file_url,
)
from src.domain.convert.engines.external_refs import (
    UncheckableDocumentError,
    strip_external_references,
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


# Documents are untrusted, and LibreOffice loads what they link to while opening
# them: an image linked by path or URL was read from the server's disk or fetched
# over the network (anything the service can reach) and embedded in the output.
# Every conversion runs with a profile that stops it:
#
#   BlockUntrustedRefererLinks  links from documents outside a trusted location
#                               (none is configured) are not loaded
#   ooInet*Proxy*               every HTTP, HTTPS and FTP request goes to a proxy
#                               host that cannot exist (.invalid, RFC 6761), so
#                               nothing is fetched even where the block does not
#                               apply; the *_proxy variables do the same for
#                               anything that reads the environment
#   Update/Link                 links are never updated on load
#   macros                      never run
#
# The ODF importers of Calc and Impress and the RTF importer still read local
# files despite the block, so the document is also stripped of outside
# references first (external_refs.py).
_UNREACHABLE_PROXY = "proxy.invalid"
_PROFILE_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" \
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">\
<prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">\
<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">\
<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update">\
<prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update">\
<prop oor:name="Link" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetProxyType" oor:op="fuse"><value>2</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetNoProxy" oor:op="fuse"><value></value></prop></item>
""" + "".join(
    f'''<item oor:path="/org.openoffice.Inet/Settings">\
<prop oor:name="ooInet{scheme}ProxyName" oor:op="fuse"><value>{_UNREACHABLE_PROXY}</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings">\
<prop oor:name="ooInet{scheme}ProxyPort" oor:op="fuse"><value>9</value></prop></item>
'''
    for scheme in ("HTTP", "HTTPS", "FTP")
) + "</oor:items>\n"

_PROXY_ENV = {
    name: f"http://{_UNREACHABLE_PROXY}:9"
    for name in ("http_proxy", "https_proxy", "ftp_proxy", "all_proxy",
                 "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY")
}


def _write_profile_settings(profile: Path) -> None:
    """Put the hardened settings into the (new) profile before soffice first starts with it."""
    user_dir = profile / "user"
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "registrymodifications.xcu").write_text(_PROFILE_SETTINGS, encoding="utf-8")


def _soffice_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.lower() != "no_proxy"}
    env.update(_PROXY_ENV)
    return env


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

    async def _next_profile_dir(self) -> Path:
        global _profile_counter
        async with _profile_lock:
            _profile_counter += 1
            n = _profile_counter
        return Path(tempfile.gettempdir()) / f"lo_profile_{os.getpid()}_{n}"

    async def convert(self, data: bytes, src: str, dst: str, **opts) -> bytes:
        target = _CONVERT_TO.get(dst)
        if not target:
            raise ConversionFailedError(
                self.name, src, dst, f"LibreOffice has no filter for '{dst}'"
            )

        timeout = base.resolve_timeout(opts, 180)
        try:
            # Off the event loop: a large spreadsheet's XML takes seconds to parse.
            data = await asyncio.to_thread(strip_external_references, data)
        except UncheckableDocumentError as e:
            raise ConversionFailedError(
                self.name, src, dst,
                f"the document could not be checked for links to outside files ({e}); "
                f"it is corrupt or not really a .{src} file",
            ) from e
        profile = await self._next_profile_dir()
        _write_profile_settings(profile)
        # -env:UserInstallation needs a valid file:// URL on Windows too; see
        # path_to_file_url for why string concatenation does not do it.
        profile_url = path_to_file_url(profile)

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
            await self._run_command(argv, cwd=ws.path, src=src, dst=dst, timeout=timeout,
                                    env=_soffice_env())

            # soffice does not accept a custom output name; pick it up by extension
            try:
                produced = ws.find_one(f"input.{dst}")
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
