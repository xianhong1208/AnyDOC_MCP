"""Landing page route."""

import html
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["Index"])



# Lightweight Markdown -> HTML converter
def _inline(text: str) -> str:
    """Apply inline Markdown syntax (code, bold, links).

    Args:
        text: Text that has already been HTML-escaped.

    Returns:
        HTML fragment with inline syntax applied.
    """
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def markdown_to_html(md: str) -> str:
    """Convert Markdown to HTML: headings, lists, code blocks, bold, links and rules.

    Args:
        md: Raw Markdown string.

    Returns:
        Converted HTML string (all content is HTML-escaped).
    """
    lines = md.split("\n")
    out: list[str] = []
    para: list[str] = []
    list_type: str | None = None
    i = 0

    def flush_para():
        if para:
            text = " ".join(para).strip()
            if text:
                out.append(f"<p>{_inline(text)}</p>")
            para.clear()

    def close_list():
        nonlocal list_type
        if list_type:
            out.append(f"</{list_type}>")
            list_type = None

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code block ```
        if stripped.startswith("```"):
            flush_para()
            close_list()
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(html.escape(lines[i]))
                i += 1
            out.append("<pre><code>" + "\n".join(buf) + "</code></pre>")
            i += 1
            continue

        # Blank line
        if not stripped:
            flush_para()
            close_list()
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^(-{3,}|\*{3,})$", stripped):
            flush_para()
            close_list()
            out.append("<hr>")
            i += 1
            continue

        # Heading #..######
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(html.escape(m.group(2)))}</h{level}>")
            i += 1
            continue

        # Unordered list - / *
        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            flush_para()
            if list_type != "ul":
                close_list()
                out.append("<ul>")
                list_type = "ul"
            out.append(f"<li>{_inline(html.escape(m.group(1)))}</li>")
            i += 1
            continue

        # Ordered list 1. 2. ...
        m = re.match(r"^\d+\.\s+(.*)$", stripped)
        if m:
            flush_para()
            if list_type != "ol":
                close_list()
                out.append("<ol>")
                list_type = "ol"
            out.append(f"<li>{_inline(html.escape(m.group(1)))}</li>")
            i += 1
            continue

        # Plain paragraph
        para.append(html.escape(stripped))
        i += 1

    flush_para()
    close_list()
    return "\n".join(out)


# Page template
_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%%TITLE%%</title>
%%FAVICON%%
<style>
:root{--brand:#0a4d8c;--brand-dark:#063a6b;--bg:#f5f7fa;--card:#fff;--text:#1f2933;--muted:#6b7280;--border:#e5e7eb;}
*{box-sizing:border-box;}
body{margin:0;font-family:-apple-system,"Segoe UI","PingFang TC","Microsoft JhengHei",sans-serif;background:var(--bg);color:var(--text);line-height:1.65;}
.hero{background:linear-gradient(135deg,var(--brand),var(--brand-dark));color:#fff;padding:52px 24px 60px;text-align:center;}
.badge{display:inline-block;background:rgba(255,255,255,.2);padding:3px 12px;border-radius:999px;font-size:.85rem;letter-spacing:.5px;}
.hero h1{margin:10px 0 6px;font-size:2.1rem;}
.hero p{margin:8px auto 0;opacity:.92;max-width:560px;}
.actions{margin-top:26px;display:flex;gap:12px;justify-content:center;flex-wrap:wrap;}
.btn{display:inline-flex;align-items:center;gap:8px;padding:12px 22px;border-radius:8px;font-weight:600;text-decoration:none;transition:transform .05s,box-shadow .2s,background .2s;}
.btn-primary{background:#fff;color:var(--brand);box-shadow:0 2px 10px rgba(0,0,0,.18);}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(0,0,0,.22);}
.btn-ghost{background:transparent;color:#fff;border:1px solid rgba(255,255,255,.6);}
.btn-ghost:hover{background:rgba(255,255,255,.14);}
.container{max-width:880px;margin:-32px auto 40px;padding:0 24px;}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:32px 38px;box-shadow:0 6px 28px rgba(0,0,0,.07);}
.card h1,.card h2,.card h3{color:var(--brand-dark);}
.card h1{margin-top:8px;}
.card h2{border-bottom:1px solid var(--border);padding-bottom:6px;margin-top:30px;}
.card code{background:#eef2f7;padding:2px 6px;border-radius:4px;font-size:.9em;}
.card pre{background:#0f172a;color:#e2e8f0;padding:16px;border-radius:8px;overflow-x:auto;}
.card pre code{background:none;padding:0;color:inherit;}
.card a{color:var(--brand);}
.footer{text-align:center;color:var(--muted);font-size:.85rem;padding:8px 24px 40px;}
.footer code{background:#e5e7eb;padding:2px 6px;border-radius:4px;}
.topbar{background:#fff;padding:14px 24px;text-align:center;border-bottom:1px solid var(--border);}
.wordmark{font-weight:700;font-size:20px;letter-spacing:-0.01em;color:#1E3A5F;}
</style>
</head>
<body>
%%TOPBAR%%
  <div class="hero">
    <span class="badge">v%%VERSION%%</span>
    <h1>%%TITLE%%</h1>
    <p>%%DESC%%</p>
    <div class="actions">
      <a class="btn btn-primary" href="/docs">📘 API docs (Swagger)</a>
      <a class="btn btn-ghost" href="/health">❤ Health check</a>
    </div>
  </div>
  <div class="container">
    <div class="card">
%%INSTRUCTIONS%%
    </div>
  </div>
  <div class="footer">
    AnyDoc &middot
    MIT License &middot
    MCP endpoint: <code>/mcp</code>
  </div>
</body>
</html>"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    """Service landing page: name, version, a link to /docs and the instructions text.

    Args:
        request: Incoming request, used to read the app's title/version/description
            and instructions.

    Returns:
        Rendered landing page as an HTMLResponse.
    """
    app = request.app
    title = getattr(app, "title", "MCP Service")
    version = getattr(app, "version", "0.0.0")
    description = getattr(app, "description", "") or "AnyDoc MCP service"

    instructions_md = getattr(app.state, "instructions", "") or ""
    instructions_html = (
        markdown_to_html(instructions_md) if instructions_md
        else "<p>(No instructions provided yet.)</p>"
    )

    topbar = '  <div class="topbar"><span class="wordmark">AnyDoc</span></div>'
    favicon = ""

    page = (
        _PAGE
        .replace("%%FAVICON%%", favicon)
        .replace("%%TOPBAR%%", topbar)
        .replace("%%TITLE%%", html.escape(title))
        .replace("%%VERSION%%", html.escape(str(version)))
        .replace("%%DESC%%", html.escape(description))
        .replace("%%INSTRUCTIONS%%", instructions_html)
    )
    return HTMLResponse(content=page)
