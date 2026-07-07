"""
Trust-invariant tests — the properties that must hold for the system to be safe.

The LLM is never called: every test installs a FakeClient. These are the offline
eval / regression suite referenced in the PRD; they encode the "never silently
approve" guarantees and the graceful-degradation behaviour.

Run:  pytest -q
"""
import json
import uuid

import pytest

from app.agents import extractor, router, validator
from app import orchestrator
from app.db import store
from app.llm.client import LLMResult
from app.models import (
    Decision,
    ExtractedDocument,
    ExtractedField as F,
    FieldStatus,
    FieldValidation,
    Outcome,
    ValidationResult,
)

CUSTOMER = "ACME-IMPORTS"


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #
def _res(payload: dict) -> LLMResult:
    return LLMResult(text=json.dumps(payload), model="fake", prompt_tokens=1, output_tokens=1, cost_usd=0.0)


class FakeClient:
    """Stand-in LLM. `fail=True` simulates a provider/network outage."""

    def __init__(self, semantic=None, reasoning=None, vision=None, fail=False):
        self.semantic, self.reasoning, self.vision, self.fail = semantic, reasoning, vision, fail

    def vision_json(self, *a, **k) -> LLMResult:
        if self.fail:
            raise RuntimeError("provider down")
        return _res(self.vision or {})

    def text_json(self, system, user, temperature=0.0) -> LLMResult:
        if self.fail:
            raise RuntimeError("provider down")
        s = system.lower()
        if "validation assistant" in s:
            return _res(self.semantic or {"results": []})
        if "operations notes" in s:
            return _res(self.reasoning or {"reasoning": "ok", "amendment_draft": "draft"})
        return _res({})


def install(monkeypatch, fake):
    monkeypatch.setattr(validator, "get_client", lambda: fake)
    monkeypatch.setattr(router, "get_client", lambda: fake)
    monkeypatch.setattr(extractor, "get_client", lambda: fake)


def good_doc(**overrides) -> ExtractedDocument:
    """A document where every field matches ACME rules deterministically (no LLM)."""
    base = dict(
        doc_type=F(value="Commercial Invoice", confidence=0.95, source_quote="COMMERCIAL INVOICE"),
        consignee_name=F(value="Acme Imports Ltd.", confidence=0.95, source_quote="Acme Imports Ltd."),
        hs_code=F(value="8471.30", confidence=0.95, source_quote="HS Code 8471.30"),
        port_of_loading=F(value="Shanghai", confidence=0.95, source_quote="Shanghai"),
        port_of_discharge=F(value="Rotterdam", confidence=0.95, source_quote="Rotterdam"),
        incoterms=F(value="FOB", confidence=0.95, source_quote="FOB"),
        description_of_goods=F(value="laptop computers", confidence=0.95, source_quote="laptop computers"),
        gross_weight=F(value="12,500 kg", confidence=0.95, source_quote="12,500 kg"),
        invoice_number=F(value="INV-2024-0001", confidence=0.95, source_quote="INV-2024-0001"),
    )
    base.update(overrides)
    return ExtractedDocument(**base)


def _field(results, name) -> FieldValidation:
    return next(r for r in results if r.field == name)


# --------------------------------------------------------------------------- #
# Invariant 1: wrong unit never auto-approves
# --------------------------------------------------------------------------- #
def test_wrong_weight_unit_is_mismatch_not_approve():
    doc = good_doc(gross_weight=F(value="12,500 lb", confidence=0.95, source_quote="12,500 lb"))
    res, _ = validator.validate(doc, CUSTOMER)
    assert _field(res.results, "gross_weight").status == FieldStatus.MISMATCH
    assert router._decide(res) != Outcome.AUTO_APPROVE


def test_missing_unit_is_uncertain():
    doc = good_doc(gross_weight=F(value="12500", confidence=0.95, source_quote="12500"))
    res, _ = validator.validate(doc, CUSTOMER)
    assert _field(res.results, "gross_weight").status == FieldStatus.UNCERTAIN


# --------------------------------------------------------------------------- #
# Invariant 1b: Incoterm with a named place ("FOB Shanghai") still matches the
# allowlist — Incoterms 2020 require a named place, so the code is a token, not
# the whole string. A disallowed term ("DDP") must still mismatch.
# --------------------------------------------------------------------------- #
def test_incoterm_with_named_place_matches_allowlist():
    doc = good_doc(incoterms=F(value="FOB Shanghai", confidence=0.95, source_quote="Incoterms: FOB Shanghai"))
    res, _ = validator.validate(doc, CUSTOMER)
    assert _field(res.results, "incoterms").status == FieldStatus.MATCH
    assert router._decide(res) == Outcome.AUTO_APPROVE


def test_disallowed_incoterm_is_mismatch():
    doc = good_doc(incoterms=F(value="DDP", confidence=0.95, source_quote="Incoterms: DDP"))
    res, _ = validator.validate(doc, CUSTOMER)
    assert _field(res.results, "incoterms").status == FieldStatus.MISMATCH
    assert router._decide(res) == Outcome.AMENDMENT


# --------------------------------------------------------------------------- #
# Invariant 2: missing values surface as uncertain -> human review
# --------------------------------------------------------------------------- #
def test_missing_value_is_uncertain_and_routes_to_review():
    doc = good_doc(invoice_number=F(value=None, confidence=0.0, source_quote=None))
    res, _ = validator.validate(doc, CUSTOMER)
    assert _field(res.results, "invoice_number").status == FieldStatus.UNCERTAIN
    assert router._decide(res) == Outcome.HUMAN_REVIEW


def test_ungrounded_value_is_uncertain():
    doc = good_doc(hs_code=F(value="8471.30", confidence=0.95, source_quote=None))
    res, _ = validator.validate(doc, CUSTOMER)
    assert _field(res.results, "hs_code").status == FieldStatus.UNCERTAIN


# --------------------------------------------------------------------------- #
# Invariant 3: malformed semantic output becomes uncertain (not a match/mismatch)
# --------------------------------------------------------------------------- #
def _semantic_doc():
    # "Acme Imports Limited" != exact "Acme Imports Ltd." -> defers to the semantic LLM.
    return good_doc(consignee_name=F(value="Acme Imports Limited", confidence=0.95,
                                     source_quote="Acme Imports Limited"))


def test_semantic_string_false_does_not_become_match(monkeypatch):
    # bool("false") is True — the classic bug. A stringy verdict must be uncertain.
    install(monkeypatch, FakeClient(semantic={"results": [{"field": "consignee_name", "match": "false"}]}))
    res, _ = validator.validate(_semantic_doc(), CUSTOMER)
    assert _field(res.results, "consignee_name").status == FieldStatus.UNCERTAIN


def test_semantic_missing_verdict_is_uncertain(monkeypatch):
    install(monkeypatch, FakeClient(semantic={"results": []}))
    res, _ = validator.validate(_semantic_doc(), CUSTOMER)
    assert _field(res.results, "consignee_name").status == FieldStatus.UNCERTAIN


def test_semantic_real_bools_resolve(monkeypatch):
    install(monkeypatch, FakeClient(semantic={"results": [{"field": "consignee_name", "match": True}]}))
    res, _ = validator.validate(_semantic_doc(), CUSTOMER)
    assert _field(res.results, "consignee_name").status == FieldStatus.MATCH

    install(monkeypatch, FakeClient(semantic={"results": [{"field": "consignee_name", "match": False}]}))
    res, _ = validator.validate(_semantic_doc(), CUSTOMER)
    assert _field(res.results, "consignee_name").status == FieldStatus.MISMATCH


# --------------------------------------------------------------------------- #
# Invariant 4: provider failure does not destroy deterministic behaviour
# --------------------------------------------------------------------------- #
def test_semantic_provider_failure_degrades_to_uncertain(monkeypatch):
    install(monkeypatch, FakeClient(fail=True))
    res, _ = validator.validate(_semantic_doc(), CUSTOMER)  # must NOT raise
    assert _field(res.results, "consignee_name").status == FieldStatus.UNCERTAIN


def test_router_provider_failure_still_decides_and_drafts(monkeypatch):
    install(monkeypatch, FakeClient(fail=True))
    vr = ValidationResult(
        results=[FieldValidation(field="incoterms", status=FieldStatus.MISMATCH,
                                 found="DDP", expected="FOB/CIF", reason="not allowed")],
        has_mismatch=True, has_uncertain=False,
    )
    decision, _ = router.route(vr, CUSTOMER)  # must NOT raise
    assert decision.outcome == Outcome.AMENDMENT       # deterministic decision survives
    assert decision.reasoning                          # fallback reasoning present
    assert decision.amendment_draft                    # fallback draft present


# --------------------------------------------------------------------------- #
# Invariant 5: all three outcomes are reachable end-to-end through the orchestrator
# --------------------------------------------------------------------------- #
def _vision(**overrides) -> dict:
    base = good_doc().model_dump()
    base.update(overrides)
    return base


def _run_e2e(monkeypatch, vision_payload) -> Decision:
    install(monkeypatch, FakeClient(vision=vision_payload, reasoning={"reasoning": "r", "amendment_draft": "d"}))
    run_id = str(uuid.uuid4())
    store.create_run(run_id, CUSTOMER, "x.pdf")
    rec = orchestrator.run_pipeline(run_id, CUSTOMER, [b"fake-image-bytes"])
    return rec.decision


def test_e2e_auto_approve(monkeypatch, fresh_db):
    assert _run_e2e(monkeypatch, _vision()).outcome == Outcome.AUTO_APPROVE


def test_e2e_amendment(monkeypatch, fresh_db):
    payload = _vision(incoterms={"value": "DDP", "confidence": 0.95, "source_quote": "DDP"})
    assert _run_e2e(monkeypatch, payload).outcome == Outcome.AMENDMENT


def test_e2e_human_review(monkeypatch, fresh_db):
    payload = _vision(invoice_number={"value": None, "confidence": 0.0, "source_quote": None})
    assert _run_e2e(monkeypatch, payload).outcome == Outcome.HUMAN_REVIEW


# --------------------------------------------------------------------------- #
# Invariant 6: unknown customer is rejected (never silently uses another's rules)
# --------------------------------------------------------------------------- #
def test_unknown_customer_rejected():
    with pytest.raises(FileNotFoundError):
        validator.load_rules("NOT-A-REAL-CUSTOMER")
    assert CUSTOMER in validator.available_customers()
