# Nova · Multi-Agent Trade-Document Pipeline

A three-agent system that ingests a trade document (PDF or image), **extracts**
structured fields with a vision LLM, **validates** them against a per-customer
rule set, and **routes** the result to one of three outcomes - auto-approve,
human review, or a drafted amendment request. Verified outputs land in SQLite
and are queryable in natural language.

> **One engine, many configs.** The three agents are generic. The per-customer
> rule set (`backend/app/config/rules/`) is the product surface - it is what
> makes the pipeline customer-specific. Swapping the customer is a config change,
> not a code change.

```
Document ──▶ Extractor ──▶ Validator ──▶ Router ──┬─▶ auto-approve ─┐
 (PDF/img)   (vision +     (rules +      (decide   ├─▶ human review  ├─▶ SQLite ──▶ NL→SQL ──▶ UI
              confidence)   fuzzy match)  + explain)└─▶ amendment ────┘   (runs + ledger)
              ▲──────────────── Orchestrator: retries · budget cap · checkpoints ───────────────▲
```

---

## Prerequisites

- Python 3.10+
- Node.js 18+
- An LLM provider key. **Azure OpenAI** (default) or vanilla **OpenAI**.
  The vision deployment must be a vision-capable model (e.g. `gpt-4o`).

The pipeline is model-agnostic: switching providers is an env change, no code edits.

---

## 1. Backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then edit .env with your keys (see below)
uvicorn app.main:app --reload --port 8000
```

### Configure `.env`

For **Azure OpenAI** (default):

```env
LLM_PROVIDER=azure
AZURE_OPENAI_API_KEY=<your-key>
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
AZURE_OPENAI_API_VERSION=2024-10-21
AZURE_OPENAI_VISION_DEPLOYMENT=<your-gpt-4o-deployment-name>
AZURE_OPENAI_TEXT_DEPLOYMENT=<your-gpt-4o-mini-deployment-name>
```

> Azure uses **deployment names**, not model names. Create the deployments in the
> Azure portal first and put their names here.

For **vanilla OpenAI**: set `LLM_PROVIDER=openai` and `OPENAI_API_KEY=...`.

Check it's wired: `curl http://localhost:8000/api/health` → `{"ok":true,"configured":true}`.

## 2. Frontend

```bash
cd frontend
npm install
npm run dev          # http://localhost:3000
```

Open http://localhost:3000, upload a document from `samples/`, and click **Run pipeline**.

---

## Tests

Trust-invariant suite (no API key needed - the LLM is faked):

```bash
cd backend && source .venv/bin/activate
pytest -q
```

27 tests covering the safety properties. Part 1 (`test_trust_invariants.py`): wrong
units never auto-approve, missing / ungrounded values become `uncertain`, malformed
or stringy semantic verdicts become `uncertain` (not a silent match), provider
failure preserves deterministic routing, all three outcomes are reachable end-to-end,
and unknown customers are rejected. Part 2 (`test_shipment_invariants.py`):
cross-document contradictions are caught even when every document passes its own
rules, inconclusive cross-checks degrade to `uncertain`, the shipment pipeline's
terminal state is `pending_review` (the agent can never send), every outcome carries
a CG-reviewable draft even in a provider outage, and bad emails (unknown customer, no
attachments) fail loud in the queue instead of vanishing.

---

## Sample documents

Pre-generated in `samples/` (regenerate with `python samples/generate_samples.py`):

| File | Quality | Expected outcome |
|---|---|---|
| `clean_commercial_invoice.pdf` | crisp, all fields satisfy ACME rules | **auto-approve** |
| `messy_bill_of_lading.jpg` | low-res, rotated, blurred scan; `DDP` incoterm (not allowed) | **amendment** |
| `incomplete_packing_list.pdf` | legible but genuinely missing invoice no. + HS code | **human review** (uncertain fields surface) |

These three cover all three router outcomes live.

---

## Part 2 — the CG workflow (email-triggered, multi-doc)

Part 1's agents wired into the real SU → CG → customer loop. No process change:
SU still emails documents, CG still validates and replies — the agent does the
reading and the typing, and **CG always clicks send**.

Open **http://localhost:3000/inbox** (backend + frontend running as above).

- **Trigger** — the backend watches a simulated inbox folder (`backend/inbox/`).
  One "email" = a folder containing `email.json` (`{"from", "subject", "customer",
  "body"}`) plus attached PDFs/images, `email.json` written last. The UI's
  *Simulate SU email* buttons drop a sample in; the watcher picks it up within ~2s
  — the exact same code path a real arrival would take. Swapping in real plumbing
  (IMAP/webhook) replaces only `app/inbox.py`.
- **Multi-doc extraction** — each attachment becomes one Part 1 run (extract →
  validate → route), unchanged.
- **Cross-document consistency** — consignee, HS code, ports, incoterm, gross
  weight and invoice number must agree *across* BOL / Invoice / Packing List.
  The `messy_shipment` sample is the proof case: every document passes ACME's
  rules individually, but the packing list's HS code and the BOL's weight
  contradict the other documents — only the cross-check catches it.
- **Decide & draft** — one deterministic shipment verdict (any uncertain → human
  review; any mismatch/inconsistency → amendment; else approve) plus one drafted
  reply to SU listing every discrepancy as `document / field: found X, expected Y`.
- **Hand off** — the pipeline's terminal state is `pending_review`. The CG operator
  edits the draft and clicks *Approve & send*; the sent text and timestamp are
  stored next to the agent's original (audit trail). The agent has no send path.
- **Queryable** — "show me everything pending review for customer ACME-IMPORTS"
  works in the *Ask your data* box (shipments table joined to runs).

Sample shipments live in `samples/shipments/` (regenerate with
`python samples/generate_shipments.py`):

| Sample | What's in it | Expected outcome |
|---|---|---|
| `clean_shipment` | BOL + Invoice + Packing List, all consistent | **auto-approve** + approval draft |
| `messy_shipment` | each doc passes ACME's rules alone, but HS code + gross weight conflict across docs | **amendment** + discrepancy email |

To start the demo from an empty queue:
`sqlite3 backend/nova.db "DELETE FROM shipment_ledger; DELETE FROM ledger WHERE run_id IN (SELECT run_id FROM runs WHERE shipment_id IS NOT NULL); DELETE FROM runs WHERE shipment_id IS NOT NULL; DELETE FROM shipments;"`

See `docs/PRD_PART2.md` for the one-page Part 2 PRD.

---

## What to look at

- **Per-field confidence + source quote** - every extracted field shows the verbatim
  text it came from. No quote ⇒ flagged ungrounded (hallucination guard).
- **Validation table** - `match` / `mismatch` / `uncertain`, with deterministic vs
  semantic method labels. Uncertain fields always surface; nothing is silently approved.
- **Decision + reasoning** - the router explains *why*, and drafts an amendment email
  when fields mismatch.
- **Trace + cost ledger** - per-step model, tokens, USD cost, and latency, keyed by `run_id`.
- **Ask your data** - natural-language questions (e.g. *"how many shipments were flagged
  this week?"*) compiled to read-only SQL and answered from real rows.

See `docs/sample_queries.md` for queries to try, `docs/PRD.md` and
`docs/TECHNICAL_WRITEUP.md` for the design.

---

## Project layout

```
backend/
  app/
    agents/        extractor.py · validator.py · router.py · cross_validator.py (Part 2)
    llm/client.py  model-agnostic provider (Azure/OpenAI), token+cost accounting
    db/            schema.sql · store.py (runs + ledger + shipments)
    query/nl2sql.py  natural-language → read-only SQL
    orchestrator.py  retries · budget cap · checkpoints · resume (runs + shipments)
    inbox.py         Part 2 trigger: watched-folder SU inbox → shipment pipeline
    ingest.py        PDF/image → page PNGs (PyMuPDF, no system deps)
    models.py        Pydantic contracts (the spine)
    config/rules/    one rule-set file per customer (ACME-IMPORTS.json) - the config layer
    main.py          FastAPI surface
frontend/            Next.js UI — / (single doc) · /inbox (Part 2 CG review queue)
samples/             three test docs + two sample shipments + generators
docs/                PRD (x2) · technical write-up · sample queries
```

> **Multi-customer is config, not code.** Each customer is a file in
> `config/rules/<CUSTOMER>.json`. The UI customer dropdown is populated from
> `/api/customers` (the files present), and an unknown customer is rejected - the
> system never silently falls back to another customer's rules. Only `ACME-IMPORTS`
> ships here, but adding a customer is dropping in a file.

## Design notes (the short version)

- **Why three agents, not one prompt?** Only extraction genuinely needs the LLM's
  full power. Validation is mostly deterministic Python (regex/enum/numeric); routing
  is a deterministic decision tree. Three stages let us push deterministic work to
  code and reserve the model for vision + natural-language generation - minimizing
  hallucination surface and cost. One giant prompt forces everything through the LLM.
- **Crash recovery** - a run is a row, not a function call. Status advances
  `queued → extracting → validating → routing → stored`; each agent's partial output
  is checkpointed *before* the next runs. A crash *after* extraction resumes for free
  from the persisted JSON (no re-paying for the expensive vision call); a crash
  *before* extraction re-ingests from the retained upload, so the document is not
  lost. Recovery runs **automatically on startup** (in a background thread) and is
  also exposed as `POST /api/resume`.
- **Graceful degradation** - if the LLM provider is unavailable, the deterministic
  layer still works: semantic checks degrade to `uncertain` (human review) and the
  router still produces its decision plus a deterministic amendment draft. A provider
  outage never destroys routing or fails the run silently.
- **Confidence & grounding (honest framing)** - every field carries a `source_quote`
  as *evidence* it was read from the document, not *proof*: a model can fabricate a
  value and a quote together, and we do not yet verify the quote against an
  independent OCR pass. The real backstops are the deterministic rules (which catch a
  wrong value regardless of confidence) and the "missing/ungrounded ⇒ uncertain"
  escalation. Self-reported confidence is treated as a routing signal, not truth.
- **Guardrails live on the orchestrator**, not the agents: per-step retries, a hard
  per-document USD budget cap, bounded retries.
