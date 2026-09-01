# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller bundle configuration. Run on Windows:
#     uv run pyinstaller desktop/anydoc.spec --noconfirm
#
# Produces dist/AnyDoc/AnyDoc.exe (onedir mode).
#
# Why onedir instead of onefile:
#   - onefile unpacks the whole bundle into %TEMP% on every start, adding 2-4 s
#     of cold start; that feels bad for a "drop a file, see a reaction" tool
#   - the installer puts the whole folder into Program Files anyway, so the
#     "single file" advantage of onefile is unused here
#   - discovery.py looks for portable dependencies in engines/ next to the exe;
#     onedir keeps that directory layout stable (with onefile, sys.executable
#     points at a temporary extraction dir whose path changes every run)

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

block_cipher = None

hidden = [
    # anydoc is a Rust extension module; PyInstaller static analysis cannot see inside
    "anydoc",
    # These are lazy imports (imported on use), also invisible to static analysis
    "pypdf",
    "PIL.Image",
    "PIL.ImageDraw",
    # Drag-and-drop support; without it the GUI degrades but keeps working
    "tkinterdnd2",
]

datas = []
binaries = collect_dynamic_libs("anydoc")

# tkinterdnd2 keeps the Tcl tkdnd extension inside its package dir; bundle it all
try:
    from PyInstaller.utils.hooks import collect_data_files

    datas += collect_data_files("tkinterdnd2")
except Exception:
    pass

# Service config and help text. The GUI does not read config.yaml, but a future
# --serve mode would, and instructions.md is the user-facing feature description.
datas += [
    ("../config/instructions.md", "config"),
]

a = Analysis(
    ["gui.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden + collect_submodules("src.domain.convert"),
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # The whole MCP server stack is unused by the desktop app.
        #
        # This list only works because src/log.py imports fastmcp lazily. Before
        # that, the conversion core was tied to the whole MCP framework through
        # the logger and excluding these made the program fail to start. After
        # the change desktop.converter pulls in only 73 top-level modules, with
        # no fastmcp / uvicorn / starlette / opentelemetry at all.
        #
        # loguru cannot be excluded: src/log.py uses it; it is a real domain dependency.
        "fastmcp", "mcp", "fastapi", "starlette", "uvicorn", "httpx",
        "opentelemetry", "watchfiles", "jsonschema", "pytest",
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AnyDoc",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX is a frequent antivirus false positive; not worth the risk
    console=False,      # GUI program: no black console window
    icon="anydoc.ico" if __import__("os").path.exists("desktop/anydoc.ico") else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="AnyDoc",
)
