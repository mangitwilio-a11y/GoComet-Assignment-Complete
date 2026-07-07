"""
Natural-language query over stored runs.

Two-hop, grounded: (1) LLM writes a single read-only SELECT against a schema we
describe to it; (2) we execute it with the store's read-only guard; (3) LLM turns
the actual rows into a plain-English answer. The answer is grounded in real rows,
not the model's memory — if the query returns nothing, the answer says so.
"""
from __future__ import annotations

from app.db import store
from app.llm.client import get_client

SCHEMA_DOC = """Table: runs  -- one processed document
Columns:
  run_id TEXT, customer TEXT, filename TEXT,
  shipment_id TEXT -- NULL for direct uploads; join key to shipments for email-triggered docs
  status TEXT  -- one of: queued, extracting, validating, routing, stored, failed
  outcome TEXT -- one of: auto_approve, human_review, amendment, or NULL if not yet routed
  created_at TEXT -- ISO-8601 UTC timestamp
  updated_at TEXT

Table: shipments  -- one supplier (SU) email carrying one or more documents
Columns:
  shipment_id TEXT, customer TEXT, from_addr TEXT, subject TEXT,
  status TEXT  -- one of: received, processing, cross_validating, drafting, pending_review, sent, failed
  outcome TEXT -- one of: auto_approve, human_review, amendment, or NULL if not yet decided
  received_at TEXT -- ISO-8601 UTC timestamp
  updated_at TEXT, sent_at TEXT -- sent_at set when the CG operator sent the reply

Notes:
- Shipment questions (emails, pending review queue, replies sent) => shipments table.
  Document questions => runs table. Join on shipment_id when both are needed.
- "pending review" / "waiting for CG" => shipments.status = 'pending_review'
- "flagged" / "needs review" => outcome = 'human_review'
- "amendment" / "discrepancy email" => outcome = 'amendment'
- "approved" => outcome = 'auto_approve'
- "this week" => created_at >= date('now','-7 days') (received_at for shipments)
- Use date(created_at) for day comparisons.
"""

SQL_SYSTEM = f"""You translate a question into ONE SQLite SELECT over this schema.
{SCHEMA_DOC}
Rules: SELECT only; no semicolons; no writes. Prefer COUNT/GROUP BY for "how many".
Return ONLY JSON: {{"sql": "SELECT ..."}}."""

ANSWER_SYSTEM = """You answer a trade-ops question using ONLY the provided rows. Be concise and
factual. If rows are empty, say no matching records were found. Return ONLY JSON: {"answer": "..."}."""


def ask(question: str) -> dict:
    client = get_client()
    # Hop 1: NL -> SQL
    gen = client.text_json(SQL_SYSTEM, f"Question: {question}")
    sql = gen.json().get("sql", "").strip()

    # Hop 2: execute (read-only guarded)
    try:
        rows = store.execute_readonly(sql)
    except Exception as e:
        return {"question": question, "sql": sql, "error": str(e), "answer": None, "rows": []}

    # Hop 3: rows -> grounded NL answer
    ans = client.text_json(
        ANSWER_SYSTEM,
        f"Question: {question}\nSQL: {sql}\nRows (JSON): {rows}",
    )
    answer = ans.json().get("answer")
    return {"question": question, "sql": sql, "rows": rows, "answer": answer, "error": None}
