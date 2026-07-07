"""
Part 2 trust invariants — shipment-level safety properties.

Same discipline as test_trust_invariants.py: the LLM is never called (FakeClient
throughout). These encode the guarantees the CG workflow depends on:

  * a cross-document contradiction is caught even when every document passes
    its per-document rules individually
  * inconclusive semantic verdicts (missing / stringy / provider down) become
    `uncertain`, never a silent pass
  * the agent NEVER sends: the pipeline's terminal state is pending_review, and
    crash recovery does not touch shipments waiting on a human
  * every outcome carries a draft for CG, even in a full provider outage
"""
import json
import os
import uuid

import pytest

from app import inbox, orchestrator
from app.agents import cross_validator, extractor, router, validator
from app.db import store
from app.llm.client import LLMResult
from app.models import (
    CrossStatus,
    ExtractedDocument,
    ExtractedField as F,
    FieldStatus,
    FieldValidation,
    Outcome,
    RunRecord,
    RunStatus,
    ShipmentStatus,
    ValidationResult,
)

CUSTOMER = "ACME-IMPORTS"


def _res(payload: dict) -> LLMResult:
    return LLMResult(text=json.dumps(payload), model="fake", prompt_tokens=1, output_tokens=1, cost_usd=0.0)


class FakeClient:
    def __init__(self, semantic=None, shipment=None, vision=None, fail=False):
        self.semantic, self.shipment, self.vision, self.fail = semantic, shipment, vision, fail

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
        if "cargo/control-group" in s:
            return _res(self.shipment or {"reasoning": "ok", "draft": "Dear Supplier, ..."})
        return _res({})


def install(monkeypatch, fake):
    for mod in (cross_validator, validator, router, extractor):
        monkeypatch.setattr(mod, "get_client", lambda fake=fake: fake)


def doc(hs="8471.30", weight="12,500 kg", consignee="Acme Imports Ltd.", **overrides) -> ExtractedDocument:
    base = dict(
        doc_type=F(value="Commercial Invoice", confidence=0.95, source_quote="COMMERCIAL INVOICE"),
        consignee_name=F(value=consignee, confidence=0.95, source_quote=consignee),
        hs_code=F(value=hs, confidence=0.95, source_quote=f"HS Code {hs}"),
        port_of_loading=F(value="Shanghai", confidence=0.95, source_quote="Shanghai"),
        port_of_discharge=F(value="Rotterdam", confidence=0.95, source_quote="Rotterdam"),
        incoterms=F(value="FOB", confidence=0.95, source_quote="FOB"),
        description_of_goods=F(value="laptop computers", confidence=0.95, source_quote="laptop computers"),
        gross_weight=F(value=weight, confidence=0.95, source_quote=weight),
        invoice_number=F(value="INV-2024-0101", confidence=0.95, source_quote="INV-2024-0101"),
    )
    base.update(overrides)
    return ExtractedDocument(**base)


def rec(filename: str, run_id=None) -> RunRecord:
    return RunRecord(run_id=run_id or str(uuid.uuid4()), customer=CUSTOMER,
                     filename=filename, status=RunStatus.STORED)


def pair(*docs_):
    return [(rec(f"doc{i}.pdf"), d) for i, d in enumerate(docs_)]


def check_for(cross, field):
    return next((c for c in cross.checks if c.field == field), None)


# --------------------------------------------------------------------------- #
# Cross-validation
# --------------------------------------------------------------------------- #
def test_hs_code_conflict_is_caught_even_when_both_pass_per_doc_rules(monkeypatch):
    """The headline case: 8471.30 and 8523.51 are BOTH on ACME's allowlist, so
    per-document validation approves each doc. Only the cross-check objects."""
    install(monkeypatch, FakeClient())
    for hs in ("8471.30", "8523.51"):
        v, _ = validator.validate(doc(hs=hs), CUSTOMER)
        assert not v.has_mismatch and not v.has_uncertain  # per-doc: clean

    cross, _ = cross_validator.cross_validate(pair(doc(hs="8471.30"), doc(hs="8523.51")))
    c = check_for(cross, "hs_code")
    assert c.status == CrossStatus.INCONSISTENT and c.method == "deterministic"
    assert cross.has_inconsistent


def test_weight_within_band_consistent_but_unit_mismatch_never_passes(monkeypatch):
    install(monkeypatch, FakeClient())
    ok, _ = cross_validator.cross_validate(pair(doc(weight="12,500 kg"), doc(weight="12,480 kg")))
    assert check_for(ok, "gross_weight").status == CrossStatus.CONSISTENT

    bad, _ = cross_validator.cross_validate(pair(doc(weight="12,500 kg"), doc(weight="12,500 lb")))
    assert check_for(bad, "gross_weight").status == CrossStatus.INCONSISTENT


def test_semantic_variant_consistent_when_llm_confirms(monkeypatch):
    install(monkeypatch, FakeClient(semantic={"results": [
        {"field": "consignee_name", "consistent": True, "reason": "same company"}]}))
    cross, _ = cross_validator.cross_validate(
        pair(doc(), doc(consignee="ACME IMPORTS LIMITED")))
    c = check_for(cross, "consignee_name")
    assert c.status == CrossStatus.CONSISTENT and c.method == "semantic"


@pytest.mark.parametrize("fake", [
    FakeClient(fail=True),                                                  # provider outage
    FakeClient(semantic={"results": []}),                                   # missing verdict
    FakeClient(semantic={"results": [{"field": "consignee_name", "consistent": "true"}]}),  # stringy bool
])
def test_inconclusive_semantic_cross_check_degrades_to_uncertain(monkeypatch, fake):
    install(monkeypatch, fake)
    cross, _ = cross_validator.cross_validate(
        pair(doc(), doc(consignee="ACME IMPORTS LIMITED")))
    c = check_for(cross, "consignee_name")
    assert c.status == CrossStatus.UNCERTAIN
    assert cross.has_uncertain


def test_incoterm_named_place_variants_agree_but_different_codes_conflict(monkeypatch):
    """Regression (live-tested): 'FOB' vs 'FOB Shanghai' is the SAME term with the
    Incoterms-2020 named place — must not flag a clean shipment. 'FOB' vs 'CIF'
    is a real conflict."""
    install(monkeypatch, FakeClient())
    same, _ = cross_validator.cross_validate(pair(
        doc(), doc(incoterms=F(value="FOB Shanghai", confidence=0.95, source_quote="FOB Shanghai"))))
    assert check_for(same, "incoterms").status == CrossStatus.CONSISTENT

    diff, _ = cross_validator.cross_validate(pair(
        doc(), doc(incoterms=F(value="CIF", confidence=0.95, source_quote="CIF"))))
    assert check_for(diff, "incoterms").status == CrossStatus.INCONSISTENT


def test_field_in_single_doc_is_not_cross_checked(monkeypatch):
    install(monkeypatch, FakeClient())
    only_one_has_invoice = pair(doc(), doc(invoice_number=F(value=None)))
    cross, _ = cross_validator.cross_validate(only_one_has_invoice)
    assert check_for(cross, "invoice_number") is None


# --------------------------------------------------------------------------- #
# Shipment routing + drafts
# --------------------------------------------------------------------------- #
def _clean_validation():
    return ValidationResult(results=[FieldValidation(
        field="hs_code", status=FieldStatus.MATCH, found="8471.30",
        expected="8471.30", confidence=0.95, reason="ok")],
        has_mismatch=False, has_uncertain=False)


def _stored_run(validation=None):
    r = rec("doc.pdf")
    r.validation = validation or _clean_validation()
    return r


def test_shipment_decision_tree(monkeypatch):
    install(monkeypatch, FakeClient())
    clean_cross, _ = cross_validator.cross_validate(pair(doc(), doc()))
    bad_cross, _ = cross_validator.cross_validate(pair(doc(hs="8471.30"), doc(hs="8523.51")))

    d, _ = router.route_shipment([_stored_run()], clean_cross, CUSTOMER, "s")
    assert d.outcome == Outcome.AUTO_APPROVE

    d, _ = router.route_shipment([_stored_run()], bad_cross, CUSTOMER, "s")
    assert d.outcome == Outcome.AMENDMENT

    failed = rec("broken.pdf")
    failed.status = RunStatus.FAILED
    d, _ = router.route_shipment([_stored_run(), failed], clean_cross, CUSTOMER, "s")
    assert d.outcome == Outcome.HUMAN_REVIEW  # an unverifiable doc is never approved


def test_every_outcome_has_a_draft_even_with_provider_down(monkeypatch):
    install(monkeypatch, FakeClient(fail=True))
    clean_cross, _ = cross_validator.cross_validate(pair(doc(), doc()))
    d, _ = router.route_shipment([_stored_run()], clean_cross, CUSTOMER, "s")
    assert d.outcome == Outcome.AUTO_APPROVE and d.draft and d.reasoning

    # Outage during an amendment: the deterministic draft still names every
    # discrepancy, so CG always has a sendable reply.
    install(monkeypatch, FakeClient())
    bad_cross, _ = cross_validator.cross_validate(pair(doc(hs="8471.30"), doc(hs="8523.51")))
    install(monkeypatch, FakeClient(fail=True))
    d, _ = router.route_shipment([_stored_run()], bad_cross, CUSTOMER, "s")
    assert d.outcome == Outcome.AMENDMENT and "hs_code" in d.draft


# --------------------------------------------------------------------------- #
# End-to-end: trigger -> pipeline -> pending_review (never sent)
# --------------------------------------------------------------------------- #
def _png() -> bytes:
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _good_vision():
    return {name: {"value": v, "confidence": 0.95, "source_quote": v} for name, v in {
        "doc_type": "Commercial Invoice", "consignee_name": "Acme Imports Ltd.",
        "hs_code": "8471.30", "port_of_loading": "Shanghai", "port_of_discharge": "Rotterdam",
        "incoterms": "FOB", "description_of_goods": "laptop computers",
        "gross_weight": "12,500 kg", "invoice_number": "INV-2024-0101"}.items()}


def test_shipment_pipeline_ends_pending_review_and_never_sends(monkeypatch, fresh_db, tmp_path):
    install(monkeypatch, FakeClient(vision=_good_vision()))
    monkeypatch.setattr(orchestrator, "_pages_for", lambda rec: [_png()])

    sid = str(uuid.uuid4())
    store.create_shipment(sid, CUSTOMER, "su@supplier", "test shipment")
    for name in ("invoice.png", "packing.png"):
        store.create_run(str(uuid.uuid4()), CUSTOMER, name, source_path="unused", shipment_id=sid)

    ship = orchestrator.run_shipment(sid)
    assert ship.status == ShipmentStatus.PENDING_REVIEW  # terminal for the agent
    assert ship.sent_at is None and ship.draft_final is None
    assert ship.decision.outcome == Outcome.AUTO_APPROVE and ship.decision.draft
    assert ship.cross_validation and not ship.cross_validation.has_inconsistent

    # Crash recovery must NOT touch a shipment waiting on the human.
    assert store.incomplete_shipments() == []

    # Only the explicit CG action moves it to SENT, preserving the audit trail.
    store.update_shipment(sid, ShipmentStatus.SENT, draft_final="edited by CG")
    sent = store.get_shipment(sid)
    assert sent.status == ShipmentStatus.SENT and sent.sent_at and sent.draft_final == "edited by CG"


def test_inbox_fails_loud_on_unknown_customer_and_empty_email(monkeypatch, fresh_db, tmp_path):
    from app.settings import settings
    monkeypatch.setattr(settings, "inbox_dir", str(tmp_path / "inbox"))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))

    bad = tmp_path / "inbox" / "bad-customer"
    bad.mkdir(parents=True)
    (bad / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    (bad / "email.json").write_text(json.dumps({"customer": "NOBODY-CORP", "from": "x", "subject": "s"}))

    empty = tmp_path / "inbox" / "no-attachments"
    empty.mkdir(parents=True)
    (empty / "email.json").write_text(json.dumps({"customer": CUSTOMER, "from": "x", "subject": "s"}))

    ids = inbox.scan_once()
    assert len(ids) == 2
    for sid in ids:
        s = store.get_shipment(sid)
        assert s.status == ShipmentStatus.FAILED and s.error  # visible in the queue, not silently dropped
    # Neither shipment created any runs or started a pipeline.
    for sid in ids:
        assert store.runs_for_shipment(sid) == []
    # Folders were moved out of the live inbox either way.
    assert os.listdir(tmp_path / "inbox") == [inbox.PROCESSED_SUBDIR]
