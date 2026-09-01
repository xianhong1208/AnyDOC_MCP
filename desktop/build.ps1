#
# Windows build script: produces dist\AnyDoc\AnyDoc.exe and Output\AnyDoc-Setup-1.0.0.exe
#
#     cd anydoc
#     powershell -ExecutionPolicy Bypass -File desktop\build.ps1
#
# Prerequisites:
#   - Python 3.13 (tick tcl/tk during install, otherwise there is no tkinter
#     and the GUI cannot start)
#   - uv           https://docs.astral.sh/uv/
#   - Inno Setup 6.3+ (needs the built-in download page API; 6.2 and below
#     have no CreateDownloadPage)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "[1/5] Installing dependencies" -ForegroundColor Cyan
uv sync
uv sync --group desktop

Write-Host "[2/5] Checking tkinter" -ForegroundColor Cyan
# Do not skip this. If tcl/tk was not ticked when installing Python on Windows,
# every step before this succeeds and the missing window is only discovered when
# a user double-clicks the exe, after it has already been distributed.
uv run python -c "import tkinter; r=tkinter.Tk(); r.destroy(); print('  tkinter OK')"

Write-Host "[3/5] Checking the conversion core" -ForegroundColor Cyan
uv run python -c @"
import sys; sys.path.insert(0, '.')
from desktop.converter import engine_status
status = engine_status()
print('  engines:', status)
if not status.get('anydoc'):
    raise SystemExit('firecrawl-anydoc is not installed; the bundled exe could not read documents')
"@

Write-Host "[4/5] PyInstaller bundle" -ForegroundColor Cyan
uv run pyinstaller desktop\anydoc.spec --noconfirm --clean

if (-not (Test-Path "dist\AnyDoc\AnyDoc.exe")) {
    throw "Bundle failed: dist\AnyDoc\AnyDoc.exe not found"
}
$size = (Get-ChildItem dist\AnyDoc -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ("  output size: {0:N0} MB" -f $size)

Write-Host "[5/5] Building the installer with Inno Setup" -ForegroundColor Cyan
$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) {
    Write-Warning "Inno Setup not found ($iscc); only the exe was produced, no installer"
    Write-Host "exe location: dist\AnyDoc\AnyDoc.exe" -ForegroundColor Green
    exit 0
}
& $iscc desktop\installer.iss

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "  portable exe: dist\AnyDoc\AnyDoc.exe"
Write-Host "  installer:    Output\AnyDoc-Setup-1.0.0.exe"
