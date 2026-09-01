"""Full-route smoke test: actually run every route the capability matrix claims.

    uv run python scripts/sweep_routes.py

"The matrix says it is supported" is not the same as "it actually works"; this script
finds the gap. Every route goes through the full service layer (plan_conversion ->
run_plan) and the output is checked with **magic bytes and content checks**, not
just "no exception" or a byte count.

Why this is a standalone script rather than a pytest case:
  - It needs pandoc / libreoffice / tesseract all installed and takes about 70 s per
    run, which is too slow for every commit
  - It validates the actual behavior of "this machine + this set of engine versions",
    i.e. it is an environment check rather than a logic check; unit tests mock the
    environment away, this script does the opposite

When to run:
  - after upgrading firecrawl-anydoc / pandoc / LibreOffice
  - after changing the registry routing table or plan_conversion
  - after deploying to a new environment (engines and fonts inside the container)

Track record: the very first run caught 6 `-> rtf` routes producing RTF fragments
(missing the `{\\rtf1\\ansi` header) with a normal byte count and no exception;
only the header check revealed it.

The fixtures are deliberately Chinese so CJK fonts and OCR language packs are
exercised too.
"""
import asyncio
import io
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.convert.registry import capability_matrix, plan_conversion
from src.domain.convert.service import run_plan
from src.domain.convert.types import SourceFile

WORK = Path(tempfile.mkdtemp(prefix="sweep_"))
NEEDLE = "季度營運報告"

SRC_MD = f"""# {NEEDLE}

## 業績摘要

| 部門 | Q1 | Q2 |
|---|---|---|
| 業務 | 120 | 145 |

- 東南亞市場拓展
"""

# Target format -> magic bytes (missing = plain text, checked by content instead)
MAGIC = {
    "pdf": b"%PDF-",
    "docx": b"PK\x03\x04", "xlsx": b"PK\x03\x04", "pptx": b"PK\x03\x04",
    "odt": b"PK\x03\x04", "ods": b"PK\x03\x04", "odp": b"PK\x03\x04",
    "epub": b"PK\x03\x04",
    "doc": b"\xd0\xcf\x11\xe0", "xls": b"\xd0\xcf\x11\xe0", "ppt": b"\xd0\xcf\x11\xe0",
    "png": b"\x89PNG", "jpg": b"\xff\xd8\xff", "gif": b"GIF8",
    "bmp": b"BM", "tiff": (b"II*\x00", b"MM\x00*"),
    "rtf": b"{\\rtf",
}
TEXT_TARGETS = {"md", "html", "txt", "rst", "tex", "csv", "tsv", "json", "xml"}


def sh(*argv):
    return subprocess.run(argv, cwd=WORK, capture_output=True, timeout=180)


def build_fixtures() -> dict:
    f = {}
    (WORK / "s.md").write_text(SRC_MD, encoding="utf-8")

    # Plain-text formats are written directly
    f["md"] = SRC_MD.encode()
    f["txt"] = f"{NEEDLE}\n\n業績摘要\n東南亞市場拓展\n".encode()
    f["html"] = f"<html><body><h1>{NEEDLE}</h1><p>東南亞市場拓展</p>"\
                "<table><tr><td>業務</td><td>120</td></tr></table></body></html>".encode()
    f["rst"] = f"{NEEDLE}\n{'=' * 24}\n\n東南亞市場拓展\n".encode()
    f["tex"] = ("\\documentclass{article}\\usepackage{CJKutf8}\\begin{document}"
                f"\\section{{{NEEDLE}}}東南亞市場拓展\\end{{document}}").encode()
    f["csv"] = f"部門,Q1,Q2\n業務,120,145\n{NEEDLE},東南亞市場拓展,0\n".encode()
    f["tsv"] = f"部門\tQ1\n業務\t120\n{NEEDLE}\t東南亞市場拓展\n".encode()
    f["json"] = f'{{"title": "{NEEDLE}", "note": "東南亞市場拓展"}}'.encode()
    f["xml"] = f"<doc><title>{NEEDLE}</title><note>東南亞市場拓展</note></doc>".encode()

    # Produced by pandoc
    for target, ext in [("docx", "docx"), ("odt", "odt"), ("rtf", "rtf"),
                        ("epub3", "epub"), ("pptx", "pptx")]:
        r = sh("pandoc", "-f", "markdown", "-t", target, "--standalone",
               "-o", f"s.{ext}", "s.md")
        if r.returncode == 0:
            f[ext] = (WORK / f"s.{ext}").read_bytes()

    # Legacy formats and spreadsheets produced by LibreOffice
    def lo(src_name, target, ext, out_name):
        sh("soffice", "--headless", "--norestore", "--nolockcheck",
           f"-env:UserInstallation=file://{WORK}/lo_{out_name}",
           "--convert-to", target, "--outdir", str(WORK), src_name)
        produced = WORK / f"{Path(src_name).stem}.{ext}"
        if produced.exists():
            data = produced.read_bytes()
            produced.rename(WORK / f"{out_name}.{ext}")
            return data
        return None

    for src_name, target, ext, out in [
        ("s.docx", "doc", "doc", "g_doc"), ("s.docx", "pdf", "pdf", "g_pdf"),
        ("s.pptx", "ppt", "ppt", "g_ppt"), ("s.pptx", "odp", "odp", "g_odp"),
        ("s.csv", "xlsx", "xlsx", "g_xlsx"), ("s.csv", "xls", "xls", "g_xls"),
        ("s.csv", "ods", "ods", "g_ods"),
    ]:
        if not (WORK / src_name).exists():
            (WORK / "s.csv").write_bytes(f["csv"])
        data = lo(src_name, target, ext, out)
        if data:
            f[ext] = data

    # Images: always draw real text on them.
    # A blank image makes the OCR routes "fail", but that is a fixture without text,
    # not a broken engine; the first version of this sweep reported 12 false
    # failures for exactly that reason.
    import glob

    from PIL import Image, ImageDraw, ImageFont

    cjk = glob.glob("/usr/share/fonts/**/NotoSansCJK-Regular.ttc", recursive=True)
    font = ImageFont.truetype(cjk[0], 34) if cjk else ImageFont.load_default()
    for ext, fmt in [("png", "PNG"), ("jpg", "JPEG"), ("webp", "WEBP"),
                     ("gif", "GIF"), ("bmp", "BMP"), ("tiff", "TIFF")]:
        img = Image.new("RGB", (700, 220), (255, 255, 255))
        d = ImageDraw.Draw(img)
        d.text((25, 30), "Invoice INV-2026-0811", fill=(0, 0, 0), font=font)
        d.text((25, 90), NEEDLE, fill=(0, 0, 0), font=font)
        d.text((25, 150), "Total: 45800 TWD", fill=(0, 0, 0), font=font)
        b = io.BytesIO()
        img.save(b, fmt)
        f[ext] = b.getvalue()

    return f


def validate(dst: str, data: bytes, text_source: bool) -> str:
    """Return an empty string on success, otherwise the failure reason."""
    if not data:
        return "empty output"
    magic = MAGIC.get(dst)
    if magic:
        expected = magic if isinstance(magic, tuple) else (magic,)
        if not any(data.startswith(m) for m in expected):
            return f"magic bytes mismatch (got {data[:6]!r})"
        if len(data) < 200 and dst not in ("bmp",):
            return f"output too small ({len(data)}B)"
        return ""
    if dst in TEXT_TARGETS:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return "output is not UTF-8 text"
        if not text.strip():
            return "output is blank text"
        if text_source and NEEDLE not in text and "季度" not in text:
            return f"content lost (starts with {text.strip()[:40]!r})"
        return ""
    return ""


async def main():
    fixtures = build_fixtures()
    matrix = capability_matrix()
    total = sum(len(v) for v in matrix.values())
    print(f"Capability matrix claims {total} routes across {len(matrix)} source formats")
    missing = sorted(set(matrix) - set(fixtures))
    if missing:
        print(f"No fixture (skipped): {missing}")
    print()

    failures, skipped, passed = [], 0, 0
    t0 = time.perf_counter()
    for src in sorted(matrix):
        if src not in fixtures:
            skipped += len(matrix[src])
            continue
        for dst in matrix[src]:
            try:
                plan = plan_conversion(src, dst)
                source = SourceFile(data=fixtures[src], fmt=src, name="t")
                out = await run_plan(source, plan)
                problem = validate(dst, out, text_source=src not in
                                   {"png", "jpg", "webp", "gif", "bmp", "tiff"})
                if problem:
                    failures.append((src, dst, [r.engine for r in plan], problem))
                else:
                    passed += 1
            except Exception as e:
                failures.append((src, dst, ["?"], f"{type(e).__name__}: {str(e)[:90]}"))

    elapsed = time.perf_counter() - t0
    print(f"Ran {passed + len(failures)} routes / skipped {skipped}, took {elapsed:.0f}s")
    print(f"Passed {passed}, failed {len(failures)}")
    if failures:
        print("\nFailures:")
        for src, dst, engines, why in failures:
            print(f"  {src:>5} -> {dst:<5} [{','.join(engines):<22}] {why}")


asyncio.run(main())
