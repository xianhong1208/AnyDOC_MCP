"""Colors, fonts and ttk styles for the GUI.

Kept in its own module so gui.py can focus on behavior: layout and appearance
tweaks never need to touch any logic.

Fonts **must be set explicitly** rather than relying on the platform default. Tk's
default font lacks CJK glyphs in some environments, and the symptom is "the whole
CJK string vanishes" rather than tofu boxes, which looks like a broken program
instead of a missing font. Each platform gets a list of fonts known to include CJK.
"""

import sys
import tkinter.font as tkfont
from tkinter import ttk

# -- Palette ---------------------------------------------------------------
# Neutral, slightly cool grays with one blue accent. Deliberately not vivid:
# this is a tool people look at every day, and calm beats eye-catching.
BG = "#f5f6f8"          # window background
SURFACE = "#ffffff"     # cards / input areas
BORDER = "#d9dde4"
BORDER_STRONG = "#b8bfc9"
TEXT = "#1f2430"
TEXT_MUTED = "#6b7280"
ACCENT = "#2563eb"
ACCENT_HOVER = "#1d4ed8"
ACCENT_DISABLED = "#9db6ea"
OK = "#15803d"
ERR = "#b91c1c"
WARN = "#a16207"
DROP_IDLE = "#fbfcfd"
DROP_ACTIVE = "#eef4ff"

# -- Fonts -----------------------------------------------------------------
# First candidate that actually exists, per platform. Microsoft JhengHei UI is the
# Traditional Chinese system font on Windows, PingFang TC on macOS, and Linux
# usually has Noto Sans CJK TC.
_FAMILY_CANDIDATES = {
    "win32": ["Microsoft JhengHei UI", "Microsoft JhengHei", "Segoe UI"],
    "darwin": ["PingFang TC", "Heiti TC", "Helvetica Neue"],
}
_FALLBACK = ["Noto Sans CJK TC", "Noto Sans TC", "WenQuanYi Zen Hei", "DejaVu Sans"]

SIZE_TITLE = 15
SIZE_BODY = 10
SIZE_SMALL = 9
SIZE_DROP = 12


def pick_family() -> str:
    """Pick a font that exists on this machine and includes CJK glyphs.

    tkfont.families() does not list fontconfig fonts in some Linux environments,
    so when nothing matches we still return the first candidate and let Tk resolve
    it; better than forcing a default font known to lack CJK glyphs.
    """
    candidates = _FAMILY_CANDIDATES.get(sys.platform, []) + _FALLBACK
    try:
        available = {f.lower() for f in tkfont.families()}
    except Exception:
        return candidates[0]
    for family in candidates:
        if family.lower() in available:
            return family
    return candidates[0]


def apply(root) -> dict:
    """Apply the styles and return the font set for the caller to use."""
    family = pick_family()
    fonts = {
        "title": (family, SIZE_TITLE, "bold"),
        "body": (family, SIZE_BODY),
        "body_bold": (family, SIZE_BODY, "bold"),
        "small": (family, SIZE_SMALL),
        "drop": (family, SIZE_DROP),
        "mono": ("Consolas" if sys.platform == "win32" else "monospace", SIZE_SMALL),
    }

    root.configure(bg=BG)

    style = ttk.Style(root)
    # clam is the only built-in theme that gives full control over colors. The
    # default vista/aqua themes ignore background settings, so button colors
    # silently refuse to change with no error message.
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(".", background=BG, foreground=TEXT, font=fonts["body"])
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=SURFACE, relief="flat")
    style.configure("TLabel", background=BG, foreground=TEXT, font=fonts["body"])
    style.configure("Title.TLabel", font=fonts["title"], foreground=TEXT)
    style.configure("Muted.TLabel", foreground=TEXT_MUTED, font=fonts["small"])
    style.configure("Step.TLabel", font=fonts["body_bold"], foreground=TEXT)

    # Primary action button: white on blue. map() handles hover and disabled;
    # without a disabled color the disabled state looks identical to clickable.
    style.configure(
        "Accent.TButton",
        background=ACCENT, foreground="#ffffff",
        font=fonts["body_bold"], padding=(18, 10),
        borderwidth=0, focuscolor=ACCENT,
    )
    style.map(
        "Accent.TButton",
        background=[("disabled", ACCENT_DISABLED), ("active", ACCENT_HOVER)],
        foreground=[("disabled", "#eef2ff")],
    )

    style.configure(
        "Secondary.TButton",
        background=SURFACE, foreground=TEXT,
        font=fonts["body"], padding=(12, 8),
        borderwidth=1, relief="solid",
    )
    style.map("Secondary.TButton", background=[("active", "#eef1f5")])

    style.configure(
        "TCombobox",
        fieldbackground=SURFACE, background=SURFACE,
        bordercolor=BORDER, arrowsize=14, padding=6,
    )
    # The progress bar needs both borderwidth and troughrelief turned off. With
    # only borderwidth, clam still draws a sunken 3D groove; in a flat UI that
    # gray ring stands out and looks unfinished.
    style.configure(
        "Thin.Horizontal.TProgressbar",
        background=ACCENT, troughcolor="#e4e8ee",
        borderwidth=0, troughrelief="flat", relief="flat",
        thickness=6, pbarrelief="flat",
        # clam draws a 3D border from bordercolor / lightcolor / darkcolor; only
        # setting all of them to the base colors makes it truly flat.
        # borderwidth=0 alone is not enough.
        bordercolor="#e4e8ee", lightcolor=ACCENT, darkcolor=ACCENT,
    )

    return fonts
