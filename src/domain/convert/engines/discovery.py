"""Cross-platform discovery of external executables

On Linux `shutil.which("soffice")` is enough: the package manager puts the
binary on PATH. **Windows is different**: LibreOffice installs into
`C:\\Program Files\\LibreOffice\\program\\` and by default is **not added to
PATH**, so `which` never finds it. tesseract and pandoc each have their own
default install locations too.

Without this layer, four of the six engines report NOT AVAILABLE on Windows and
the capability matrix shrinks to 44 routes: installed, yet unusable.
"""

import os
import shutil
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# Default install locations on Windows. Environment variables such as
# %ProgramFiles% are used instead of a hard-coded "C:\Program Files" because
# the folder name differs on non-English Windows (and the drive letter may not
# be C: either).
_WINDOWS_HINTS: dict[str, list[str]] = {
    "soffice": [
        r"%ProgramFiles%\LibreOffice\program\soffice.exe",
        r"%ProgramFiles(x86)%\LibreOffice\program\soffice.exe",
        r"%ProgramFiles%\LibreOffice 7\program\soffice.exe",
    ],
    "pandoc": [
        r"%ProgramFiles%\Pandoc\pandoc.exe",
        r"%LOCALAPPDATA%\Pandoc\pandoc.exe",
        r"%LOCALAPPDATA%\Programs\Pandoc\pandoc.exe",
    ],
    "tesseract": [
        r"%ProgramFiles%\Tesseract-OCR\tesseract.exe",
        r"%ProgramFiles(x86)%\Tesseract-OCR\tesseract.exe",
        r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe",
    ],
    "pdftoppm": [
        r"%ProgramFiles%\poppler\Library\bin\pdftoppm.exe",
        r"%ProgramFiles%\poppler\bin\pdftoppm.exe",
    ],
}

# If the installer placed dependencies next to the exe (portable layout), prefer
# those, so different LibreOffice versions on the same machine do not interfere.
_BUNDLED_SUBDIRS = ("engines", "vendor", "bin")

_cache: dict[str, str | None] = {}


def _bundle_root() -> Path | None:
    """Directory of the PyInstaller-built exe; None when not bundled

    `sys.frozen` is the flag PyInstaller sets. sys.executable is used rather
    than __file__: in onefile mode __file__ points into the extracted temp
    directory, while the dependencies sit next to the exe.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def find_binary(name: str) -> str | None:
    """Return the full path of an external executable, or None if not found

    Search order:
      1. engines/ vendor/ bin/ next to the exe (the installer's portable layout)
      2. PATH (the normal Linux case; also Windows users who added it by hand)
      3. known Windows install locations

    Results are cached: this function is called many times per capability
    matrix query, and each which() scans the whole PATH.
    """
    if name in _cache:
        return _cache[name]

    resolved = _search(name)
    _cache[name] = resolved
    return resolved


def _search(name: str) -> str | None:
    exe_name = f"{name}.exe" if IS_WINDOWS else name

    root = _bundle_root()
    if root:
        for subdir in _BUNDLED_SUBDIRS:
            for candidate in (root / subdir).rglob(exe_name):
                if candidate.is_file():
                    return str(candidate)

    found = shutil.which(name) or shutil.which(exe_name)
    if found:
        return found

    if IS_WINDOWS:
        for pattern in _WINDOWS_HINTS.get(name, []):
            expanded = Path(os.path.expandvars(pattern))
            # expandvars leaves undefined variables as %VAR%; such a path can
            # never exist, so no extra check is needed
            if expanded.is_file():
                return str(expanded)

    return None


def clear_cache() -> None:
    """Clear the discovery cache

    For tests, or to re-detect after installing a dependency without restarting.
    """
    _cache.clear()


def path_to_file_url(path: Path) -> str:
    """Convert a path to a file:// URL for LibreOffice's -env:UserInstallation

    String concatenation is not an option. Linux `/tmp/x` happens to become the
    correct `file:///tmp/x`, but Windows `C:\\Users\\x` would become
    `file://C:\\Users\\x`: one slash short, backslashes unconverted, and
    LibreOffice rejects it. `Path.as_uri()` is correct on both.
    """
    return path.resolve().as_uri()
