# Sample NL → SQL Queries

The "Ask your data" box (and `POST /api/query`) turns a natural-language question
into a single **read-only** SQL SELECT over the `runs` table, executes it behind a
guard that rejects any mutation, and answers from the actual rows returned.

Try these after running a few documents through the pipeline:

| Question | Compiles to (approx) |
|---|---|
| How many shipments were flagged for human review? | `SELECT COUNT(*) FROM runs WHERE outcome='human_review'` |
| How many documents were auto-approved this week? | `SELECT COUNT(*) FROM runs WHERE outcome='auto_approve' AND created_at >= date('now','-7 days')` |
| How many amendment requests did we draft? | `SELECT COUNT(*) FROM runs WHERE outcome='amendment'` |
| Break down all runs by outcome. | `SELECT outcome, COUNT(*) n FROM runs GROUP BY outcome` |
| How many documents did we process for ACME-IMPORTS? | `SELECT COUNT(*) FROM runs WHERE customer='ACME-IMPORTS'` |
| Which runs failed? | `SELECT run_id, filename, status FROM runs WHERE status='failed'` |
| What was the most recent document processed? | `SELECT filename, created_at FROM runs ORDER BY created_at DESC LIMIT 1` |

### Part 2: shipment-level questions

Email-triggered work lands in the `shipments` table (one row per SU email, joined
to `runs` via `shipment_id`), so CG-workflow questions work too:

| Question | Compiles to (approx) |
|---|---|
| Show me everything pending review for customer ACME-IMPORTS. | `SELECT * FROM shipments WHERE customer='ACME-IMPORTS' AND status='pending_review'` |
| How many shipments needed an amendment this week? | `SELECT COUNT(*) FROM shipments WHERE outcome='amendment' AND received_at >= date('now','-7 days')` |
| How many replies has CG sent? | `SELECT COUNT(*) FROM shipments WHERE status='sent'` |
| Which suppliers sent us shipments? | `SELECT DISTINCT from_addr FROM shipments` |
| How many documents arrived per shipment? | `SELECT shipment_id, COUNT(*) FROM runs WHERE shipment_id IS NOT NULL GROUP BY shipment_id` |

**Grounding:** the final answer is generated only from the rows the query returned.
If the query matches nothing, the system says so rather than guessing.

**Safety:** `execute_readonly()` rejects anything that isn't a single `SELECT`
(no `;`, `insert`, `update`, `delete`, `drop`, `alter`, `attach`, `pragma`,
`create`). The NL→SQL layer cannot mutate data.

### Try it from the CLI

```bash
curl -s -X POST http://localhost:8000/api/query \
  -H 'Content-Type: application/json' \
  -d '{"question":"how many shipments were flagged for human review?"}' | python -m json.tool
```

Returns `{question, sql, rows, answer, error}` - note the `sql` field so you can
see exactly what ran.
