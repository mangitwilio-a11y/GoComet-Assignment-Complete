"""
Core data contracts for the Nova trade-document pipeline.

These Pydantic models are the *spine* of the system: every agent hands off
one of these objects, and the same shapes are persisted to SQLite and rendered
in the UI. The `{value, confidence, source_quote}` triple on every extracted
field is the anti-hallucination contract — a field is only trustworthy if the
model can point at the text it came from.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
class ExtractedField(BaseModel):
    """A single field pulled from a document.

    `source_quote` is the verbatim text the value was read from. If the model
    cannot produce a quote, the value is treated as ungrounded (a likely
    hallucination) regardless of the confidence it reports.
    """

    value: Optional[str] = Field(
        default=None, description="The extracted value, or null if not present in the document."
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Model-reported confidence 0-1. Used as a routing signal, not ground truth.",
    )
    source_quote: Optional[str] = Field(
        default=None,
        description="Verbatim snippet from the document that supports the value. Null = ungrounded.",
    )


# The eight required fields from the assignment, plus document type.
class ExtractedDocument(BaseModel):
    doc_type: ExtractedField
    consignee_name: ExtractedField
    hs_code: ExtractedField
    port_of_loading: ExtractedField
    port_of_discharge: ExtractedField
    incoterms: ExtractedField
    description_of_goods: ExtractedField
    gross_weight: ExtractedField
    invoice_number: ExtractedField

    def as_field_map(self) -> dict[str, ExtractedField]:
        return {k: v for k, v in self.model_dump().items()}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class FieldStatus(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNCERTAIN = "uncertain"


class FieldValidation(BaseModel):
    field: str
    status: FieldStatus
    found: Optional[str] = None
    expected: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = Field(description="Why this status — deterministic rule fired, or semantic judgement.")
    method: str = Field(
        default="deterministic",
        description="'deterministic' (code rule) or 'semantic' (LLM fuzzy match).",
    )


class ValidationResult(BaseModel):
    results: List[FieldValidation]
    has_mismatch: bool
    has_uncertain: bool

    @property
    def summary(self) -> str:
        return (
            f"{sum(r.status == FieldStatus.MATCH for r in self.results)} match / "
            f"{sum(r.status == FieldStatus.MISMATCH for r in self.results)} mismatch / "
            f"{sum(r.status == FieldStatus.UNCERTAIN for r in self.results)} uncertain"
        )


# ---------------------------------------------------------------------------
# Routing / Decision
# ---------------------------------------------------------------------------
class Outcome(str, Enum):
    AUTO_APPROVE = "auto_approve"
    HUMAN_REVIEW = "human_review"
    AMENDMENT = "amendment"


class Decision(BaseModel):
    outcome: Outcome
    reasoning: str = Field(description="Human-readable explanation of *why* this outcome was chosen.")
    amendment_draft: Optional[str] = Field(
        default=None, description="Drafted amendment-request email when outcome == amendment."
    )


# ---------------------------------------------------------------------------
# Cross-document validation (Part 2)
#
# A shipment carries several documents (BOL + Invoice + Packing List) that all
# describe the SAME physical cargo. Fields like consignee and HS code must agree
# across them — a per-document check can pass on every doc individually while
# the shipment is still wrong (invoice says 8471.30, packing list says 8517.62).
# ---------------------------------------------------------------------------
class CrossStatus(str, Enum):
    CONSISTENT = "consistent"
    INCONSISTENT = "inconsistent"
    UNCERTAIN = "uncertain"


class CrossDocValue(BaseModel):
    """One document's reading of a cross-checked field, with its evidence."""

    run_id: str
    filename: str
    doc_type: Optional[str] = None
    value: Optional[str] = None
    source_quote: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class CrossFieldCheck(BaseModel):
    field: str
    status: CrossStatus
    values: List[CrossDocValue]
    reason: str
    method: str = Field(default="deterministic", description="'deterministic' or 'semantic'.")


class CrossValidationResult(BaseModel):
    checks: List[CrossFieldCheck]
    has_inconsistent: bool
    has_uncertain: bool

    @property
    def summary(self) -> str:
        return (
            f"{sum(c.status == CrossStatus.CONSISTENT for c in self.checks)} consistent / "
            f"{sum(c.status == CrossStatus.INCONSISTENT for c in self.checks)} inconsistent / "
            f"{sum(c.status == CrossStatus.UNCERTAIN for c in self.checks)} uncertain"
        )


# ---------------------------------------------------------------------------
# Run / pipeline state (persisted to the `runs` table)
# ---------------------------------------------------------------------------
class RunStatus(str, Enum):
    QUEUED = "queued"
    EXTRACTING = "extracting"
    VALIDATING = "validating"
    ROUTING = "routing"
    STORED = "stored"
    FAILED = "failed"


class RunRecord(BaseModel):
    run_id: str
    customer: str
    filename: str
    status: RunStatus
    shipment_id: Optional[str] = None
    extracted: Optional[ExtractedDocument] = None
    validation: Optional[ValidationResult] = None
    decision: Optional[Decision] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Shipment — one SU email with N attached documents (persisted to `shipments`)
# ---------------------------------------------------------------------------
class ShipmentStatus(str, Enum):
    RECEIVED = "received"
    PROCESSING = "processing"          # per-document runs executing
    CROSS_VALIDATING = "cross_validating"
    DRAFTING = "drafting"
    PENDING_REVIEW = "pending_review"  # draft ready — waiting for the CG human
    SENT = "sent"                      # CG reviewed, edited, and clicked send
    FAILED = "failed"


class ShipmentDecision(BaseModel):
    """Shipment-level verdict + the reply the CG operator will review.

    Unlike the per-doc Decision, `draft` exists for EVERY outcome: an approval
    note, an amendment request, or (for human review) a confirmation request to
    SU listing what could not be verified. The agent never sends it — CG does.
    """

    outcome: Outcome
    reasoning: str
    draft: str


class ShipmentRecord(BaseModel):
    shipment_id: str
    customer: str
    from_addr: str
    subject: str
    status: ShipmentStatus
    cross_validation: Optional[CrossValidationResult] = None
    decision: Optional[ShipmentDecision] = None
    draft_final: Optional[str] = Field(
        default=None, description="The CG-edited text that was actually sent (audit trail)."
    )
    error: Optional[str] = None
    received_at: Optional[str] = None
    updated_at: Optional[str] = None
    sent_at: Optional[str] = None
