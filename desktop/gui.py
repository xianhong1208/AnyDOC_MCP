"""AnyDoc desktop: drag-and-drop conversion.

Why tkinter: it ships with the Python standard library and adds only about 10MB
to the bundle. PySide6 would add 150MB and a more complicated license (LGPL),
which is not worth it for a "drop file, pick format, click convert" tool.

Drag and drop comes from tkinterdnd2. It is the only extra dependency and
**nothing breaks without it**: the app degrades to the "Choose files" button,
which matters in locked-down environments.

The screen is deliberately a top-to-bottom three-step flow: pick files -> pick
format -> convert. A first-time user knows the next step without reading any
help; each step's controls stay disabled until the previous one is done.

Threading model: conversion runs in a background thread and reports progress to
the UI thread through a queue. tkinter is not thread-safe and every widget
operation must happen on the main thread, so the worker only pushes messages
onto the queue and after() polling drains them.
"""

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from desktop import theme
from desktop.converter import (
    ConversionResult,
    convert_file,
    engine_status,
    missing_engine_hint,
    target_formats_for,
)

APP_TITLE = "AnyDoc Document Converter"

_FORMAT_LABELS = {
    "pdf": "PDF document",
    "docx": "Word document (.docx)",
    "doc": "Word 97-2003 (.doc)",
    "odt": "OpenDocument text (.odt)",
    "rtf": "RTF document",
    "epub": "EPUB e-book",
    "xlsx": "Excel workbook (.xlsx)",
    "xls": "Excel 97-2003 (.xls)",
    "ods": "OpenDocument spreadsheet",
    "csv": "CSV (comma-separated)",
    "pptx": "PowerPoint (.pptx)",
    "ppt": "PowerPoint 97-2003 (.ppt)",
    "odp": "OpenDocument presentation",
    "md": "Markdown",
    "html": "HTML web page",
    "txt": "Plain text",
    "rst": "reStructuredText",
    "tex": "LaTeX",
    "png": "PNG image",
    "jpg": "JPEG image",
    "webp": "WebP image",
    "gif": "GIF image",
    "bmp": "BMP image",
    "tiff": "TIFF image",
}


def _label_for(fmt: str) -> str:
    return _FORMAT_LABELS.get(fmt, fmt.upper())


class AnyDocApp:
    def __init__(self, root: tk.Tk, initial_files: list[Path] | None = None):
        self.root = root
        self.files: list[Path] = []
        self.format_codes: list[str] = []
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None

        root.title(APP_TITLE)
        root.geometry("640x720")
        root.minsize(560, 640)
        self.fonts = theme.apply(root)

        self._build_ui()
        self._enable_drag_and_drop()
        self._report_engine_state()
        self._sync_controls()
        self.root.after(80, self._drain_events)

        if initial_files:
            self.add_files(initial_files)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(24, 20, 24, 20))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
        self.engine_label = ttk.Label(outer, text="", style="Muted.TLabel")
        self.engine_label.pack(anchor="w", pady=(2, 16))

        self._build_step_one(outer)
        self._build_step_two(outer)
        self._build_step_three(outer)
        self._build_results(outer)

    def _step_heading(self, parent: ttk.Frame, number: str, text: str) -> ttk.Frame:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, 6))
        ttk.Label(row, text=f"{number}  {text}", style="Step.TLabel").pack(side="left")
        return row

    def _build_step_one(self, parent: ttk.Frame) -> None:
        header = self._step_heading(parent, "①", "Choose files")
        self.clear_button = ttk.Button(
            header, text="Clear", style="Secondary.TButton", command=self.clear
        )

        # The drop zone is a tk.Label, not ttk: ttk does not let you change the
        # background directly, and the hover color change is the clearest
        # "you can drop here" cue.
        self.drop_zone = tk.Label(
            parent,
            text="Drop files here\nor click to choose",
            font=self.fonts["drop"],
            bg=theme.DROP_IDLE, fg=theme.TEXT_MUTED,
            relief="solid", borderwidth=1,
            height=5, cursor="hand2",
        )
        self.drop_zone.pack(fill="x")
        self.drop_zone.bind("<Button-1>", lambda _e: self.browse())
        self.drop_zone.bind("<Enter>", lambda _e: self._highlight_drop(True))
        self.drop_zone.bind("<Leave>", lambda _e: self._highlight_drop(False))

        self.file_summary = ttk.Label(parent, text="", style="Muted.TLabel")
        self.file_summary.pack(anchor="w", pady=(8, 4))

        list_wrap = tk.Frame(parent, bg=theme.BORDER, bd=0)
        list_wrap.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(
            list_wrap, selectmode="extended", height=6,
            font=self.fonts["body"], bg=theme.SURFACE, fg=theme.TEXT,
            borderwidth=0, highlightthickness=0,
            activestyle="none", selectbackground="#dbe6fb", selectforeground=theme.TEXT,
        )
        scrollbar = ttk.Scrollbar(list_wrap, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        scrollbar.pack(side="right", fill="y")

    def _build_step_two(self, parent: ttk.Frame) -> None:
        ttk.Frame(parent, height=16).pack()
        self._step_heading(parent, "②", "Choose output format")

        self.format_var = tk.StringVar()
        self.format_box = ttk.Combobox(
            parent, textvariable=self.format_var, state="disabled",
            font=self.fonts["body"],
        )
        self.format_box.pack(fill="x")
        self.format_hint = ttk.Label(parent, text="", style="Muted.TLabel")
        self.format_hint.pack(anchor="w", pady=(4, 0))

    def _build_step_three(self, parent: ttk.Frame) -> None:
        ttk.Frame(parent, height=16).pack()
        self._step_heading(parent, "③", "Convert")

        self.convert_button = ttk.Button(
            parent, text="Convert", style="Accent.TButton",
            command=self.start_conversion, state="disabled",
        )
        self.convert_button.pack(fill="x")

        # Not packed while idle: an empty progress bar is pure visual noise and
        # makes people think the app is stuck. It appears when conversion starts.
        self.progress = ttk.Progressbar(
            parent, mode="determinate", style="Thin.Horizontal.TProgressbar"
        )

    def _build_results(self, parent: ttk.Frame) -> None:
        ttk.Frame(parent, height=14).pack()
        wrap = tk.Frame(parent, bg=theme.BORDER)
        wrap.pack(fill="both", expand=True)
        self.results = tk.Text(
            wrap, height=7, state="disabled", wrap="word",
            font=self.fonts["small"], bg=theme.SURFACE, fg=theme.TEXT,
            borderwidth=0, highlightthickness=0, padx=10, pady=8,
        )
        self.results.pack(fill="both", expand=True, padx=1, pady=1)
        self.results.tag_configure("ok", foreground=theme.OK)
        self.results.tag_configure("err", foreground=theme.ERR)
        self.results.tag_configure("warn", foreground=theme.WARN)
        self.results.tag_configure("muted", foreground=theme.TEXT_MUTED)

    def _highlight_drop(self, active: bool) -> None:
        self.drop_zone.configure(
            bg=theme.DROP_ACTIVE if active else theme.DROP_IDLE,
            fg=theme.ACCENT if active else theme.TEXT_MUTED,
        )

    # ------------------------------------------------------------------
    # Drag and drop
    # ------------------------------------------------------------------

    def _enable_drag_and_drop(self) -> None:
        """Wire up tkinterdnd2; without it, quietly degrade to button-only picking.

        Locked-down environments often cannot install extra packages; a tool that
        breaks outright when one is missing is the worst possible design.
        """
        try:
            from tkinterdnd2 import DND_FILES  # noqa: F401

            self.drop_zone.drop_target_register("DND_Files")
            self.drop_zone.dnd_bind("<<Drop>>", self._on_drop)
            self.drop_zone.dnd_bind("<<DragEnter>>", lambda _e: self._highlight_drop(True))
            self.drop_zone.dnd_bind("<<DragLeave>>", lambda _e: self._highlight_drop(False))
        except (ImportError, AttributeError, tk.TclError):
            self.drop_zone.configure(text="Click here to choose files")

    def _on_drop(self, event) -> None:
        self._highlight_drop(False)
        self.add_files([Path(p) for p in _split_dnd_paths(event.data) if p])

    # ------------------------------------------------------------------
    # File list
    # ------------------------------------------------------------------

    def browse(self) -> None:
        chosen = filedialog.askopenfilenames(title="Choose files to convert")
        if chosen:
            self.add_files([Path(p) for p in chosen])

    def add_files(self, paths: list[Path]) -> None:
        added = 0
        for path in paths:
            if path.is_dir():
                # A dropped folder contributes only its top level, no recursion:
                # recursing easily pulls in thousands of files, which is rarely
                # what the user meant
                for child in sorted(path.iterdir()):
                    if child.is_file() and child not in self.files:
                        self.files.append(child)
                        added += 1
            elif path.is_file() and path not in self.files:
                self.files.append(path)
                added += 1

        if added:
            self._sync_controls()

    def clear(self) -> None:
        self.files.clear()
        self._sync_controls()

    # ------------------------------------------------------------------
    # Control state
    # ------------------------------------------------------------------

    def _sync_controls(self) -> None:
        """Refresh the whole UI from the current file list.

        Centralized in one method because "are there files" affects five controls
        at once (list, count, format menu, hint, convert button). Spread around,
        one would eventually be missed and "Convert enabled with no files" would
        appear.
        """
        self.listbox.delete(0, "end")
        for path in self.files:
            self.listbox.insert("end", f"  {path.name}")

        if not self.files:
            self.file_summary.configure(text="No files selected")
            self.clear_button.pack_forget()
            self.format_box.configure(values=[], state="disabled")
            self.format_var.set("")
            self.format_hint.configure(text="")
            self.convert_button.configure(state="disabled")
            self.format_codes = []
            return

        self.file_summary.configure(text=f"{len(self.files)} file(s) selected")
        self.clear_button.pack(side="right")

        options = self._common_targets()
        self.format_codes = options
        self.format_box.configure(
            values=[_label_for(f) for f in options],
            state="readonly" if options else "disabled",
        )

        if options:
            preferred = "pdf" if "pdf" in options else options[0]
            self.format_var.set(_label_for(preferred))
            self.convert_button.configure(state="normal")
            self.format_hint.configure(
                text=f"These files share {len(options)} output format(s)"
                if len(self.files) > 1 else ""
            )
        else:
            self.format_var.set("")
            self.convert_button.configure(state="disabled")
            self.format_hint.configure(
                text="These files have no output format in common; convert them in batches"
            )

    def _common_targets(self) -> list[str]:
        """Intersection, not union.

        Listing a format some files cannot reach means the user picks it and gets
        half successes, half failures; that is more confusing than a shorter list.
        """
        common: set | None = None
        for path in self.files:
            formats = set(target_formats_for(path))
            common = formats if common is None else (common & formats)
        return sorted(common or set())

    def _selected_format(self) -> str | None:
        label = self.format_var.get()
        for code in self.format_codes:
            if _label_for(code) == label:
                return code
        return None

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def start_conversion(self) -> None:
        target = self._selected_format()
        if not target or not self.files:
            return
        if self.worker and self.worker.is_alive():
            return

        output_dir = filedialog.askdirectory(
            title="Choose an output folder (cancel to save next to the originals)"
        )
        destination = Path(output_dir) if output_dir else None

        self.convert_button.configure(state="disabled", text="Converting...")
        self.progress.configure(maximum=len(self.files), value=0)
        # after= is required: pack() appends to the end of the child sequence by
        # default and the results area is already packed, so without it the
        # progress bar would land at the very bottom of the window.
        self.progress.pack(fill="x", pady=(10, 0), after=self.convert_button)
        self.log(f"Converting {len(self.files)} file(s) -> {_label_for(target)}", "muted")

        self.worker = threading.Thread(
            target=self._run_conversion,
            args=(list(self.files), target, destination),
            daemon=True,
        )
        self.worker.start()

    def _run_conversion(self, files, target, destination) -> None:
        """Background thread: only pushes to the queue, never touches widgets.

        tkinter is not thread-safe; touching widgets from another thread crashes
        randomly, and the symptoms rarely point at the real cause.
        """
        for index, path in enumerate(files, start=1):
            result = convert_file(path, target, output_dir=destination)
            self.events.put(("result", index, result))
        self.events.put(("done", len(files), None))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, index, payload = self.events.get_nowait()
                if kind == "result":
                    self._show_result(index, payload)
                elif kind == "done":
                    self.convert_button.configure(state="normal", text="Convert")
                    self.progress.pack_forget()
                    self.log("Done.", "muted")
        except queue.Empty:
            pass
        self.root.after(80, self._drain_events)

    def _show_result(self, index: int, result: ConversionResult) -> None:
        self.progress.configure(value=index)
        if result.ok:
            self.log(f"✓  {result.source.name}  ->  {result.output.name}", "ok")
            if result.lossy:
                self.log(
                    "     Layout was reflowed; the original images are collected at the end "
                    "of the document and may need adjusting",
                    "warn",
                )
        else:
            self.log(f"✕  {result.source.name}: {result.message}", "err")

    # ------------------------------------------------------------------

    def log(self, text: str, tag: str = "") -> None:
        self.results.configure(state="normal")
        self.results.insert("end", text + "\n", tag)
        self.results.see("end")
        self.results.configure(state="disabled")

    def _report_engine_state(self) -> None:
        hint = missing_engine_hint()
        if hint:
            self.engine_label.configure(text="Some features are unavailable")
            self.log("⚠  " + hint, "warn")
        else:
            ready = sum(1 for ok in engine_status().values() if ok)
            self.engine_label.configure(text=f"Conversion engines ready ({ready})")


def _split_dnd_paths(raw: str) -> list[str]:
    """Parse the tkdnd path string.

    Paths containing spaces are wrapped in braces: `{C:\\My Docs\\a.docx} C:\\b.pdf`.
    A plain split(" ") would cut "My Docs" in two and yield two nonexistent paths.
    """
    paths, buffer, in_brace = [], "", False
    for char in raw:
        if char == "{":
            in_brace = True
        elif char == "}":
            in_brace = False
            paths.append(buffer)
            buffer = ""
        elif char == " " and not in_brace:
            if buffer:
                paths.append(buffer)
            buffer = ""
        else:
            buffer += char
    if buffer:
        paths.append(buffer)
    return paths


def main() -> None:
    initial = [Path(a) for a in sys.argv[1:] if Path(a).exists()]

    # TkinterDnD.Tk() is the prerequisite for drag and drop; fall back to plain
    # Tk when the package is missing. The UI works as usual, just without drops.
    try:
        from tkinterdnd2 import TkinterDnD

        root = TkinterDnD.Tk()
    except Exception:
        root = tk.Tk()

    AnyDocApp(root, initial_files=initial)
    root.mainloop()


if __name__ == "__main__":
    main()
