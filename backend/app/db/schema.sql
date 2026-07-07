-- Nova pipeline persistence.
--
-- Two tables, both keyed by run_id:
--   runs   -> the checkpoint. status advances queued->extracting->...->stored,
--             and each agent's partial output is written BEFORE the next agent
--             starts. On restart we read the last row and resume from there.
--   ledger -> the observability spine. Every step appends a span (duration) and
--             its token cost. "Trace a shipment" == SELECT * FROM ledger WHERE run_id=?.

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    customer      TEXT NOT NULL,
    filename      TEXT NOT NULL,
    source_path   TEXT,                     -- retained upload, so extraction can also resume after a crash
    status        TEXT NOT NULL,            -- RunStatus enum
    extracted     TEXT,                     -- JSON: ExtractedDocument
    validation    TEXT,                     -- JSON: ValidationResult
    decision      TEXT,                     -- JSON: Decision
    outcome       TEXT,                     -- denormalised Outcome for fast queries
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_status  ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_outcome ON runs(outcome);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);

CREATE TABLE IF NOT EXISTS ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    step          TEXT NOT NULL,            -- 'extract' | 'validate' | 'route'
    model         TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd      REAL DEFAULT 0,
    duration_ms   INTEGER DEFAULT 0,
    detail        TEXT,                     -- freeform span note
    created_at    TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_ledger_run ON ledger(run_id);

-- Part 2: shipments. One SU email = one shipment = N runs (one per attachment).
-- Same checkpoint discipline as runs: status advances received->processing->
-- cross_validating->drafting->pending_review->sent, each step persisted before
-- the next starts, so shipment recovery mirrors run recovery.
CREATE TABLE IF NOT EXISTS shipments (
    shipment_id      TEXT PRIMARY KEY,
    customer         TEXT NOT NULL,
    from_addr        TEXT NOT NULL DEFAULT '',
    subject          TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL,            -- ShipmentStatus enum
    cross_validation TEXT,                     -- JSON: CrossValidationResult
    decision         TEXT,                     -- JSON: ShipmentDecision
    outcome          TEXT,                     -- denormalised Outcome for fast queries
    draft_final      TEXT,                     -- what CG actually sent (audit trail)
    error            TEXT,
    received_at      TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    sent_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_shipments_status  ON shipments(status);
CREATE INDEX IF NOT EXISTS idx_shipments_outcome ON shipments(outcome);

-- Shipment-level trace spans (cross_validate, draft) — the per-doc spans stay
-- in `ledger` keyed by run_id; shipment cost = its runs' ledger + this table.
CREATE TABLE IF NOT EXISTS shipment_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id   TEXT NOT NULL,
    step          TEXT NOT NULL,               -- 'cross_validate' | 'draft'
    model         TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd      REAL DEFAULT 0,
    duration_ms   INTEGER DEFAULT 0,
    detail        TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id)
);

CREATE INDEX IF NOT EXISTS idx_shipment_ledger ON shipment_ledger(shipment_id);
