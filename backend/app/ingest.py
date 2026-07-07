"""
Document ingest: turn any uploaded trade doc (PDF or image) into a list of
PNG page images the vision model can read. PDFs are rendered with PyMuPDF
(no system dependency — runs on a laptop).
"""
from __future__ import annotations

import io

import fitz  # PyMuPDF
from PIL import Image

# Cap pages so a 200-page scan can't blow the vision budget on one document.
MAX_PAGES = 4
RENDER_DPI = 200


def to_page_images(data: bytes, filename: str) -> list[bytes]:
    name = filename.lower()
    if name.endswith(".pdf"):
        return _pdf_to_pngs(data)
    # Treat everything else as an image; normalise to PNG.
    return [_normalise_image(data)]


def _pdf_to_pngs(data: bytes) -> list[bytes]:
    pages: list[bytes] = []
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        zoom = RENDER_DPI / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        for page in doc[:MAX_PAGES]:
            pix = page.get_pixmap(matrix=matrix)
            pages.append(pix.tobytes("png"))
    finally:
        doc.close()
    return pages


def _normalise_image(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
