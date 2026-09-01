"""Conversion routing table: (source format, target format) -> engine

This table is the capability boundary of the whole service. It is three
things at once:
  1. the dispatch source of truth at runtime
  2. the data behind the list_supported_conversions tool
  3. the human-readable "what do we support" document

All three share one dataset, so the declared capability and the implementation
**cannot** drift apart.
"""

from dataclasses import dataclass, replace

from src.domain.convert.engines.anydoc_engine import (
    SUPPORTED_SOURCES as ANYDOC_SOURCES,
)
from src.domain.convert.engines.anydoc_engine import (
    AnydocEngine,
)
from src.domain.convert.engines.base import ConversionEngine
from src.domain.convert.engines.image import ImageEngine
from src.domain.convert.engines.libreoffice import LibreOfficeEngine
from src.domain.convert.engines.ocr import OcrEngine
from src.domain.convert.engines.pandoc import PandocEngine
from src.domain.convert.engines.text import (
    SUPPORTED_SOURCES as TEXT_SOURCES,
)
from src.domain.convert.engines.text import (
    TextEngine,
)
from src.domain.convert.types import (
    IMAGE_FORMATS,
    NoConversionPathError,
    UnsupportedFormatError,
    normalize_format,
)
from src.log import get_mcptools_logger

logger = get_mcptools_logger()


# ============================================================================
# Fidelity levels
# ============================================================================
# These are not decorative labels; plan_conversion() makes decisions on them.

FIDELITY_HIGH = "high"              # layout, fonts, positions kept (LibreOffice, Pillow)
FIDELITY_STRUCTURAL = "structural"  # headings/lists/tables kept, layout is not (Pandoc)
FIDELITY_LOSSY = "lossy"            # content only, all styling lost (anydoc, OCR, TextEngine)

_FIDELITY_RANK = {FIDELITY_HIGH: 3, FIDELITY_STRUCTURAL: 2, FIDELITY_LOSSY: 1}


@dataclass(frozen=True)
class Route:
    """One conversion step (a single hop)"""
    src: str
    dst: str
    engine: str
    fidelity: str
    note: str = ""

    def describe(self) -> str:
        base = f"{self.src} → {self.dst} via {self.engine} ({self.fidelity})"
        return f"{base} — {self.note}" if self.note else base


# ============================================================================
# Engine instances (singletons)
# ============================================================================

ENGINES: dict[str, ConversionEngine] = {
    "libreoffice": LibreOfficeEngine(),
    "pandoc": PandocEngine(),
    "anydoc": AnydocEngine(),
    "pillow": ImageEngine(),
    "ocr": OcrEngine(),
    "text": TextEngine(),
}


# ============================================================================
# Direct route table
# ============================================================================

def _build_direct_routes() -> dict[tuple[str, str], Route]:
    routes: dict[tuple[str, str], Route] = {}

    def add(src: str, dst: str, engine: str, fidelity: str, note: str = ""):
        # First registration wins: when several engines can serve the same
        # (src, dst), the one registered earlier is the higher-quality choice.
        routes.setdefault((src, dst), Route(src, dst, engine, fidelity, note))

    # --- LibreOffice: Office family and PDF, layout-preserving ---
    office_sources = ["docx", "doc", "odt", "rtf", "xlsx", "xls", "ods",
                      "pptx", "ppt", "odp", "html", "txt"]
    for src in office_sources:
        add(src, "pdf", "libreoffice", FIDELITY_HIGH, "layout and fonts preserved")

    for src, dsts in {
        "docx": ["odt", "rtf", "doc", "html", "txt"],
        "doc": ["docx", "odt", "rtf"],
        "odt": ["docx", "rtf", "doc"],
        "rtf": ["docx", "odt"],
        "xlsx": ["ods", "csv", "html", "xls"],
        "xls": ["xlsx", "ods", "csv"],
        "ods": ["xlsx", "csv"],
        "csv": ["xlsx", "ods"],
        "pptx": ["odp", "ppt"],
        "ppt": ["pptx", "odp"],
        "odp": ["pptx"],
    }.items():
        for dst in dsts:
            add(src, dst, "libreoffice", FIDELITY_HIGH)

    # --- Engine overrides for text extraction (must be registered before Pandoc) ---
    # Several engines can serve the same (src, md); setdefault lets the first
    # registration win. Listed here are sources where anydoc is clearly better
    # than Pandoc in practice:
    #
    #   epub -> md   Pandoc emits <span id="ch001.xhtml"></span> and <div class=...>
    #                EPUB-internal anchors, which are pure noise for the model;
    #                anydoc produces clean headings and tables.
    #
    # docx / odt are deliberately not listed: Pandoc's output is just as clean
    # for them, and it keeps structural fidelity (round-trippable), which makes
    # it a better intermediate for multi-hop paths than anydoc's lossy output.
    add("epub", "md", "anydoc", FIDELITY_LOSSY, "text content extracted, styling not kept")

    # --- Pandoc: semantic conversion between text formats ---
    # pptx is writer-only: pandoc can build slides from the heading hierarchy
    # but cannot read pptx (reading pptx goes through anydoc or libreoffice).
    pandoc_readers = ["md", "html", "docx", "odt", "epub", "rst", "tex", "txt"]
    pandoc_writers = ["md", "html", "docx", "odt", "epub", "rst", "tex", "txt",
                      "rtf", "pptx"]
    for src in pandoc_readers:
        for dst in pandoc_writers:
            if src != dst:
                add(src, dst, "pandoc", FIDELITY_STRUCTURAL,
                    "structure preserved, layout is not")

    # --- anydoc: Office / PDF / EPUB -> md, content extraction ---
    # Registered after Pandoc, so formats both can read (docx / odt / epub) go
    # to Pandoc first (it keeps more structure). Only what Pandoc cannot read
    # (pdf / xlsx / pptx / csv) falls through to anydoc.
    #
    # Image formats are deliberately excluded: anydoc does not handle images;
    # those go to the OCR engine below.
    for src in sorted(ANYDOC_SOURCES):
        add(src, "md", "anydoc", FIDELITY_LOSSY, "text content extracted, styling not kept")

    # --- TextEngine: json / xml / tsv -> md ---
    for src in sorted(TEXT_SOURCES):
        add(src, "md", "text", FIDELITY_LOSSY, "plain-text wrapping, no structural parsing")

    # --- Tesseract OCR: image -> text ---
    for src in IMAGE_FORMATS:
        for dst in ("md", "txt"):
            add(src, dst, "ocr", FIDELITY_LOSSY, "OCR text recognition, text only")

    # --- Pillow: image-to-image ---
    for src in IMAGE_FORMATS:
        for dst in IMAGE_FORMATS:
            if src != dst:
                add(src, dst, "pillow", FIDELITY_HIGH)

    return routes


DIRECT_ROUTES: dict[tuple[str, str], Route] = _build_direct_routes()

# Candidate intermediate formats for multi-hop conversion, ordered from least
# to most lossy as a pivot.
# docx first: it is the only format Pandoc writes well AND LibreOffice reads
# well, so the most common request, md -> pdf, works precisely as
# md ->(pandoc)-> docx ->(soffice)-> pdf.
# md after html: it is the lossiest pivot of all (tables barely survive,
# images and styling are gone).
PIVOT_FORMATS: list[str] = ["docx", "html", "odt", "md", "pdf"]

# Maximum hops for the multi-hop search. 3 rather than 2 for coverage: e.g.
# epub -> pptx needs epub ->(pandoc)-> docx ->(pandoc)-> pptx. Every hop
# accumulates error, so at equal quality the ranking always prefers fewer hops.
MAX_HOPS = 3


# ============================================================================
# Queries
# ============================================================================

def available_engines() -> dict[str, bool]:
    """Report whether each engine is actually usable on this machine (soffice/pandoc present)"""
    return {name: engine.is_available() for name, engine in ENGINES.items()}


def direct_route(src: str, dst: str) -> Route | None:
    """Look up a single-hop route; treated as absent if unusable on this machine"""
    route = DIRECT_ROUTES.get((src, dst))
    if route and ENGINES[route.engine].can_handle(src, dst):
        return route
    return None


def _adjacency() -> dict[str, dict[str, Route]]:
    """Turn the route table into an adjacency map containing only edges usable here"""
    graph: dict[str, dict[str, Route]] = {}
    for (src, dst), route in DIRECT_ROUTES.items():
        if ENGINES[route.engine].can_handle(src, dst):
            graph.setdefault(src, {})[dst] = route
    return graph


def _search_paths(src: str, dst: str, max_hops: int) -> list[list[Route]]:
    """Enumerate every src -> dst path within max_hops

    The graph is tiny (about 150 edges, 30 nodes), so exhaustive search beats
    writing Dijkstra. Our cost function is also not simply additive (it takes
    the worst fidelity along the path), so Dijkstra's optimal-substructure
    assumption would not hold here anyway.
    """
    graph = _adjacency()
    found: list[list[Route]] = []

    def walk(node: str, path: list[Route], visited: set):
        if len(path) >= max_hops:
            return
        for nxt, route in graph.get(node, {}).items():
            if nxt in visited:
                continue
            if nxt == dst:
                found.append(path + [route])
                continue  # target reached; do not keep wandering past it
            walk(nxt, path + [route], visited | {nxt})

    walk(src, [], {src})
    return found


def _path_floor(path: list[Route]) -> int:
    """A path is only as good as its weakest hop: the bottleneck rule, no exceptions"""
    return min(_FIDELITY_RANK[r.fidelity] for r in path)


def _pivot_cost(path: list[Route]) -> int:
    """Preference cost of the intermediate formats; lower is better

    Fidelity labels only describe "the engine quality of a single hop"; they
    cannot capture "how good is this format as a pivot". Concrete case:
    md -> pdf has two paths, md->docx->pdf and md->html->pdf, each hop labelled
    structural + high, so their scores are identical. But LibreOffice's HTML
    import handles tables and layout far worse than its docx import, and the
    output differs a lot.

    The ordering of PIVOT_FORMATS encodes this observed pivot quality and is
    used here to break ties.
    """
    unknown = len(PIVOT_FORMATS)
    intermediates = [r.dst for r in path[:-1]]
    return sum(
        PIVOT_FORMATS.index(fmt) if fmt in PIVOT_FORMATS else unknown
        for fmt in intermediates
    )


def plan_conversion(src: str, dst: str, prefer_fidelity: bool = True) -> list[Route]:
    """Plan the src -> dst conversion path; returns the Routes to run in order

    Decision rules (applied in order):

    1. **If a direct path exists, take it regardless of fidelity.**
       When the user explicitly asks for "PDF to Markdown", lossy is what they
       want and must not be blocked. The quality threshold only constrains
       multi-hop paths that we chain together on our own initiative.

    2. **Multi-hop ranking: quality > hop count > pivot preference.**
       Path quality = fidelity of the weakest hop (bottleneck rule). When the
       first two tie, the PIVOT_FORMATS ordering decides; see _pivot_cost().

    3. **With prefer_fidelity, the quality floor for multi-hop paths is structural.**
       This blocks every path that "quietly passes through content extraction".

       **Note that this mode is off by default** (the tool layer's
       allow_quality_loss defaults to True). The default used to be the
       opposite: refuse any lossy path, on the grounds of "don't hand the user
       a file that looks successful but opens as text only". Real usage
       overturned that judgement:

         user asks "PDF to Word" -> refused -> the model falls back to pdf->md
         -> then tries md->docx -> guesses parameters 8 times -> succeeds after
         2.5 minutes, with a result identical to the direct conversion.

       The threshold did not prevent the bad outcome; it only made the same
       outcome take a long detour. The warning mechanism (_fidelity_warning in
       the tool layer) already spells out the loss, and the model did relay it
       to the user. So the default became "convert, and be explicit about it",
       with prefer_fidelity=True reserved for "the user explicitly said layout
       must not be lost".

    4. **When a lossy path is allowed, mark the loss in Route.note.**
       The tool layer's _fidelity_warning() turns it into a warning for the
       model, which relays it to the user. "Possible, but know the cost" beats
       both "refused" and "silently handed a poor file".

    Args:
        src: normalized source format code
        dst: normalized target format code
        prefer_fidelity: when True (default), reject lossy multi-hop paths

    Returns:
        Routes to execute in order; an empty list when src == dst

    Raises:
        NoConversionPathError: no path, or a path whose quality is not acceptable
    """
    if src == dst:
        return []

    # Rule 1: direct path first; no quality threshold on explicitly requested conversions
    direct = direct_route(src, dst)
    if direct:
        return [direct]

    # Rule 2: enumerate multi-hop paths, sorted by (quality desc, hops asc)
    paths = _search_paths(src, dst, MAX_HOPS)
    if not paths:
        raise NoConversionPathError(src, dst)

    # Ranking priority: quality > hop count > pivot preference
    paths.sort(key=lambda p: (-_path_floor(p), len(p), _pivot_cost(p)))
    best = paths[0]
    floor = _path_floor(best)

    # Rule 3: the quality threshold only constrains multi-hop paths
    if prefer_fidelity and floor < _FIDELITY_RANK[FIDELITY_STRUCTURAL]:
        lossy_hop = next(r for r in best if r.fidelity == FIDELITY_LOSSY)
        raise NoConversionPathError(
            src, dst,
            hint=(
                f"The only feasible path goes through content extraction at "
                f"{lossy_hop.src} → {lossy_hop.dst}; the original layout, tables and "
                f"images will be lost. If the user only needs the text and does not "
                f"care about appearance, retry with allow_quality_loss=true. If the "
                f"user needs the layout preserved, tell them this is technically "
                f"not possible."
            ),
        )

    # Rule 4: when allowing a lossy path, mark each lossy hop so the loss
    # travels all the way to the user
    if floor < _FIDELITY_RANK[FIDELITY_STRUCTURAL]:
        best = [
            replace(r, note=(r.note or "layout and styling are lost at this step"))
            if r.fidelity == FIDELITY_LOSSY else r
            for r in best
        ]

    logger.info(
        f"Planned {src} → {dst} in {len(best)} hop(s): "
        + " → ".join([best[0].src] + [r.dst for r in best])
    )
    return best


def resolve_formats(src_raw: str | None, dst_raw: str | None) -> tuple[str, str]:
    """Validate and normalize source/target formats; both must be supported"""
    src = normalize_format(src_raw)
    dst = normalize_format(dst_raw)
    if not src:
        raise UnsupportedFormatError(f"Unrecognized source format: '{src_raw}'")
    if not dst:
        raise UnsupportedFormatError(f"Unrecognized target format: '{dst_raw}'")
    return src, dst


def capability_matrix() -> dict[str, list[str]]:
    """Return {source format: [reachable target formats, ...]} for usable engines only

    This is the data source for the list_supported_conversions tool. Only
    direct routes are listed; multi-hop paths depend on plan_conversion's
    policy and are not promised here.
    """
    matrix: dict[str, list[str]] = {}
    for (src, dst), route in DIRECT_ROUTES.items():
        if ENGINES[route.engine].can_handle(src, dst):
            matrix.setdefault(src, []).append(dst)
    return {src: sorted(dsts) for src, dsts in sorted(matrix.items())}
