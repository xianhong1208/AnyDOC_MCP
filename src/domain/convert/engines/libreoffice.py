"""LibreOffice headless engine: Office-to-Office and Office-to-PDF

This is the only practical way to convert docx/xlsx/pptx to PDF while
**preserving layout**. Pandoc cannot do it (it understands semantic structure,
not layout), so every path that demands visual fidelity comes through here.
"""

import asyncio
import os
import shutil
import signal
import stat
import tempfile
import time
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
# deadlocks outright. So every conversion gets a profile of its own, a fresh
# private directory (mkdtemp, mode 0700) named lo_profile_<pid>_<random>, and
# deletes it when the conversion ends, however it ends. Profiles left by a
# crashed or killed server are removed by sweep_stale_profiles() at startup.
PROFILE_PREFIX = "lo_profile_"
# A file in the profile that a conversion holds an exclusive flock on for as long
# as it runs. The sweep never deletes a profile it cannot lock, so a profile in use
# by another worker, or another server sharing the temp directory, is left alone.
# The lock is released by the kernel when its holder dies, however it dies.
_IN_USE_FILE = ".morphyard-in-use"
# The sweep only deletes profiles untouched for longer than this. Every engine call
# has a timeout (LibreOffice: 180 s by default), so a live profile is never this old;
# the lock and the process check below protect it anyway.
STALE_PROFILE_AGE_SEC = 3600
_USER_INSTALLATION = "-env:UserInstallation="

try:
    import fcntl
except ImportError:  # Windows: no flock; the age and process checks still apply
    fcntl = None


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


def _soffice_env(temp_dir: Path | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.lower() != "no_proxy"}
    env.update(_PROXY_ENV)
    if temp_dir is not None:
        # soffice keeps its own temp files (lu<pid>….tmp) in TMPDIR and leaves them
        # behind when it is killed; inside the profile they go when the profile goes.
        env["TMPDIR"] = env["TMP"] = env["TEMP"] = str(temp_dir)
    return env


class _Profile:
    """A LibreOffice user profile for one conversion, removed when it ends."""

    def __init__(self, root: Path | None = None):
        root = Path(root or tempfile.gettempdir())
        self.path = Path(tempfile.mkdtemp(prefix=f"{PROFILE_PREFIX}{os.getpid()}_", dir=root))
        self._lock_fd: int | None = None
        try:
            self._lock_fd = os.open(self.path / _IN_USE_FILE, os.O_CREAT | os.O_RDWR, 0o600)
            if fcntl is not None:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            _write_profile_settings(self.path)
            self.temp_dir = self.path / "tmp"
            self.temp_dir.mkdir()
        except BaseException:
            self.discard()
            raise
        # -env:UserInstallation needs a valid file:// URL on Windows too; see
        # path_to_file_url for why string concatenation does not do it.
        self.url = path_to_file_url(self.path)

    def discard(self) -> None:
        """Make sure no soffice still runs with this profile, then delete it."""
        if getattr(self, "url", None):
            for pid in _pids_using_profile(self.url):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        shutil.rmtree(self.path, ignore_errors=True)
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None


def _profile_args_in_use() -> list[tuple[int, str]]:
    """(pid, -env:UserInstallation value) of every running process started with one.

    Linux only (/proc); elsewhere nothing is found and the lock and age checks remain.
    """
    found: list[tuple[int, str]] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    own = os.getpid()
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == own:
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        for arg in args:
            if arg.startswith(_USER_INSTALLATION.encode()):
                found.append((int(entry.name),
                              arg[len(_USER_INSTALLATION):].decode("utf-8", "replace")))
    return found


def _pids_using_profile(url: str) -> list[int]:
    return [pid for pid, value in _profile_args_in_use() if value == url]


def _newest_mtime(path: Path) -> float:
    """The latest change to the profile directory or its user/ directory (where soffice writes)."""
    newest = path.lstat().st_mtime
    try:
        newest = max(newest, (path / "user").lstat().st_mtime)
    except OSError:
        pass
    return newest


def _locked_by_someone(path: Path) -> bool:
    """True when another open conversion holds the profile's in-use lock."""
    if fcntl is None:
        return False
    try:
        fd = os.open(path / _IN_USE_FILE, os.O_RDWR)
    except OSError:
        return False  # no lock file: a profile from before 1.2.2, or one being torn down
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)  # closing releases the lock if we took it
    return False


def sweep_stale_profiles(root: Path | None = None,
                         min_age_sec: float = STALE_PROFILE_AGE_SEC) -> int:
    """Delete profiles left behind by earlier runs; returns how many were removed.

    A profile is removed only when all of these hold:
      - it is a real directory named lo_profile_* (a symlink is never followed);
      - it belongs to the user this server runs as;
      - nothing has changed in it for min_age_sec;
      - no conversion holds its in-use lock;
      - no running process was started with it as its LibreOffice profile.
    So a profile of a conversion running in another worker, or another server sharing
    the temp directory, is never touched.
    """
    root = Path(root or tempfile.gettempdir())
    now = time.time()
    uid = os.geteuid() if hasattr(os, "geteuid") else None
    in_use = [value.rstrip("/") for _, value in _profile_args_in_use()]
    removed = 0
    try:
        candidates = list(root.glob(f"{PROFILE_PREFIX}*"))
    except OSError:
        return 0
    for path in candidates:
        try:
            st = path.lstat()
            if not stat.S_ISDIR(st.st_mode):
                continue
            if uid is not None and st.st_uid != uid:
                continue
            if now - _newest_mtime(path) < min_age_sec:
                continue
        except OSError:
            continue
        if any(value.endswith("/" + path.name) for value in in_use):
            continue
        if _locked_by_someone(path):
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed += 1
    if removed:
        logger.info(f"[libreoffice] removed {removed} stale profile(s) from {root}")
    return removed


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
        profile = _Profile()
        try:
            with Workspace() as ws:
                ws.write(f"input.{src}", data)
                argv = [
                    self.resolved_binary(),
                    "--headless",
                    "--norestore",
                    "--nolockcheck",
                    "--nodefault",
                    f"{_USER_INSTALLATION}{profile.url}",
                    "--convert-to", target,
                    "--outdir", str(ws.path),
                    f"input.{src}",
                ]
                await self._run_command(argv, cwd=ws.path, src=src, dst=dst, timeout=timeout,
                                        env=_soffice_env(profile.temp_dir))

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
        finally:
            # Success, failure, timeout or a cancelled request: the profile goes.
            profile.discard()

        if not result:
            raise ConversionFailedError(self.name, src, dst, "empty output file")
        logger.info(f"[libreoffice] {src} → {dst}: {len(data)} → {len(result)} bytes")
        return result
