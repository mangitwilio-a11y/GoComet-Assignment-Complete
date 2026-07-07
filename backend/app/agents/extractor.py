"""
Extractor Agent — vision LLM reads a trade document and emits structured fields.

Hallucination is *reduced* (not eliminated) by contract:
  * every field must carry a `source_quote` — the text it was read from. No quote =>
    the value is ungrounded and the downstream validator treats it as uncertain,
    never as a silent pass.
  * `value` is null when the field is genuinely absent. The model is told that
    "not present" is a correct, rewarded answer — this is what stops it from
    inventing a plausible HS code to fill the blank.

Honest limitation: the model can fabricate a value AND a matching quote together
(observed: it normalized an ambiguous '8471.3O' to '8471.30' and quoted the
normalized form). So `source_quote` is EVIDENCE, not proof — it is not verified
against an independent OCR pass here. The real backstops are the deterministic
validator rules and the escalate-on-doubt routing policy.
"""
from __future__ import annotations

from app.llm.client import LLMResult, get_client
from app.models import ExtractedDocument, ExtractedField

FIELDS = [
    "doc_type",
    "consignee_name",
    "hs_code",
    "port_of_loading",
    "port_of_discharge",
    "incoterms",
    "description_of_goods",
    "gross_weight",
    "invoice_number",
]

SYSTEM = """You are a meticulous trade-document extraction engine. You read Bills of Lading,
Commercial Invoices, Packing Lists and Certificates of Origin and return ONLY structured JSON.

Hard rules:
1. Extract a value ONLY if it is actually visible in the document. If a field is not present,
   return value=null. Inventing a value is a critical failure; "not present" is the correct answer.
2. For every field, provide `source_quote`: the exact text from the document you read the value
   from (copy it verbatim, including surrounding words if helpful). If you cannot point to text,
   set source_quote=null and confidence below 0.4.
3. `confidence` (0.0-1.0) reflects how legible and unambiguous the source text is — a crisp printed
   value is ~0.95; a smudged/handwritten/partially-cut value is ~0.5; an inferred or guessed value
   is <0.4. Do NOT report high confidence for a value you had to guess.
4. Normalise lightly: trim whitespace; keep HS codes and invoice numbers exactly as printed;
   keep gross_weight with its unit (e.g. "12,500 kg").
"""

USER = """Extract these fields and return a JSON object with EXACTLY these keys:
doc_type, consignee_name, hs_code, port_of_loading, port_of_discharge, incoterms,
description_of_goods, gross_weight, invoice_number.

Each key maps to an object: {"value": <string|null>, "confidence": <0..1>, "source_quote": <string|null>}.

doc_type is the kind of document (e.g. "Bill of Lading", "Commercial Invoice").
incoterms is the trade term code (e.g. FOB, CIF, EXW).
Return only the JSON object, nothing else."""


def _coerce_field(raw: dict | None) -> ExtractedField:
    if not isinstance(raw, dict):
        return ExtractedField(value=None, confidence=0.0, source_quote=None)
    val = raw.get("value")
    conf = raw.get("confidence", 0.0)
    quote = raw.get("source_quote")
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    # Guardrail: a value with no supporting quote cannot be high-confidence.
    if val is not None and not quote:
        conf = min(conf, 0.39)
    return ExtractedField(
        value=str(val) if val is not None else None,
        confidence=conf,
        source_quote=str(quote) if quote else None,
    )


def extract(page_images: list[bytes]) -> tuple[ExtractedDocument, LLMResult]:
    result = get_client().vision_json(SYSTEM, USER, page_images)
    data = result.json()
    fields = {name: _coerce_field(data.get(name)) for name in FIELDS}
    return ExtractedDocument(**fields), result
