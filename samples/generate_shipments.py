"""
Generate two reproducible sample SHIPMENTS (Part 2) — one SU email each, with a
full three-document set (Bill of Lading + Commercial Invoice + Packing List):

  shipments/clean_shipment/  — all three docs satisfy ACME rules AND agree with
                               each other -> auto_approve + approval draft
  shipments/messy_shipment/  — every doc passes ACME's rules INDIVIDUALLY, but
                               the docs disagree with each other:
                                 * HS code: invoice/BOL say 8471.30, packing
                                   list says 8523.51 (both on the allowlist!)
                                 * gross weight: BOL says 13,050 kg vs 12,500 kg
                                   elsewhere (each within the per-doc ±5%)
                               -> only the cross-document check catches it
                               -> amendment + discrepancy email draft

That contrast is the point: per-document validation alone would approve the
messy shipment. Run:  python samples/generate_shipments.py
"""
from __future__ import annotations

import json
import os

import fitz  # PyMuPDF

HERE = os.path.dirname(os.path.abspath(__file__))
SHIPMENTS = os.path.join(HERE, "shipments")


def make_pdf(out_path: str, title: str, rows: list[tuple[str, int]]) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4
    page.insert_text((60, 70), title, fontsize=20, fontname="helv")
    y = 120
    for text, size in rows:
        if text:
            page.insert_text((60, y), text, fontsize=size, fontname="helv")
        y += 26 if size >= 12 else 22
    doc.save(out_path)
    doc.close()
    print("wrote", out_path)


def write_email(folder: str, meta: dict) -> None:
    # email.json is written LAST — the watcher treats its presence as "complete".
    with open(os.path.join(folder, "email.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print("wrote", os.path.join(folder, "email.json"))


def _docs(folder: str, *, hs_invoice: str, hs_packing: str, hs_bol: str,
          weight_invoice: str, weight_packing: str, weight_bol: str,
          consignee_bol: str) -> None:
    inv_no = "INV-2024-0101"

    make_pdf(os.path.join(folder, "commercial_invoice.pdf"), "COMMERCIAL INVOICE", [
        ("Seller: Shenzhen Tech Manufacturing Co., Ltd.", 11),
        (f"Consignee: Acme Imports Ltd.", 12),
        (f"Invoice No: {inv_no}", 12),
        ("Date: 2024-04-02", 11),
        ("Port of Loading: Shanghai, CN", 12),
        ("Port of Discharge: Rotterdam, NL", 12),
        ("Incoterms: FOB Shanghai", 12),
        ("Description of Goods: 1,000 x Laptop computers (notebook computer)", 12),
        (f"HS Code: {hs_invoice}", 12),
        (f"Gross Weight: {weight_invoice}", 12),
        ("Total Value: USD 420,000.00", 11),
        ("Country of Origin: China", 11),
    ])

    make_pdf(os.path.join(folder, "bill_of_lading.pdf"), "BILL OF LADING", [
        ("Shipper: Shenzhen Tech Manufacturing Co., Ltd.", 11),
        (f"Consignee: {consignee_bol}", 12),
        ("B/L No: SHRT-88451", 12),
        (f"Invoice Reference: {inv_no}", 11),
        ("Port of Loading: Shanghai", 12),
        ("Port of Discharge: Rotterdam", 12),
        ("Incoterms: FOB", 12),
        ("Description: Laptop computers", 12),
        (f"HS Code: {hs_bol}", 12),
        (f"Gross Weight: {weight_bol}", 12),
        ("Vessel: MV NORDIC STAR / Voyage 114W", 11),
    ])

    make_pdf(os.path.join(folder, "packing_list.pdf"), "PACKING LIST", [
        ("Exporter: Shenzhen Tech Manufacturing Co., Ltd.", 11),
        ("Consignee: Acme Imports Ltd.", 12),
        (f"Invoice No: {inv_no}", 12),
        ("Port of Loading: Shanghai", 12),
        ("Port of Discharge: Rotterdam", 12),
        ("Incoterms: FOB", 12),
        ("Description of Goods: Laptop computers, 40 cartons", 12),
        (f"HS Code: {hs_packing}", 12),
        (f"Gross Weight: {weight_packing}", 12),
        ("Net Weight: 11,800 kg", 11),
        ("Dimensions: 40 x (60x40x30 cm)", 11),
    ])


def make_clean() -> None:
    folder = os.path.join(SHIPMENTS, "clean_shipment")
    os.makedirs(folder, exist_ok=True)
    _docs(folder,
          hs_invoice="8471.30", hs_packing="8471.30", hs_bol="8471.30",
          weight_invoice="12,500 kg", weight_packing="12,500 kg", weight_bol="12,480 kg",
          consignee_bol="Acme Imports Ltd.")
    write_email(folder, {
        "from": "dispatch@shenzhen-tech.example",
        "subject": "Shipment INV-2024-0101 — docs for ACME (BOL + CI + PL)",
        "customer": "ACME-IMPORTS",
        "body": "Hi CG team, please find attached the document set for the laptop "
                "shipment on MV NORDIC STAR. Kindly confirm so we can dispatch. — SU",
    })


def make_messy() -> None:
    folder = os.path.join(SHIPMENTS, "messy_shipment")
    os.makedirs(folder, exist_ok=True)
    _docs(folder,
          # Every value below passes ACME's per-document rules. The shipment is
          # still wrong — the documents contradict each other.
          hs_invoice="8471.30", hs_packing="8523.51", hs_bol="8471.30",
          weight_invoice="12,500 kg", weight_packing="12,500 kg", weight_bol="13,050 kg",
          consignee_bol="ACME IMPORTS LIMITED")  # semantic variant — should still match
    write_email(folder, {
        "from": "dispatch@shenzhen-tech.example",
        "subject": "Shipment INV-2024-0101 — corrected docs for ACME",
        "customer": "ACME-IMPORTS",
        "body": "Hi CG team, resending the full set after your last note. "
                "Everything should be fine now. — SU",
    })


if __name__ == "__main__":
    make_clean()
    make_messy()
