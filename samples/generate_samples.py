"""
Generate two reproducible test documents:

  clean_commercial_invoice.pdf  — crisp, all fields match ACME rules -> auto_approve
  messy_bill_of_lading.jpg      — low-res rotated noisy scan with a wrong incoterm
                                  and an ambiguous HS code -> human_review / amendment

Run:  python samples/generate_samples.py
"""
from __future__ import annotations

import io
import os

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))


def _font(size: int):
    for path in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Clean Commercial Invoice (PDF) — every field satisfies the ACME rule set.
# ---------------------------------------------------------------------------
def make_clean_pdf() -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4
    lines = [
        ("COMMERCIAL INVOICE", 20, 60),
        ("Seller: Shenzhen Tech Manufacturing Co., Ltd.", 11, 110),
        ("Consignee: Acme Imports Ltd.", 12, 135),
        ("Invoice No: INV-2024-0042", 12, 160),
        ("Date: 2024-03-11", 11, 182),
        ("", 11, 200),
        ("Port of Loading: Shanghai, CN", 12, 225),
        ("Port of Discharge: Rotterdam, NL", 12, 250),
        ("Incoterms: FOB Shanghai", 12, 275),
        ("", 11, 295),
        ("Description of Goods: 1,000 x Laptop computers (notebook computer)", 12, 320),
        ("HS Code: 8471.30", 12, 345),
        ("Gross Weight: 12,500 kg", 12, 370),
        ("Net Weight: 11,800 kg", 11, 392),
        ("", 11, 410),
        ("Total Value: USD 420,000.00", 11, 435),
        ("Country of Origin: China", 11, 460),
    ]
    for text, size, y in lines:
        if text:
            page.insert_text((60, y), text, fontsize=size, fontname="helv")
    out = os.path.join(HERE, "clean_commercial_invoice.pdf")
    doc.save(out)
    doc.close()
    print("wrote", out)


# ---------------------------------------------------------------------------
# Messy Bill of Lading (JPG) — low quality scan with problems baked in:
#   * Incoterms DDP  -> not in ACME allowlist (FOB/CIF) => mismatch -> amendment
#   * HS code partly obscured / ambiguous "8471.3O" (letter O) => low conf / mismatch
#   * rotation + blur + JPEG artefacts to stress the vision model
# ---------------------------------------------------------------------------
def make_messy_jpg() -> None:
    W, H = 1000, 1300
    img = Image.new("RGB", (W, H), (243, 240, 232))
    d = ImageDraw.Draw(img)
    title = _font(34)
    body = _font(24)
    faint = _font(22)

    d.text((60, 50), "BILL OF LADING", font=title, fill=(40, 40, 40))
    rows = [
        ("Shipper: Ningbo Components Ltd.", (40, 40, 40)),
        ("Consignee: Acme Imports Ltd.", (40, 40, 40)),
        ("B/L No: NBRT-77123", (40, 40, 40)),
        ("Port of Loading: Ningbo", (40, 40, 40)),
        ("Port of Discharge: Hamburg", (40, 40, 40)),
        ("Incoterms: DDP", (40, 40, 40)),  # WRONG for ACME
        ("Description: portable computers / laptop", (40, 40, 40)),
        ("HS Code: 8471.3O", (90, 90, 90)),  # faint + ambiguous O vs 0
        ("Gross Weight: 12,​480 kg", (40, 40, 40)),
        ("Invoice No: INV-2024-0043", (40, 40, 40)),
        ("Marks & Nos: ACME/RTM/0043", (120, 120, 120)),
    ]
    y = 140
    for text, color in rows:
        d.text((60, y), text, font=body if color == (40, 40, 40) else faint, fill=color)
        y += 70

    # Degrade: rotate slightly, blur, add JPEG noise via low quality save.
    img = img.rotate(-2.2, expand=True, fillcolor=(243, 240, 232))
    img = img.filter(ImageFilter.GaussianBlur(0.8))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=38)
    out = os.path.join(HERE, "messy_bill_of_lading.jpg")
    with open(out, "wb") as f:
        f.write(buf.getvalue())
    print("wrote", out)


# ---------------------------------------------------------------------------
# Incomplete Packing List (PDF) — a clean, legible document that genuinely OMITS
# two required fields (invoice number, HS code — a packing list often lacks them).
# The extractor returns value=null for these, the validator forces `uncertain`,
# and the router routes to HUMAN_REVIEW. Demonstrates the third outcome and the
# "never silently approve a missing field" guarantee.
# ---------------------------------------------------------------------------
def make_incomplete_pdf() -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    lines = [
        ("PACKING LIST", 20, 60),
        ("Exporter: Shanghai Logistics Co., Ltd.", 11, 110),
        ("Consignee: Acme Imports Ltd.", 12, 140),
        ("Port of Loading: Shanghai", 12, 170),
        ("Port of Discharge: Rotterdam", 12, 200),
        ("Incoterms: CIF", 12, 230),
        ("Description of Goods: Laptop computers, 40 cartons", 12, 260),
        ("Gross Weight: 12,450 kg", 12, 290),
        ("Net Weight: 11,700 kg", 11, 315),
        ("Dimensions: 40 x (60x40x30 cm)", 11, 340),
        # NOTE: no Invoice Number and no HS Code on this document — by design.
    ]
    for text, size, y in lines:
        page.insert_text((60, y), text, fontsize=size, fontname="helv")
    out = os.path.join(HERE, "incomplete_packing_list.pdf")
    doc.save(out)
    doc.close()
    print("wrote", out)


if __name__ == "__main__":
    make_clean_pdf()
    make_messy_jpg()
    make_incomplete_pdf()
