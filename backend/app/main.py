"""FastAPI surface for the Nova pipeline."""
from __future__ import annotations

import os
import shutil
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import inbox, orchestrator
from app.agents.validator import available_customers
from app.db import store
from app.ingest import to_page_images
from app.models import ShipmentStatus
from app.query import nl2sql
from app.settings import settings

app = FastAPI(title="Nova Trade-Doc Pipeline")

app.add_middleware(
    CORSMiddleware,
    # Any localhost port — Next.js falls back to 3001/3002/... if 3000 is taken.
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = settings.upload_dir
SHIPMENT_SAMPLES_DIR = Path(__file__).resolve().parent.parent.parent / "samples" / "shipments"


def _safe_resume() -> None:
    try:
        orchestrator.resume_incomplete()
    except Exception:  # noqa: BLE001 — recovery is best-effort; never crash startup
        pass


@app.on_event("startup")
def _startup() -> None:
    store.init_db()
    # Automatic crash recovery: resume any runs/shipments left mid-pipeline by a
    # previous crash. Done in a background thread so startup is not blocked, and
    # only when a provider is configured (resumed work may need to re-run agents).
    if settings.configured:
        threading.Thread(target=_safe_resume, daemon=True).start()
        # Part 2 trigger: watch the simulated SU inbox for arriving emails.
        inbox.start_watcher()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "provider": settings.provider, "configured": settings.configured}


@app.get("/api/customers")
def customers() -> dict:
    """Configured customers = rule-set files in config/rules/. Drives the UI dropdown
    so we never imply a customer works when no rule set exists for it."""
    return {"customers": available_customers()}


@app.post("/api/runs")
async def create_run(file: UploadFile = File(...), customer: str = Form(...)) -> dict:
    """Kick off a pipeline run and return immediately. The pipeline runs in a
    background thread, advancing status in the DB; the client polls GET
    /api/runs/{run_id} to watch extracting -> validating -> routing -> stored."""
    if not settings.configured:
        raise HTTPException(500, "LLM provider not configured. Copy .env.example to .env and set keys.")
    if customer not in available_customers():
        raise HTTPException(400, f"Unknown customer '{customer}'. Configured: {available_customers()}")

    data = await file.read()
    filename = file.filename or "upload"
    try:
        pages = to_page_images(data, filename)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not read document: {e}")

    run_id = str(uuid.uuid4())
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext = os.path.splitext(filename)[1] or ".bin"
    source_path = os.path.join(UPLOAD_DIR, f"{run_id}{ext}")
    with open(source_path, "wb") as f:
        f.write(data)

    store.create_run(run_id, customer, filename, source_path=source_path)
    # Background thread: each store call opens its own SQLite connection, so writing
    # from the worker while the request thread/poller reads is safe (WAL mode).
    threading.Thread(
        target=orchestrator.run_pipeline, args=(run_id, customer, pages), daemon=True
    ).start()
    return {"run_id": run_id, "status": "queued"}


@app.get("/api/runs")
def list_runs() -> dict:
    return {"runs": [r.model_dump() for r in store.list_runs()]}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    rec = store.get_run(run_id)
    if not rec:
        raise HTTPException(404, "run not found")
    return {"run": rec.model_dump(), "ledger": store.get_ledger(run_id), "cost_usd": store.run_cost(run_id)}


class QueryBody(BaseModel):
    question: str


@app.post("/api/query")
def query(body: QueryBody) -> dict:
    if not settings.configured:
        raise HTTPException(500, "LLM provider not configured.")
    return nl2sql.ask(body.question)


@app.get("/api/stats")
def stats() -> dict:
    """Dashboard aggregates over stored runs."""
    rows = store.execute_readonly(
        "SELECT outcome, COUNT(*) n FROM runs WHERE outcome IS NOT NULL GROUP BY outcome"
    )
    total = store.execute_readonly("SELECT COUNT(*) n FROM runs")[0]["n"]
    return {"total_runs": total, "by_outcome": rows}


@app.post("/api/resume")
def resume() -> dict:
    return {"resumed": orchestrator.resume_incomplete()}


# ---------------------------------------------------------------------------
# Part 2 — shipments (SU email -> CG review queue)
# ---------------------------------------------------------------------------
@app.get("/api/shipments")
def list_shipments() -> dict:
    ships = store.list_shipments()
    return {
        "shipments": [s.model_dump() for s in ships],
        "pending_review": sum(s.status == ShipmentStatus.PENDING_REVIEW for s in ships),
    }


@app.get("/api/shipments/{shipment_id}")
def get_shipment(shipment_id: str) -> dict:
    rec = store.get_shipment(shipment_id)
    if not rec:
        raise HTTPException(404, "shipment not found")
    runs = store.runs_for_shipment(shipment_id)
    return {
        "shipment": rec.model_dump(),
        "runs": [{**r.model_dump(), "ledger": store.get_ledger(r.run_id)} for r in runs],
        "shipment_ledger": store.get_shipment_ledger(shipment_id),
        "cost_usd": store.shipment_cost(shipment_id),
    }


class SendBody(BaseModel):
    draft: str


@app.post("/api/shipments/{shipment_id}/send")
def send_reply(shipment_id: str, body: SendBody) -> dict:
    """CG reviewed (and possibly edited) the draft and clicked send. This is the
    ONLY way a shipment reaches SENT — the agent never calls it. Sending here
    means recording the final text + timestamp (the audit trail); real mail
    plumbing would hang off this point."""
    rec = store.get_shipment(shipment_id)
    if not rec:
        raise HTTPException(404, "shipment not found")
    if rec.status != ShipmentStatus.PENDING_REVIEW:
        raise HTTPException(409, f"Shipment is '{rec.status.value}', not pending_review — nothing to send.")
    if not body.draft.strip():
        raise HTTPException(400, "Cannot send an empty reply.")
    store.update_shipment(shipment_id, ShipmentStatus.SENT, draft_final=body.draft)
    return {"shipment": store.get_shipment(shipment_id).model_dump()}


class SimulateBody(BaseModel):
    sample: str  # folder name under samples/shipments/, e.g. "clean_shipment"


@app.post("/api/simulate-email")
def simulate_email(body: SimulateBody) -> dict:
    """Demo helper: drop a sample SU email into the watched inbox. The watcher
    picks it up exactly as it would a real arrival — same code path."""
    src = SHIPMENT_SAMPLES_DIR / body.sample
    if not src.is_dir() or not (src / "email.json").exists():
        available = sorted(p.name for p in SHIPMENT_SAMPLES_DIR.glob("*/") if (p / "email.json").exists()) \
            if SHIPMENT_SAMPLES_DIR.exists() else []
        raise HTTPException(400, f"Unknown sample '{body.sample}'. Available: {available}")
    # Copy under a dot-name (watcher ignores it), then rename — so the folder
    # appears in the inbox atomically complete.
    stamp = uuid.uuid4().hex[:8]
    tmp = os.path.join(settings.inbox_dir, f".incoming-{stamp}")
    final = os.path.join(settings.inbox_dir, f"{body.sample}-{stamp}")
    os.makedirs(settings.inbox_dir, exist_ok=True)
    shutil.copytree(src, tmp)
    os.rename(tmp, final)
    return {"queued": os.path.basename(final)}
