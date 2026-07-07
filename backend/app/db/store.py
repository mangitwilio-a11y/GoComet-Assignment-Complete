"""
SQLite persistence — the single source of truth.

`runs` is the checkpoint table: the orchestrator advances `status` and writes
each agent's partial output BEFORE the next agent runs, so a crash can resume.
`ledger` is the trace: every step appends a span + token cost keyed by run_id.

The UI and query layer read from here only — they never touch the live pipeline.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.models import (
    CrossValidationResult,
    Decision,
    ExtractedDocument,
    RunRecord,
    RunStatus,
    ShipmentDecision,
    ShipmentRecord,
    ShipmentStatus,
    ValidationResult,
)
from app.settings import settings

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        # WAL lets the polling UI read while the background pipeline thread writes.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_PATH.read_text())
        # Migration for databases created before Part 2: runs gained shipment_id.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        if "shipment_id" not in cols:
            conn.execute("ALTER TABLE runs ADD COLUMN shipment_id TEXT")


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------
def create_run(
    run_id: str,
    customer: str,
    filename: str,
    source_path: Optional[str] = None,
    shipment_id: Optional[str] = None,
) -> None:
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, customer, filename, source_path, shipment_id, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, customer, filename, source_path, shipment_id, RunStatus.QUEUED.value, now, now),
        )


def get_source_path(run_id: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT source_path FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return row["source_path"] if row else None


def update_run(
    run_id: str,
    status: RunStatus,
    extracted: Optional[ExtractedDocument] = None,
    validation: Optional[ValidationResult] = None,
    decision: Optional[Decision] = None,
    error: Optional[str] = None,
) -> None:
    """Checkpoint: persist new status + any newly-available partial output.

    Only non-None fields are written, so each call layers on top of the prior
    checkpoint without erasing earlier partial results.
    """
    sets = ["status = ?", "updated_at = ?"]
    vals: list = [status.value, _now()]
    if extracted is not None:
        sets.append("extracted = ?")
        vals.append(extracted.model_dump_json())
    if validation is not None:
        sets.append("validation = ?")
        vals.append(validation.model_dump_json())
    if decision is not None:
        sets.append("decision = ?")
        vals.append(decision.model_dump_json())
        sets.append("outcome = ?")
        vals.append(decision.outcome.value)
    if error is not None:
        sets.append("error = ?")
        vals.append(error)
    vals.append(run_id)
    with _connect() as conn:
        conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id = ?", vals)


def append_ledger(
    run_id: str,
    step: str,
    model: str = "",
    prompt_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    duration_ms: int = 0,
    detail: str = "",
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO ledger (run_id, step, model, prompt_tokens, output_tokens, cost_usd, "
            "duration_ms, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, step, model, prompt_tokens, output_tokens, cost_usd, duration_ms, detail, _now()),
        )


def run_cost(run_id: str) -> float:
    with _connect() as conn:
        row = conn.execute("SELECT COALESCE(SUM(cost_usd),0) c FROM ledger WHERE run_id=?", (run_id,)).fetchone()
        return float(row["c"]) if row else 0.0


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def _row_to_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        customer=row["customer"],
        filename=row["filename"],
        status=RunStatus(row["status"]),
        shipment_id=row["shipment_id"] if "shipment_id" in row.keys() else None,
        extracted=ExtractedDocument(**json.loads(row["extracted"])) if row["extracted"] else None,
        validation=ValidationResult(**json.loads(row["validation"])) if row["validation"] else None,
        decision=Decision(**json.loads(row["decision"])) if row["decision"] else None,
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_run(run_id: str) -> Optional[RunRecord]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return _row_to_record(row) if row else None


def get_ledger(run_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT step, model, prompt_tokens, output_tokens, cost_usd, duration_ms, detail, created_at "
            "FROM ledger WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_runs(limit: int = 100) -> list[RunRecord]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_record(r) for r in rows]


def incomplete_runs() -> list[RunRecord]:
    """Runs that did not reach STORED/FAILED — candidates for crash recovery."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE status NOT IN (?, ?)",
            (RunStatus.STORED.value, RunStatus.FAILED.value),
        ).fetchall()
        return [_row_to_record(r) for r in rows]


# ---------------------------------------------------------------------------
# Shipments (Part 2) — same checkpoint/layering discipline as runs.
# ---------------------------------------------------------------------------
def create_shipment(shipment_id: str, customer: str, from_addr: str, subject: str) -> None:
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO shipments (shipment_id, customer, from_addr, subject, status, "
            "received_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (shipment_id, customer, from_addr, subject, ShipmentStatus.RECEIVED.value, now, now),
        )


def update_shipment(
    shipment_id: str,
    status: ShipmentStatus,
    cross_validation: Optional[CrossValidationResult] = None,
    decision: Optional[ShipmentDecision] = None,
    draft_final: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Checkpoint: layer new status + newly-available output, like update_run."""
    sets = ["status = ?", "updated_at = ?"]
    vals: list = [status.value, _now()]
    if cross_validation is not None:
        sets.append("cross_validation = ?")
        vals.append(cross_validation.model_dump_json())
    if decision is not None:
        sets.append("decision = ?")
        vals.append(decision.model_dump_json())
        sets.append("outcome = ?")
        vals.append(decision.outcome.value)
    if draft_final is not None:
        sets.append("draft_final = ?")
        vals.append(draft_final)
    if status == ShipmentStatus.SENT:
        sets.append("sent_at = ?")
        vals.append(_now())
    if error is not None:
        sets.append("error = ?")
        vals.append(error)
    vals.append(shipment_id)
    with _connect() as conn:
        conn.execute(f"UPDATE shipments SET {', '.join(sets)} WHERE shipment_id = ?", vals)


def _row_to_shipment(row: sqlite3.Row) -> ShipmentRecord:
    return ShipmentRecord(
        shipment_id=row["shipment_id"],
        customer=row["customer"],
        from_addr=row["from_addr"],
        subject=row["subject"],
        status=ShipmentStatus(row["status"]),
        cross_validation=CrossValidationResult(**json.loads(row["cross_validation"]))
        if row["cross_validation"] else None,
        decision=ShipmentDecision(**json.loads(row["decision"])) if row["decision"] else None,
        draft_final=row["draft_final"],
        error=row["error"],
        received_at=row["received_at"],
        updated_at=row["updated_at"],
        sent_at=row["sent_at"],
    )


def get_shipment(shipment_id: str) -> Optional[ShipmentRecord]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,)).fetchone()
        return _row_to_shipment(row) if row else None


def list_shipments(limit: int = 100) -> list[ShipmentRecord]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM shipments ORDER BY received_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_shipment(r) for r in rows]


def runs_for_shipment(shipment_id: str) -> list[RunRecord]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE shipment_id=? ORDER BY created_at", (shipment_id,)
        ).fetchall()
        return [_row_to_record(r) for r in rows]


def incomplete_shipments() -> list[ShipmentRecord]:
    """Shipments interrupted mid-pipeline. PENDING_REVIEW is NOT incomplete —
    it is the terminal agent state; only the CG human advances it to SENT."""
    done = (ShipmentStatus.PENDING_REVIEW.value, ShipmentStatus.SENT.value, ShipmentStatus.FAILED.value)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM shipments WHERE status NOT IN (?,?,?)", done
        ).fetchall()
        return [_row_to_shipment(r) for r in rows]


def append_shipment_ledger(
    shipment_id: str,
    step: str,
    model: str = "",
    prompt_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    duration_ms: int = 0,
    detail: str = "",
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO shipment_ledger (shipment_id, step, model, prompt_tokens, output_tokens, "
            "cost_usd, duration_ms, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (shipment_id, step, model, prompt_tokens, output_tokens, cost_usd, duration_ms, detail, _now()),
        )


def get_shipment_ledger(shipment_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT step, model, prompt_tokens, output_tokens, cost_usd, duration_ms, detail, created_at "
            "FROM shipment_ledger WHERE shipment_id=? ORDER BY id", (shipment_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def shipment_cost(shipment_id: str) -> float:
    """Total spend: every per-doc run's ledger plus the shipment-level spans."""
    with _connect() as conn:
        doc = conn.execute(
            "SELECT COALESCE(SUM(l.cost_usd),0) c FROM ledger l "
            "JOIN runs r ON r.run_id = l.run_id WHERE r.shipment_id=?",
            (shipment_id,),
        ).fetchone()["c"]
        ship = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) c FROM shipment_ledger WHERE shipment_id=?",
            (shipment_id,),
        ).fetchone()["c"]
        return float(doc) + float(ship)


# ---------------------------------------------------------------------------
# Read-only query execution (for the NL->SQL layer)
# ---------------------------------------------------------------------------
# Mutating keywords rejected as defense-in-depth. Matched on WORD BOUNDARIES, not
# as substrings — the old substring check rejected legitimate columns like
# `created_at` (contains "create") and `updated_at` (contains "update").
_FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "attach", "detach",
    "pragma", "create", "replace", "truncate", "vacuum", "reindex",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b")


def execute_readonly(sql: str) -> list[dict]:
    """Run a single SELECT against the runs table.

    Two independent guards, so neither is load-bearing alone:
      1. The connection is opened READ-ONLY (`mode=ro` URI + `PRAGMA query_only`),
         so the database engine itself refuses any write — this is the real guard.
      2. A surface check rejects non-SELECTs, statement chaining (`;`), and
         mutating keywords on word boundaries — defense-in-depth and clearer errors.
    """
    cleaned = sql.strip().rstrip(";").strip()
    lowered = cleaned.lower()
    if not lowered.startswith("select"):
        raise ValueError("Only SELECT statements are allowed.")
    if ";" in cleaned:
        raise ValueError("Multiple statements are not allowed.")
    m = _FORBIDDEN_RE.search(lowered)
    if m:
        raise ValueError(f"Query contains a forbidden keyword: '{m.group(1)}'.")

    # Read-only connection: the engine rejects writes even if the checks above are
    # somehow bypassed. WAL is required so a read-only handle can open the db while
    # the pipeline thread holds a write lock.
    uri = f"file:{Path(settings.db_path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(cleaned).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
