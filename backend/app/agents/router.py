"""
Router / Decision Agent — turns a validation result into one of three outcomes
and explains itself.

The *decision* is a deterministic tree (auditable, can't hallucinate the verdict):
  any uncertain field        -> HUMAN_REVIEW   (we can't even tell if it's right)
  else any mismatch          -> AMENDMENT      (clear, fixable supplier error)
  else                       -> AUTO_APPROVE   (all match, all confident, all grounded)

The LLM is used only for what it's good at: writing the human-readable reasoning
and, for amendments, drafting the supplier email listing each discrepancy.
"""
from __future__ import annotations

from typing import Optional

from app.llm.client import LLMResult, get_client
from app.models import (
    CrossStatus,
    CrossValidationResult,
    Decision,
    FieldStatus,
    Outcome,
    RunRecord,
    ShipmentDecision,
    ValidationResult,
)


def _decide(validation: ValidationResult) -> Outcome:
    if validation.has_uncertain:
        return Outcome.HUMAN_REVIEW
    if validation.has_mismatch:
        return Outcome.AMENDMENT
    return Outcome.AUTO_APPROVE


REASON_SYSTEM = """You write concise, factual operations notes for a non-technical trade-ops
operator. Be specific and reference the actual fields. Never invent facts beyond what is given.
Return ONLY JSON: {"reasoning": "...", "amendment_draft": "..."} (amendment_draft empty unless asked)."""


def _build_user(outcome: Outcome, validation: ValidationResult, customer: str) -> str:
    lines = []
    for r in validation.results:
        lines.append(f"- {r.field}: {r.status.value} | found='{r.found}' expected='{r.expected}' "
                     f"(conf {r.confidence:.2f}) :: {r.reason}")
    table = "\n".join(lines)
    base = f"Customer: {customer}\nDecision: {outcome.value}\nField results:\n{table}\n\n"
    if outcome == Outcome.AUTO_APPROVE:
        return base + ("Write a one-line reasoning confirming all fields matched with sufficient "
                       "confidence. Leave amendment_draft empty.")
    if outcome == Outcome.HUMAN_REVIEW:
        return base + ("Write reasoning naming exactly which fields are uncertain and why a human "
                       "must look. Leave amendment_draft empty.")
    return base + ("Write reasoning naming the mismatched fields. Then write amendment_draft: a short "
                   "professional email to the supplier requesting correction, listing each discrepancy "
                   "as 'field: found X, expected Y'.")


def route(validation: ValidationResult, customer: str) -> tuple[Decision, Optional[LLMResult]]:
    outcome = _decide(validation)  # deterministic — this verdict stands even if the LLM is down
    user = _build_user(outcome, validation, customer)
    llm: Optional[LLMResult] = None
    reasoning = None
    draft = None
    # The LLM call is wrapped: a provider/network failure (or malformed JSON) must
    # fall back to deterministic reasoning + draft, never fail the run. The decision
    # itself was already made above without the model.
    try:
        llm = get_client().text_json(REASON_SYSTEM, user)
        data = llm.json()
        reasoning = data.get("reasoning")
        draft = data.get("amendment_draft") or None
    except Exception:
        reasoning = None
        draft = None
    if not reasoning:
        reasoning = _fallback_reason(outcome, validation)
    if outcome == Outcome.AMENDMENT:
        # Drafting is a required router behaviour — guarantee a draft even if the
        # LLM call failed or returned no draft, by building one deterministically.
        if not draft:
            draft = _fallback_draft(validation)
    else:
        draft = None
    return Decision(outcome=outcome, reasoning=reasoning, amendment_draft=draft), llm


def _fallback_reason(outcome: Outcome, validation: ValidationResult) -> str:
    """Deterministic backstop if the LLM explanation call fails — decision still stands."""
    bad = [r.field for r in validation.results if r.status != FieldStatus.MATCH]
    if outcome == Outcome.AUTO_APPROVE:
        return "All fields matched the rule set with sufficient confidence."
    if outcome == Outcome.HUMAN_REVIEW:
        return f"Human review needed; unresolved fields: {', '.join(bad)}."
    return f"Amendment required for mismatched fields: {', '.join(bad)}."


# ---------------------------------------------------------------------------
# Shipment-level routing (Part 2) — same shape, wider input.
#
# The verdict aggregates every per-document validation PLUS the cross-document
# checks, through the same deterministic tree. The LLM only words the reply to
# SU; the draft is NEVER sent by the agent — it lands in the CG review queue.
# ---------------------------------------------------------------------------
def _decide_shipment(runs: list[RunRecord], cross: CrossValidationResult) -> tuple[Outcome, list[str]]:
    """Deterministic tree over the whole shipment. Returns (outcome, problem notes)."""
    notes: list[str] = []
    uncertain = mismatch = False
    for r in runs:
        if r.status.value == "failed" or r.validation is None:
            uncertain = True
            notes.append(f"{r.filename}: processing failed — could not be verified.")
            continue
        for fv in r.validation.results:
            if fv.status == FieldStatus.UNCERTAIN:
                uncertain = True
                notes.append(f"{r.filename} / {fv.field}: {fv.reason}")
            elif fv.status == FieldStatus.MISMATCH:
                mismatch = True
                notes.append(f"{r.filename} / {fv.field}: found '{fv.found}', expected '{fv.expected}'")
    for c in cross.checks:
        if c.status == CrossStatus.UNCERTAIN:
            uncertain = True
            notes.append(f"cross-document / {c.field}: {c.reason}")
        elif c.status == CrossStatus.INCONSISTENT:
            mismatch = True
            notes.append(f"cross-document / {c.field}: "
                         + "; ".join(f"{v.filename}='{v.value}'" for v in c.values if v.value))
    if uncertain:
        return Outcome.HUMAN_REVIEW, notes
    if mismatch:
        return Outcome.AMENDMENT, notes
    return Outcome.AUTO_APPROVE, notes


SHIPMENT_SYSTEM = """You draft concise, professional replies from a Cargo/Control-Group (CG) validator
to a Shipping Unit (SU) about one shipment's document set. The reply is addressed to the SUPPLIER who
sent the documents (greet them as "Dear Supplier" or by the sender address) — never to the customer;
the customer named in the context is who the supplier ships FOR. Never invent facts beyond what is
given. Reference documents and fields by name. Return ONLY JSON: {"reasoning": "...", "draft": "..."}.
The draft is a complete email body (greeting to sign-off) a CG operator could send after one quick edit."""


def _build_shipment_user(
    outcome: Outcome, runs: list[RunRecord], cross: CrossValidationResult,
    notes: list[str], customer: str, subject: str,
) -> str:
    doc_lines = []
    for r in runs:
        summary = r.validation.summary if r.validation else "processing failed"
        doc_lines.append(f"- {r.filename}: {summary}")
    cross_lines = [f"- {c.field}: {c.status.value} :: {c.reason}" for c in cross.checks]
    base = (
        f"Customer: {customer}\nSU email subject: {subject}\nDecision: {outcome.value}\n"
        f"Documents:\n" + "\n".join(doc_lines)
        + "\nCross-document checks:\n" + ("\n".join(cross_lines) or "- none applicable")
        + "\nProblems found:\n" + ("\n".join(f"- {n}" for n in notes) or "- none")
        + "\n\n"
    )
    if outcome == Outcome.AUTO_APPROVE:
        return base + ("Write one-line reasoning confirming every per-document and cross-document check "
                       "passed. Draft a short approval email to SU confirming the document set is verified "
                       "and cleared to forward to the customer.")
    if outcome == Outcome.HUMAN_REVIEW:
        return base + ("Write reasoning naming exactly what could not be verified and why a CG human must "
                       "look before anything is sent. Draft an email to SU asking them to confirm or supply "
                       "each unverified item, listed field by field.")
    return base + ("Write reasoning naming every discrepancy. Draft an amendment email to SU listing each "
                   "one as 'document / field: found X, expected Y' (for cross-document conflicts, state "
                   "what each document says), asking for corrected documents.")


def route_shipment(
    runs: list[RunRecord], cross: CrossValidationResult, customer: str, subject: str,
) -> tuple[ShipmentDecision, Optional[LLMResult]]:
    outcome, notes = _decide_shipment(runs, cross)  # deterministic — stands even if the LLM is down
    llm: Optional[LLMResult] = None
    reasoning = draft = None
    try:
        llm = get_client().text_json(SHIPMENT_SYSTEM, _build_shipment_user(
            outcome, runs, cross, notes, customer, subject))
        data = llm.json()
        reasoning = data.get("reasoning")
        draft = data.get("draft") or None
    except Exception:
        reasoning = draft = None
    if not reasoning:
        reasoning = _fallback_shipment_reason(outcome, notes)
    if not draft:
        draft = _fallback_shipment_draft(outcome, notes)
    return ShipmentDecision(outcome=outcome, reasoning=reasoning, draft=draft), llm


def _fallback_shipment_reason(outcome: Outcome, notes: list[str]) -> str:
    if outcome == Outcome.AUTO_APPROVE:
        return "All documents passed per-document validation and every cross-document check is consistent."
    if outcome == Outcome.HUMAN_REVIEW:
        return "Human review needed; unresolved items: " + "; ".join(notes)
    return "Amendment required; discrepancies: " + "; ".join(notes)


def _fallback_shipment_draft(outcome: Outcome, notes: list[str]) -> str:
    """Deterministic reply so CG ALWAYS has something to review, even in a full
    provider outage. Every outcome carries a draft — the agent just never sends it."""
    items = "\n".join(f"  - {n}" for n in notes) or "  - (see validation report)"
    if outcome == Outcome.AUTO_APPROVE:
        return ("Subject: Document Set Verified\n\nDear Supplier,\n\n"
                "All submitted documents for this shipment have been verified against the customer's "
                "requirements and are consistent with each other. The set is cleared for forwarding.\n\n"
                "Best regards,\nTrade Operations")
    if outcome == Outcome.HUMAN_REVIEW:
        return ("Subject: Confirmation Needed on Shipment Documents\n\nDear Supplier,\n\n"
                "We could not fully verify the following items and need your confirmation or the "
                "missing details before proceeding:\n\n"
                f"{items}\n\nPlease confirm or provide corrected details.\n\n"
                "Best regards,\nTrade Operations")
    return ("Subject: Request for Trade Document Correction\n\nDear Supplier,\n\n"
            "Our validation of the submitted document set found the following discrepancies that "
            "require correction before we can proceed:\n\n"
            f"{items}\n\nPlease review, amend, and resend the corrected documents.\n\n"
            "Best regards,\nTrade Operations")


def _fallback_draft(validation: ValidationResult) -> str:
    """Deterministic amendment email built from the mismatched fields — used when
    the LLM draft is unavailable, so an amendment outcome ALWAYS carries a draft."""
    mismatches = [r for r in validation.results if r.status == FieldStatus.MISMATCH]
    lines = [f"  - {r.field}: found '{r.found}', expected '{r.expected}'" for r in mismatches]
    body = "\n".join(lines) if lines else "  - (see attached validation report)"
    return (
        "Subject: Request for Trade Document Correction\n\n"
        "Dear Supplier,\n\n"
        "Our validation of the submitted document found the following discrepancies "
        "that require correction before we can proceed:\n\n"
        f"{body}\n\n"
        "Please review, amend, and resend the corrected document at your earliest "
        "convenience.\n\nBest regards,\nTrade Operations"
    )
