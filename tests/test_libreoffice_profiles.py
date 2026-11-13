"""LibreOffice profiles do not pile up in the temp directory (issue #22).

Every conversion ran with a profile /tmp/lo_profile_<pid>_<n> that was never
deleted: about 900 had piled up on one machine. Now each conversion deletes its
profile when it ends, whatever the outcome, after making sure no soffice still runs
with it, and the server sweeps stale profiles at startup without touching one that
another conversion is using.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from src.domain.convert.engines import libreoffice
from src.domain.convert.engines.base import CliEngine
from src.domain.convert.engines.libreoffice import (
    PROFILE_PREFIX,
    LibreOfficeEngine,
    _Profile,
    sweep_stale_profiles,
)
from src.domain.convert.types import ConversionFailedError

HAS_SOFFICE = LibreOfficeEngine().is_available()


@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    """A temp directory of this test's own, as tempfile.gettempdir() sees it."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _profiles(root: Path) -> list[Path]:
    return sorted(root.glob(f"{PROFILE_PREFIX}*"))


def _age(path: Path, seconds: float) -> None:
    """Make a profile look untouched for `seconds`."""
    then = time.time() - seconds
    for p in (path, path / "user"):
        if p.exists():
            os.utime(p, (then, then))


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1][0] != "Z"
    except (FileNotFoundError, ProcessLookupError):  # gone, possibly while being read
        return False


def _fake_run(behaviour):
    """A _run_command stand-in: records the profile it was given, then does `behaviour`."""
    seen = {}

    async def run(self, argv, cwd, src, dst, timeout=None, env=None):
        arg = next(a for a in argv if a.startswith("-env:UserInstallation="))
        profile = Path(arg.split("file://", 1)[1])
        seen["profile"] = profile
        seen["exists_during_run"] = profile.is_dir()
        seen["settings"] = (profile / "user" / "registrymodifications.xcu").read_text()
        seen["mode"] = profile.stat().st_mode & 0o777
        seen["tmpdir"] = env["TMPDIR"]
        await behaviour(cwd, dst)

    return run, seen


# ---------------------------------------------------------------------------
# One conversion, one profile, deleted at the end
# ---------------------------------------------------------------------------

async def _produce(cwd, dst):
    (Path(cwd) / f"input.{dst}").write_bytes(b"%PDF-1.4 fake")


async def _fail(cwd, dst):
    raise ConversionFailedError("libreoffice", "docx", dst, "exit code 1")


async def _time_out(cwd, dst):
    raise ConversionFailedError("libreoffice", "docx", dst, "timed out after 1s")


async def _produce_nothing(cwd, dst):
    return None


async def _cancelled(cwd, dst):
    raise asyncio.CancelledError()


@pytest.mark.parametrize("behaviour, raises", [
    (_produce, None),                              # TC-LOP-001 success
    (_fail, ConversionFailedError),                # TC-LOP-002 soffice failed
    (_time_out, ConversionFailedError),            # TC-LOP-003 timed out
    (_produce_nothing, ConversionFailedError),     # TC-LOP-004 exit 0, no output
    (_cancelled, asyncio.CancelledError),          # TC-LOP-005 request cancelled
])
async def test_the_profile_is_deleted_whatever_the_outcome(tmp_root, monkeypatch, behaviour, raises):
    run, seen = _fake_run(behaviour)
    monkeypatch.setattr(LibreOfficeEngine, "_run_command", run)
    monkeypatch.setattr(LibreOfficeEngine, "resolved_binary", lambda self: "soffice")
    if raises:
        with pytest.raises(raises):
            await LibreOfficeEngine().convert(b"plain text", "txt", "pdf")
    else:
        assert await LibreOfficeEngine().convert(b"plain text", "txt", "pdf") == b"%PDF-1.4 fake"
    assert seen["exists_during_run"]
    assert not seen["profile"].exists()
    assert _profiles(tmp_root) == []


async def test_the_profile_is_private_and_keeps_the_locked_settings(tmp_root, monkeypatch):
    """TC-LOP-006: the #18 hardening is still written into every new profile."""
    run, seen = _fake_run(_produce)
    monkeypatch.setattr(LibreOfficeEngine, "_run_command", run)
    monkeypatch.setattr(LibreOfficeEngine, "resolved_binary", lambda self: "soffice")
    await LibreOfficeEngine().convert(b"plain text", "txt", "pdf")
    assert seen["mode"] == 0o700
    # soffice's own temp files (left behind when it is killed) live in the profile
    assert Path(seen["tmpdir"]).parent == seen["profile"]
    assert seen["profile"].name.startswith(f"{PROFILE_PREFIX}{os.getpid()}_")
    for setting in ("BlockUntrustedRefererLinks", "DisableMacrosExecution", "proxy.invalid"):
        assert setting in seen["settings"]


async def test_concurrent_conversions_get_different_profiles(tmp_root, monkeypatch):
    """TC-LOP-007"""
    profiles = []

    async def run(self, argv, cwd, src, dst, timeout=None, env=None):
        profiles.append(next(a for a in argv if a.startswith("-env:UserInstallation=")))
        await asyncio.sleep(0.05)
        await _produce(cwd, dst)

    monkeypatch.setattr(LibreOfficeEngine, "_run_command", run)
    monkeypatch.setattr(LibreOfficeEngine, "resolved_binary", lambda self: "soffice")
    await asyncio.gather(*(LibreOfficeEngine().convert(b"x", "txt", "pdf") for _ in range(5)))
    assert len(set(profiles)) == 5
    assert _profiles(tmp_root) == []


def test_discard_kills_a_process_still_using_the_profile(tmp_root):
    """TC-LOP-008: a straggler soffice (here a stand-in) dies before its profile is deleted."""
    profile = _Profile()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                             f"-env:UserInstallation={profile.url}"])
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not libreoffice._pids_using_profile(profile.url):
            time.sleep(0.05)
        profile.discard()
        assert proc.wait(timeout=5) == -9
        assert not profile.path.exists()
    finally:
        proc.kill()
        proc.wait()


def test_discard_leaves_other_processes_alone(tmp_root):
    """TC-LOP-009: only the process started with this very profile is killed."""
    mine, other = _Profile(), _Profile()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                             f"-env:UserInstallation={other.url}"])
    try:
        time.sleep(0.3)
        mine.discard()
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()
        other.discard()


# ---------------------------------------------------------------------------
# The engine process and everything it started die on timeout or cancellation
# ---------------------------------------------------------------------------

class _Shell(CliEngine):
    name = "shell"
    binary = "sh"

    async def convert(self, data, src, dst, **opts):  # pragma: no cover - unused
        raise NotImplementedError


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


def _grandchild_script(pidfile: Path) -> list[str]:
    # Like soffice -> oosplash -> soffice.bin: the process we start starts another.
    # The grandchild does not hold our pipes, so nothing but the kill can end it.
    return ["sh", "-c", f"sleep 600 >/dev/null 2>&1 & echo $! > {pidfile}; wait"]


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
async def test_a_timeout_kills_the_whole_process_tree(tmp_path):
    """TC-LOP-010"""
    pidfile = tmp_path / "pid"
    with pytest.raises(ConversionFailedError, match="timed out"):
        await _Shell()._run_command(_grandchild_script(pidfile), cwd=tmp_path,
                                    src="a", dst="b", timeout=1)
    grandchild = int(pidfile.read_text())
    try:
        deadline = time.time() + 5
        while time.time() < deadline and _alive(grandchild):
            await asyncio.sleep(0.05)
        assert not _alive(grandchild)
    except AssertionError:
        _kill_quietly(grandchild)  # still alive: do not leave it behind
        raise


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
async def test_a_cancelled_request_kills_the_whole_process_tree(tmp_path):
    """TC-LOP-011"""
    pidfile = tmp_path / "pid"
    task = asyncio.create_task(_Shell()._run_command(_grandchild_script(pidfile), cwd=tmp_path,
                                                     src="a", dst="b", timeout=60))
    while not pidfile.exists() or not pidfile.read_text().strip():
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    grandchild = int(pidfile.read_text())
    try:
        deadline = time.time() + 5
        while time.time() < deadline and _alive(grandchild):
            await asyncio.sleep(0.05)
        assert not _alive(grandchild)
    except AssertionError:
        _kill_quietly(grandchild)  # still alive: do not leave it behind
        raise


# ---------------------------------------------------------------------------
# The startup sweep
# ---------------------------------------------------------------------------

def _legacy_profile(root: Path, name: str = "lo_profile_4242_7") -> Path:
    """A profile as versions before 1.2.2 left it: no in-use file."""
    path = root / name
    (path / "user").mkdir(parents=True)
    (path / "user" / "registrymodifications.xcu").write_text("<x/>")
    return path


def test_sweep_removes_old_leftovers(tmp_path):
    """TC-LOP-012"""
    old = _legacy_profile(tmp_path)
    _age(old, 2 * 3600)
    assert sweep_stale_profiles(tmp_path) == 1
    assert not old.exists()


def test_sweep_keeps_recent_profiles(tmp_path):
    """TC-LOP-013: a profile created moments ago may be about to be used."""
    recent = _legacy_profile(tmp_path)
    _age(recent, 60)
    assert sweep_stale_profiles(tmp_path) == 0
    assert recent.exists()


def test_sweep_keeps_a_profile_a_conversion_holds(tmp_root):
    """TC-LOP-014: the in-use lock protects it, however old it looks."""
    live = _Profile()
    try:
        _age(live.path, 10 * 3600)
        assert sweep_stale_profiles(tmp_root) == 0
        assert live.path.exists()
    finally:
        live.discard()


def test_sweep_keeps_a_profile_a_running_process_uses(tmp_path):
    """TC-LOP-015: even a pre-1.2.2 profile without a lock, while soffice runs with it."""
    used = _legacy_profile(tmp_path)
    _age(used, 10 * 3600)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                             f"-env:UserInstallation={used.resolve().as_uri()}"])
    try:
        time.sleep(0.3)
        assert sweep_stale_profiles(tmp_path) == 0
        assert used.exists()
    finally:
        proc.kill()
        proc.wait()


def test_sweep_skips_profiles_of_another_user(tmp_path, monkeypatch):
    """TC-LOP-016"""
    old = _legacy_profile(tmp_path)
    _age(old, 2 * 3600)
    monkeypatch.setattr(os, "geteuid", lambda: os.lstat(old).st_uid + 1)
    assert sweep_stale_profiles(tmp_path) == 0
    assert old.exists()


def test_sweep_never_follows_a_symlink(tmp_path):
    """TC-LOP-017: a lo_profile_* symlink to elsewhere is not a profile."""
    target = tmp_path / "precious"
    (target / "user").mkdir(parents=True)
    _age(target, 2 * 3600)
    link = tmp_path / "lo_profile_1_1"
    link.symlink_to(target)
    assert sweep_stale_profiles(tmp_path) == 0
    assert (target / "user").is_dir()


def test_sweep_ignores_other_names_and_files(tmp_path):
    """TC-LOP-018"""
    other = tmp_path / "not_a_profile"
    other.mkdir()
    stray = tmp_path / "lo_profile_file"
    stray.write_text("x")
    for p in (other, stray):
        _age(p, 2 * 3600)
    assert sweep_stale_profiles(tmp_path) == 0
    assert other.exists() and stray.exists()


def test_the_app_sweeps_the_temp_directory_at_startup(tmp_root):
    """TC-LOP-019: starting the app removes an old leftover from the temp directory."""
    from starlette.testclient import TestClient

    from app import create_app
    from src.config.model import (AppConfig, AuthConfig, ConfigModel, LoggingConfig,
                                  ModulesConfig, ServerConfig)

    old = _legacy_profile(tmp_root)
    _age(old, 2 * 3600)
    config = ConfigModel(
        server=ServerConfig(host="127.0.0.1", port=5050, transport="http"),
        auth=AuthConfig(enabled=False),
        logging=LoggingConfig(level="WARNING"),
        app=AppConfig(name="T", title="T"),
        modules=ModulesConfig(enabled=[]),
    )
    app = create_app(config, transport="http")
    assert old.exists()
    with TestClient(app):
        assert not old.exists()


# ---------------------------------------------------------------------------
# With the real LibreOffice
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_SOFFICE, reason="LibreOffice not installed")
async def test_real_conversions_leave_no_profile_behind(tmp_root, monkeypatch):
    """TC-LOP-020: a success, a corrupt input and a forced timeout, with soffice for real."""
    monkeypatch.setenv("TMPDIR", str(tmp_root))  # where soffice would put its own temp files
    engine = LibreOfficeEngine()
    pdf = await engine.convert(b"hello world\n", "txt", "pdf")
    assert pdf.startswith(b"%PDF")
    with pytest.raises(ConversionFailedError):
        await engine.convert(b"\x00\x01\x02 not a document at all", "doc", "pdf")
    with pytest.raises(ConversionFailedError, match="timed out"):
        await engine.convert(b"hello world, " * 200_000, "txt", "pdf", timeout=1)
    assert _profiles(tmp_root) == []
    assert not list(tmp_root.glob("lu*.tmp"))  # soffice's temp files, left when it is killed
    assert not [value for _, value in libreoffice._profile_args_in_use()
                if str(tmp_root.resolve()) in value]

