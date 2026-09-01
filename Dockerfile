FROM python:3.13-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ---------------------------------------------------------------------------
# System dependencies of the conversion engines
#
# libreoffice-*  Only three components instead of the full libreoffice package,
#                saving about 400MB. writer=documents, calc=spreadsheets,
#                impress=presentations; drop one and its formats all break.
# pandoc         Text-format conversions.
# fonts-noto-cjk Required. Without it, CJK PDFs produced by LibreOffice render as
#                tofu boxes on every page, silently. The easiest thing to miss in
#                testing and the first thing to blow up in production.
# fonts-dejavu   Fallback for Latin text and symbols.
#
# This layer grows the image from ~200MB to ~1.2GB; that is the price of
# layout-faithful conversion.
# ---------------------------------------------------------------------------
# tesseract-ocr    OCR for images and scanned PDFs. chi-tra=Traditional Chinese,
#                  chi-sim=Simplified Chinese, eng=English. Without a language pack
#                  tesseract fails to start instead of degrading, so OCR dies.
# poppler-utils    Provides pdftoppm; scanned PDFs are rasterized before OCR.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        pandoc \
        tesseract-ocr \
        tesseract-ocr-chi-tra \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng \
        poppler-utils \
        fonts-noto-cjk \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --no-install-project --no-dev

COPY . .
RUN uv sync --no-dev

# The first LibreOffice start creates the user profile and takes a few seconds.
# Warm it up at build time so the first user request does not wait for nothing.
RUN soffice --headless --terminate_after_init >/dev/null 2>&1 || true

EXPOSE 5055

CMD ["uv", "run", "python", "main.py", "--config", "config/config.yaml"]
