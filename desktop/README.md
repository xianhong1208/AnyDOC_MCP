# AnyDoc Desktop

A drag-and-drop conversion tool shipped as a Windows executable. **It shares the
conversion core with the MCP service**: `src/domain/convert/` is reused as-is and
the desktop app is just another front end.

```
                    +- src/fastmcp_tools/  -> MCP tools (called by the AI)
src/domain/convert/ +
   routing table    +- desktop/           -> GUI (used directly by people)
   multi-hop planning
   quality gate
   image preservation
   OCR fallback
```

Fix a bug once and both sides get it; the capability matrix, multi-hop logic and
image handling are identical.

---

## Files

| File | Purpose |
|------|---------|
| `converter.py` | Desktop facade: disk path -> domain layer -> written back to disk |
| `gui.py` | tkinter UI: drag and drop, batches, progress |
| `anydoc.spec` | PyInstaller configuration |
| `installer.iss` | Inno Setup installer (downloads and installs dependencies) |
| `build.ps1` | One-shot Windows build |

---

## Building (Windows only)

PyInstaller **cannot cross-compile**; a Windows exe cannot be produced on Linux.

```powershell
cd anydoc
powershell -ExecutionPolicy Bypass -File desktop\build.ps1
```

Output:

```
dist\AnyDoc\AnyDoc.exe              portable (about 60MB, install dependencies yourself)
Output\AnyDoc-Setup-1.0.0.exe       installer (about 50MB, installs dependencies for you)
```

### Prerequisites

- **Python 3.13** with **tcl/tk** ticked during installation. Without it there is
  no tkinter: every build step succeeds and the missing window is only discovered
  when a user double-clicks the exe. Step 2 of `build.ps1` exists to catch this.
- `uv`
- **Inno Setup 6.3 or newer**. 6.2 has no built-in `CreateDownloadPage`, so the
  installer's download page fails to compile.

---

## Dependency strategy

LibreOffice is about 350MB, pandoc 30MB, tesseract 60MB. Bundling all three
makes a 450MB installer that has to be redistributed for every one-line code
change. They are **downloaded at install time** instead:

| Component | Without it | Source |
|-----------|-----------|--------|
| **LibreOffice** | No PDF / Word / Excel / PPT output (85 routes fewer) | documentfoundation.org |
| **pandoc** | No Markdown / HTML <-> Word / presentation conversion | github.com/jgm/pandoc |
| **tesseract** | No text recognition for scans and images | UB Mannheim |
| **poppler** | No OCR for scanned PDFs (no rasterization) | github.com/oschwartz10612 |

This also sidesteps the licensing question of redistributing GPL (pandoc) and MPL
(LibreOffice) binaries: the installer only guides the user to the official sources.

**The installer detects first** and skips anything already installed. LibreOffice
is often present already, and reinstalling it is slow and may overwrite the user's
settings. The detection paths match `discovery.py`.

**A failed download does not fail the install.** AnyDoc itself is still installed
with a reduced capability matrix, and tells the user at startup exactly which
features are missing and how to get them.

### Offline environments

Install-time downloads need network access. For an internal network, fetch the
four files by hand and use the portable `dist\AnyDoc\` together with a
pre-installed LibreOffice / pandoc, or unzip poppler into `engines\poppler\` next
to `AnyDoc.exe`; `discovery.py` finds it automatically.

---

## Cross-platform notes

On Linux `shutil.which("soffice")` is enough; Windows is nothing like that.
`src/domain/convert/engines/discovery.py` handles three things:

1. **LibreOffice is not on PATH.** It lives in `%ProgramFiles%\LibreOffice\program\`
   and `which` never finds it. Without this layer, four of the six engines report
   NOT AVAILABLE on Windows and the capability matrix shrinks to 44 routes even
   though everything is installed.
2. **`file://` URLs.** LibreOffice's `-env:UserInstallation` must be a valid URL.
   String concatenation happens to work on Linux (`/tmp/x` -> `file:///tmp/x`),
   but `C:\Users\x` becomes `file://C:\Users\x` on Windows: one slash short and
   backslashes unconverted, so LibreOffice rejects it. `Path.as_uri()` is right on
   both.
3. **Component launchers carry `.exe`**: `swriter` vs `swriter.exe`.

The search order is "`engines\` next to the exe -> PATH -> known Windows install
locations", so the portable build works with dependencies placed next to the exe
and never touches the system PATH.

---

## Usage

- **Drag and drop**: drop one or more files onto the window
- **Context menu**: select files in Explorer -> "Convert with AnyDoc"
- **Drop onto the exe icon**: Windows passes the paths as argv
- **Buttons**: click the drop zone or "Choose files..."

With several files the target formats are the **intersection**: only formats every
file can reach are listed. Offering a format some files cannot reach means the user
picks it and gets half successes, half failures, which is more confusing than a
shorter list.

Output goes next to the source by default; a folder can be chosen. **Existing files
are never overwritten**: if `report.pdf` exists, the result is saved as
`report (1).pdf`, following the Explorer convention.

---

## Known limitations

- **Drag and drop needs `tkinterdnd2`.** Without it the app degrades to
  button-only file picking and the UI otherwise works; an important safety net in
  locked-down environments where extra packages cannot be installed.
- **UPX compression is deliberately off.** It would save about 15MB but is a
  frequent antivirus false positive, which is not worth the risk.
- **onedir rather than onefile.** onefile unpacks into `%TEMP%` on every start,
  adding 2-4 s of cold start, and `discovery.py` needs a stable directory layout
  next to the exe; onefile's `sys.executable` points at a different temporary
  directory every run.
