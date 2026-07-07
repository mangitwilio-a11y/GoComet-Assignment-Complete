"""
Orchestrator — the thin controller (NOT an agent).

It owns everything the agents must not: retries, the per-document budget cap,
checkpointing, and crash recovery. Agents reason; the orchestrator enforces
limits. This boundary is deliberate — it's where "stop agent loops, runaway
costs, infinite retries" lives.

Crash recovery: each step writes its partial output + new status to the runs
table BEFORE the next step starts. `resume()` reads the last checkpoint and skips
any step whose output is already persisted, so a restart never re-pays for the
expensive vision call it already completed.
"""
from __future__ import annotations

import time
import traceback
from typing import Callable, Optional

from app.agents import cross_validator, extractor, router, validator
from app.db import store
from app.llm.client import LLMResult
from app.models import RunRecord, RunStatus, ShipmentRecord, ShipmentStatus
from app.settings import settings


class BudgetExceeded(Exception):
    pass


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _check_budget(run_id: str) -> None:
    spent = store.run_cost(run_id)
    if spent > settings.run_budget_usd:
        raise BudgetExceeded(f"Run cost ${spent:.4f} exceeded cap ${settings.run_budget_usd:.2f}")


def _step(run_id: str, name: str, fn: Callable[[], tuple], detail: str = "") -> object:
    """Run one agent step with retry + ledger accounting + budget enforcement.

    `fn` returns (result, llm_result|None). The llm_result feeds the cost ledger.
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, settings.max_retries_per_step + 2):  # 1 try + N retries
        _check_budget(run_id)
        start = time.perf_counter()
        try:
            result, llm = fn()
            llm_meta: Optional[LLMResult] = llm
            store.append_ledger(
                run_id, name,
                model=llm_meta.model if llm_meta else "",
                prompt_tokens=llm_meta.prompt_tokens if llm_meta else 0,
                output_tokens=llm_meta.output_tokens if llm_meta else 0,
                cost_usd=llm_meta.cost_usd if llm_meta else 0.0,
                duration_ms=_ms(start),
                detail=f"{detail} (attempt {attempt})".strip(),
            )
            _check_budget(run_id)
            return result
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001 — we want to retry any agent failure
            last_err = e
            store.append_ledger(run_id, name, duration_ms=_ms(start),
                                detail=f"ERROR attempt {attempt}: {e}")
    raise RuntimeError(f"Step '{name}' failed after retries: {last_err}")


def run_pipeline(
    run_id: str,
    customer: str,
    page_images: Optional[list[bytes]],
    existing: Optional[RunRecord] = None,
) -> RunRecord:
    """Execute (or resume) the full pipeline. Returns the final RunRecord.

    `existing` non-None => resume mode: completed steps are skipped.
    """
    try:
        # --- Extract ---
        extracted = existing.extracted if existing else None
        if extracted is None:
            if not page_images:
                raise RuntimeError("No page images available to (re)run extraction.")
            store.update_run(run_id, RunStatus.EXTRACTING)
            extracted = _step(run_id, "extract", lambda: extractor.extract(page_images),
                              detail=f"{len(page_images)} page(s)")
            store.update_run(run_id, RunStatus.EXTRACTING, extracted=extracted)

        # --- Validate ---
        validation = existing.validation if existing else None
        if validation is None:
            store.update_run(run_id, RunStatus.VALIDATING)
            validation = _step(run_id, "validate", lambda: validator.validate(extracted, customer))
            store.update_run(run_id, RunStatus.VALIDATING, validation=validation)

        # --- Route ---
        decision = existing.decision if existing else None
        if decision is None:
            store.update_run(run_id, RunStatus.ROUTING)
            decision = _step(run_id, "route", lambda: router.route(validation, customer))
            store.update_run(run_id, RunStatus.ROUTING, decision=decision)

        # --- Store (terminal) ---
        store.update_run(run_id, RunStatus.STORED)
        return store.get_run(run_id)

    except Exception as e:  # noqa: BLE001
        store.update_run(run_id, RunStatus.FAILED, error=f"{e}\n{traceback.format_exc()}")
        return store.get_run(run_id)


# ---------------------------------------------------------------------------
# Shipment pipeline (Part 2) — one SU email, N documents.
#
# Reuses run_pipeline per attachment, then adds two shipment-level steps
# (cross-validate, decide+draft), each checkpointed before the next starts so a
# crash resumes without re-paying completed work. Terminal agent state is
# PENDING_REVIEW: only the CG human advances a shipment to SENT.
# ---------------------------------------------------------------------------
def _check_shipment_budget(shipment_id: str) -> None:
    spent = store.shipment_cost(shipment_id)
    if spent > settings.shipment_budget_usd:
        raise BudgetExceeded(
            f"Shipment cost ${spent:.4f} exceeded cap ${settings.shipment_budget_usd:.2f}")


def _shipment_step(shipment_id: str, name: str, fn: Callable[[], tuple], detail: str = "") -> object:
    """Shipment-level analogue of _step: retries + shipment ledger + budget."""
    last_err: Optional[Exception] = None
    for attempt in range(1, settings.max_retries_per_step + 2):
        _check_shipment_budget(shipment_id)
        start = time.perf_counter()
        try:
            result, llm = fn()
            llm_meta: Optional[LLMResult] = llm
            store.append_shipment_ledger(
                shipment_id, name,
                model=llm_meta.model if llm_meta else "",
                prompt_tokens=llm_meta.prompt_tokens if llm_meta else 0,
                output_tokens=llm_meta.output_tokens if llm_meta else 0,
                cost_usd=llm_meta.cost_usd if llm_meta else 0.0,
                duration_ms=_ms(start),
                detail=f"{detail} (attempt {attempt})".strip(),
            )
            return result
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            store.append_shipment_ledger(shipment_id, name, duration_ms=_ms(start),
                                         detail=f"ERROR attempt {attempt}: {e}")
    raise RuntimeError(f"Shipment step '{name}' failed after retries: {last_err}")


def _pages_for(rec: RunRecord) -> Optional[list[bytes]]:
    import os

    from app import ingest

    src = store.get_source_path(rec.run_id)
    if src and os.path.exists(src):
        with open(src, "rb") as f:
            return ingest.to_page_images(f.read(), rec.filename)
    return None


def run_shipment(shipment_id: str) -> ShipmentRecord:
    """Execute (or resume) a shipment end-to-end: per-doc runs -> cross-validate
    -> decide & draft -> PENDING_REVIEW. Never sends anything."""
    ship = store.get_shipment(shipment_id)
    if ship is None:
        raise ValueError(f"Unknown shipment {shipment_id}")
    try:
        # --- Per-document runs (reuse of the Part 1 pipeline, unchanged) ---
        store.update_shipment(shipment_id, ShipmentStatus.PROCESSING)
        for rec in store.runs_for_shipment(shipment_id):
            if rec.status in (RunStatus.STORED, RunStatus.FAILED):
                continue  # already checkpointed — a resume never re-pays this doc
            _check_shipment_budget(shipment_id)
            pages = None
            if rec.extracted is None:
                pages = _pages_for(rec)
                if pages is None:
                    store.update_run(rec.run_id, RunStatus.FAILED,
                                     error="Attachment not retained; cannot (re)extract.")
                    continue
            run_pipeline(rec.run_id, rec.customer, page_images=pages, existing=rec)

        runs = store.runs_for_shipment(shipment_id)
        docs = [(r, r.extracted) for r in runs if r.extracted is not None]
        if not docs:
            raise RuntimeError("No attachment could be processed — nothing to validate.")

        # --- Cross-document consistency (checkpointed) ---
        cross = ship.cross_validation
        if cross is None:
            store.update_shipment(shipment_id, ShipmentStatus.CROSS_VALIDATING)
            cross = _shipment_step(shipment_id, "cross_validate",
                                   lambda: cross_validator.cross_validate(docs),
                                   detail=f"{len(docs)} doc(s)")
            store.update_shipment(shipment_id, ShipmentStatus.CROSS_VALIDATING,
                                  cross_validation=cross)

        # --- Decide & draft (checkpointed) ---
        decision = ship.decision
        if decision is None:
            store.update_shipment(shipment_id, ShipmentStatus.DRAFTING)
            decision = _shipment_step(
                shipment_id, "draft",
                lambda: router.route_shipment(runs, cross, ship.customer, ship.subject))
            store.update_shipment(shipment_id, ShipmentStatus.DRAFTING, decision=decision)

        # --- Hand off to the human (terminal for the agent) ---
        store.update_shipment(shipment_id, ShipmentStatus.PENDING_REVIEW)
        return store.get_shipment(shipment_id)

    except Exception as e:  # noqa: BLE001
        store.update_shipment(shipment_id, ShipmentStatus.FAILED,
                              error=f"{e}\n{traceback.format_exc()}")
        return store.get_shipment(shipment_id)


def resume_incomplete() -> list[str]:
    """Resume any runs/shipments left mid-pipeline by a crash. Returns resumed ids.

    Crash *after* extraction resumes for free from the persisted JSON. Crash
    *before* extraction completed re-ingests from the retained upload
    (source_path) and re-runs extraction — so the document is not lost. Only if
    the upload was never retained do we fail and ask for a re-upload.

    Runs belonging to a shipment are resumed through their shipment, so the
    shipment-level steps (cross-validate, draft) also complete.
    """
    import os

    from app import ingest

    resumed = []
    for rec in store.incomplete_runs():
        if rec.shipment_id:
            continue  # resumed below via its shipment
        pages = None
        if rec.extracted is None:
            src = store.get_source_path(rec.run_id)
            if src and os.path.exists(src):
                with open(src, "rb") as f:
                    pages = ingest.to_page_images(f.read(), rec.filename)
            else:
                store.update_run(rec.run_id, RunStatus.FAILED,
                                 error="Crashed before extraction and upload not retained; re-upload required.")
                continue
        run_pipeline(rec.run_id, rec.customer, page_images=pages, existing=rec)
        resumed.append(rec.run_id)

    for ship in store.incomplete_shipments():
        run_shipment(ship.shipment_id)
        resumed.append(ship.shipment_id)
    return resumed
