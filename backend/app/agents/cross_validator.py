"""
Cross-Validator Agent (Part 2) — checks that the documents in one shipment
agree WITH EACH OTHER, not just with the customer's rules.

Why this exists: a per-document check can pass on every document individually
while the shipment is still wrong — the invoice says HS 8471.30 and the packing
list says 8517.62, both of which are on ACME's allowlist. Only a cross-document
comparison catches that.

Same design stance as the per-doc validator: deterministic first (normalised
string / parsed quantity equality — free, and impossible to hallucinate), with
the LLM used ONLY for fuzzy name equivalence ("Acme Imports Ltd." vs "ACME
IMPORTS LIMITED"), batched into one cheap text call per shipment. An
inconclusive or failed semantic verdict degrades to `uncertain` (human review),
never to a silent pass.

A field present in fewer than two documents is skipped here — there is nothing
to cross-check, and the per-document validator already escalates missing
required fields on its own.
"""
from __future__ import annotations

import json
from typing import Optional

from app.agents.validator import _norm, _qty
from app.llm.client import LLMResult, get_client
from app.models import (
    CrossDocValue,
    CrossFieldCheck,
    CrossStatus,
    CrossValidationResult,
    ExtractedDocument,
    RunRecord,
)

# Fields that describe the one physical shipment and must therefore agree
# across BOL / Invoice / Packing List. doc_type and invoice presentation
# legitimately differ per document and are NOT cross-checked.
CROSS_FIELDS = [
    "consignee_name",
    "hs_code",
    "port_of_loading",
    "port_of_discharge",
    "incoterms",
    "gross_weight",
    "invoice_number",
]

# Fields where wording naturally varies between documents ("Shanghai" vs
# "Port of Shanghai, CN") — a deterministic mismatch defers to the semantic
# batch instead of flagging immediately.
_SEMANTIC_OK = {"consignee_name", "port_of_loading", "port_of_discharge"}

# Gross weights may differ slightly between documents (net vs gross rounding,
# re-weighing at the port). Within this band they are the same cargo.
_WEIGHT_TOLERANCE_PCT = 2.0


def _collect(docs: list[tuple[RunRecord, ExtractedDocument]], field: str) -> list[CrossDocValue]:
    out = []
    for rec, extracted in docs:
        ef = getattr(extracted, field, None)
        if ef is None:
            continue
        out.append(CrossDocValue(
            run_id=rec.run_id, filename=rec.filename, doc_type=extracted.doc_type.value,
            value=ef.value, source_quote=ef.source_quote, confidence=ef.confidence,
        ))
    return out


def _deterministic_verdict(field: str, present: list[CrossDocValue]) -> Optional[bool]:
    """True = all agree, False = definite conflict, None = needs semantic judgement."""
    values = [v.value for v in present]

    if field == "gross_weight":
        parsed = [_qty(v) for v in values]
        if any(num is None for num, _ in parsed):
            return None  # unparseable weight — let it fall through to uncertain
        units = {u for _, u in parsed if u is not None}
        if len(units) > 1:
            return False  # kg on one doc, lb on another: never silently equal
        nums = [num for num, _ in parsed]
        lo, hi = min(nums), max(nums)
        return (hi - lo) <= lo * _WEIGHT_TOLERANCE_PCT / 100.0

    if field == "incoterms":
        # Incoterms are written "<CODE> <named place>" ("FOB Shanghai") or bare
        # ("FOB") — Incoterms 2020 require the place, docs are inconsistent about
        # printing it. The CODE is what must agree (same token rule as the
        # per-doc allowlist check). Caught by live testing: "FOB" vs "FOB
        # Shanghai" flagged a clean shipment as an amendment.
        codes = {_norm(v).split()[0] for v in values if _norm(v)}
        return len(codes) == 1

    normed = {_norm(v) for v in values}
    if len(normed) == 1:
        return True
    if field in _SEMANTIC_OK:
        return None  # wording may differ legitimately — ask the semantic batch
    return False  # codes (HS, invoice no.) must agree verbatim


SEM_SYSTEM = """You are a trade-document validation assistant. Each item lists how several documents
from ONE shipment state the same field. Decide if all the stated values refer to the same real-world
thing (e.g. 'Acme Imports Ltd.' and 'ACME IMPORTS LIMITED' are the same consignee; 'Shanghai' and
'Port of Shanghai, CN' are the same port). Return ONLY JSON:
{"results":[{"field":..,"consistent":true|false,"reason":..}]}."""


def _resolve_semantic(pending: list[dict]) -> tuple[dict[str, dict], Optional[LLMResult]]:
    if not pending:
        return {}, None
    items = [{"field": p["field"], "values": p["values"]} for p in pending]
    user = "Judge each item:\n" + json.dumps(items, indent=2)
    # Call and parse are guarded separately: a malformed response still degrades
    # every pending field to uncertain, but the LLMResult survives so the call's
    # tokens/cost reach the ledger (same pattern as the per-doc validator).
    try:
        llm = get_client().text_json(SEM_SYSTEM, user)
    except Exception:  # provider outage -> all pending become uncertain
        return {}, None
    try:
        verdicts = {v["field"]: v for v in llm.json().get("results", []) if isinstance(v, dict)}
    except Exception:
        verdicts = {}
    return verdicts, llm


def cross_validate(
    docs: list[tuple[RunRecord, ExtractedDocument]],
) -> tuple[CrossValidationResult, Optional[LLMResult]]:
    checks: list[CrossFieldCheck] = []
    pending: list[dict] = []

    for field in CROSS_FIELDS:
        readings = _collect(docs, field)
        present = [r for r in readings if r.value is not None]
        if len(present) < 2:
            continue  # nothing to cross-check; per-doc validation owns missing fields

        verdict = _deterministic_verdict(field, present)
        if verdict is True:
            checks.append(CrossFieldCheck(
                field=field, status=CrossStatus.CONSISTENT, values=readings,
                reason=f"All {len(present)} documents agree.", method="deterministic"))
        elif verdict is False:
            checks.append(CrossFieldCheck(
                field=field, status=CrossStatus.INCONSISTENT, values=readings,
                reason="Documents state different values: "
                       + "; ".join(f"{v.filename}='{v.value}'" for v in present),
                method="deterministic"))
        else:
            pending.append({
                "field": field, "readings": readings,
                "values": [{"document": v.filename, "value": v.value} for v in present],
            })

    verdicts, llm = _resolve_semantic(pending)
    for p in pending:
        v = verdicts.get(p["field"])
        consistent = v.get("consistent") if isinstance(v, dict) else None
        # Strict boolean only — a stringy/missing verdict is INCONCLUSIVE, and
        # inconclusive means a human looks, never a silent pass (same invariant
        # as the per-doc validator).
        if not isinstance(consistent, bool):
            checks.append(CrossFieldCheck(
                field=p["field"], status=CrossStatus.UNCERTAIN, values=p["readings"],
                reason="Cross-document check inconclusive or unavailable — escalating to human review.",
                method="semantic"))
        else:
            checks.append(CrossFieldCheck(
                field=p["field"],
                status=CrossStatus.CONSISTENT if consistent else CrossStatus.INCONSISTENT,
                values=p["readings"],
                reason=(v.get("reason") or "Semantic judgement."), method="semantic"))

    order = {f: i for i, f in enumerate(CROSS_FIELDS)}
    checks.sort(key=lambda c: order.get(c.field, 999))
    return CrossValidationResult(
        checks=checks,
        has_inconsistent=any(c.status == CrossStatus.INCONSISTENT for c in checks),
        has_uncertain=any(c.status == CrossStatus.UNCERTAIN for c in checks),
    ), llm
