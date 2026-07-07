# PRD - Multi-Agent Trade-Document Pipeline (Nova)

**Author:** Mangilal ·  **Role:** Full-Stack AI Engineer

---

## 1. Understanding Nova

### What is Nova? What can it do that traditional SaaS can't?
Traditional SaaS hands you a tool and a blank configuration screen, then leaves
you to bend your process to fit it. Nova inverts that. It is an agentic platform
that absorbs a customer's *actual* messy operating process - the rules that live
in an ops person's head, the four-cycle email loops, the customer-specific
exceptions - and runs it. Where SaaS sells software and charges for seats, Nova
delivers a working outcome: documents validated, exceptions surfaced, decisions
drafted. The unlock is LLMs plus agents: for the first time software can read an
unstructured PDF, apply judgement against fuzzy rules, and *act* - not just store
a record and wait for a human. Traditional SaaS can digitize a form; it cannot
read a smudged Bill of Lading, notice the Incoterm violates this customer's
contract, and draft the amendment email. Nova can. The product is not the UI -
the product is the customer's process, automated. And critically, Nova is
*governed*: its agents run with audit trails, evidence grounding, cost controls,
and tenant isolation - which is what separates a real decision-making agent in an
enterprise from a chatbot that "summarizes a PDF." That governance is not an
add-on; it is the reason an enterprise can trust an agent to *act*. It also sits as
an AI-native operating layer over GoComet's existing products (visibility,
procurement, contracts, invoices) rather than as a standalone tool - the layer that
ties them together.

### What is the FDE model and why does GoComet use it for Nova?
A Forward Deployed Engineer is one person who owns a customer's *outcome*
end-to-end - not a task in a backlog. They sit with the customer, learn the *real*
workflow (not the sanitized SOP), then build, configure, deploy, and keep running
the solution themselves. No spec thrown over a wall: the person who discovers the
problem is the person who ships it and fixes what breaks in production. GoComet
uses this model because the hard part of trade-document automation is not the model
- it is the thousand undocumented, customer-specific rules ("Acme only ships FOB or
CIF"; "this consignee tolerates ±5% on weight"). You cannot gather those from a
requirements doc; you discover them sitting next to the operator. The FDE captures
that tacit knowledge as configuration on top of a stable, generic agentic core -
which is exactly why this architecture separates a *generic engine* from a
*per-customer config*: it is the FDE model expressed in code. One engineer paired
with a client partner ships a working outcome in weeks, and the platform then
generalizes what they learned.

### What does "System of Outcomes" mean?
A **System of Record** stores truth (an ERP, a database) - it remembers. A
**System of Engagement** is where people interact with that truth (email, a
dashboard, a ticketing UI) - it routes attention. A **System of Outcomes** goes
one step further: it *does the work* and is measured by the result, not by usage.
Nova is not graded on logins or records stored; it is graded on "documents
auto-cleared correctly" and "operator hours saved." The trade-doc pipeline is a
system of outcomes because its job is to *complete* the validation - extract,
check, decide, draft - and hand a human only the genuine exceptions. Success is a
number about the business outcome, not engagement. That reframing changes what we
build: every design choice below is justified by whether it moves the outcome
metric, and trust/observability are first-class because an outcome system that
cannot be trusted or audited is worthless.

---

## 2. Problem Statement

### Where the current trade-doc validation flow breaks
Today the flow is humans reading PDFs field-by-field against rules held in memory.
Specific failure modes:

1. **Manual extraction is slow and inconsistent.** A person re-keys 8-15 fields
   per document across BoL, Invoice, Packing List, CoO. Throughput is capped by
   attention; accuracy degrades with fatigue.
2. **Rules live in people's heads.** "This customer only accepts FOB/CIF" is tacit
   knowledge. When the person is on leave, the rule is gone, and errors slip through.
3. **Silent errors are expensive.** A missed Incoterm mismatch or wrong HS code
   surfaces downstream as a customs hold, a demurrage charge, or a rejected LC -
   days later, far from where it was introduced.
4. **The exception loop is brutal.** A discrepancy triggers a manual back-and-forth
   email thread with the supplier (the "4-cycle email loop"), each cycle costing a
   day. No one tracks where a given shipment is in that loop.
5. **No memory, no audit trail.** When something goes wrong there is no record of
   *why* a document was approved or who decided.

### Success in a CG operator's first 5 minutes
The operator drops in a document and within ~30 seconds sees: every field
extracted with a confidence bar and the **exact source text** it came from; a
clear match/mismatch/uncertain verdict per field against *their* customer's rules;
a decision (approve / review / amend) with a plain-English reason; and, for a bad
doc, a ready-to-send amendment email listing each discrepancy. They trust it
because nothing is hidden - low-confidence and ungrounded fields are flagged, not
silently passed. They feel they just offloaded the boring 80% and kept control of
the 20% that needs judgement.

---

## 3. Users & Jobs-to-be-Done

### Personas
- **Priya - CG Operations Specialist (operator).** Non-technical. Processes 60-120
  documents/day across many customers, each with different rules. Measured on
  throughput and zero customs holds. Pain: re-keying, remembering per-customer
  rules, chasing suppliers. Wants to trust automation but has been burned by tools
  that confidently approve garbage.
- **Wei - Supplier Export Coordinator (supplier, SU).** Issues the documents on
  the other side. Wants amendment requests that are specific and actionable so he
  can fix and resend in one cycle, not three. Pain: vague "please correct" emails
  that don't say what's wrong.

### JTBD statements
1. **When** a new trade document arrives, **I want to** get every required field
   extracted with a confidence and its source text, **so that** I don't re-key and
   I can trust what I didn't read myself.
2. **When** I validate a document against a customer's rules, **I want to** see
   exactly which fields match, mismatch, or are uncertain, **so that** I only spend
   attention on the genuine problems.
3. **When** the system is unsure about a field, **I want to** have it flagged
   loudly rather than auto-approved, **so that** a low-confidence read never becomes
   a customs hold.
4. **When** a document has discrepancies, **I want to** get a drafted amendment
   email listing each one, **so that** the supplier can fix everything in a single
   cycle instead of four.
5. **When** I onboard a new customer, **I want to** express their rules as
   configuration rather than code, **so that** their specific requirements are live
   without an engineering project.
6. **When** my manager asks "how many shipments were flagged this week?", **I want
   to** ask in plain English and get a grounded answer, **so that** I don't wait on
   a BI report.
7. **When** the pipeline crashes mid-run, **I want to** have it resume from the last
   completed step, **so that** I don't re-pay for expensive extraction or lose work.

---

## 4. Agent Architecture (technical core)

### Why three agents - not one prompt, not five?
**Not one prompt:** the three jobs have fundamentally different reliability needs.
Extraction is a perception problem (read pixels → text) that genuinely needs a
vision LLM. Validation is mostly *deterministic computation* - does this HS code
match a regex and an allowlist, is the weight within ±5%? Routing is a
*deterministic decision tree*. Collapsing all three into one prompt forces the
deterministic work through the LLM, which (a) maximizes hallucination surface,
(b) makes the verdict unauditable, and (c) wastes tokens. Separating them lets us
push validation/routing to plain Python and reserve the model for what only it can
do: reading documents and writing natural language. **The boundary is drawn at
"does this step actually need an LLM?"**

**Not five:** we deliberately did *not* add a "classify document type" agent or a
"normalize fields" agent. Classification has no distinct failure mode worth
isolating - it folds into the extractor's first output field (`doc_type`).
Normalization is a few lines of deterministic code. Adding agents adds handoffs,
latency, and failure points without adding a new *kind* of judgement. Three is the
count where each agent owns a distinct failure mode: misreading (extractor), wrong
verdict (validator), wrong action (router).

### Each agent - responsibility / input / output (planner-executor-verifier lens)
| Agent | Role | Input | Output |
|---|---|---|---|
| **Extractor** (executor) | Read the document, emit structured fields | page images | `ExtractedDocument`: per field `{value, confidence, source_quote}` |
| **Validator** (verifier) | Check fields against the customer rule set | `ExtractedDocument` + `rules.json` | `ValidationResult`: per field `match/mismatch/uncertain` + reason + method |
| **Router** (planner of next action) | Decide the outcome and explain it | `ValidationResult` | `Decision`: `auto_approve / human_review / amendment` + reasoning (+ draft) |

The **orchestrator** is not an agent - it is a thin controller that sequences the
three, enforces guardrails, and persists state.

### How agents communicate
**Structured handoff, not shared free-form memory.** Each agent's output is a
validated Pydantic object that is the next agent's typed input. There is no shared
scratchpad the agents can both scribble on - that would reintroduce the coupling
we split them to avoid. The orchestrator owns the data and passes typed objects.
Every handoff is also persisted (see below), so the "message bus" and the
checkpoint store are the same thing.

### How state survives a crash
**A run is a row, not a function call.** The orchestrator advances a `status`
column `queued → extracting → validating → routing → stored`, and writes each
agent's partial output to the `runs` table *before* invoking the next agent. On
restart, `resume_incomplete()` reads any run not in a terminal state and skips
steps whose output is already persisted - so a crash during validation resumes
from the persisted extraction JSON and never re-pays for the expensive vision
call. A crash *before* extraction completed is also recoverable: the uploaded
document is retained (`source_path`) so the run re-ingests and re-extracts rather
than losing the document; only if the upload was never retained do we fail and
ask for a re-upload. A retried step overwrites its own checkpoint (idempotent),
so resume never duplicates work. Recovery runs automatically on startup (in a
background thread) and is also exposed as an explicit `POST /api/resume`.

---

## 5. LLM & Tooling Choices

- **Vision model (Extractor): `gpt-4o`-class.** Strong document OCR + layout
  understanding, native structured-output (JSON) mode, and available on Azure
  OpenAI (the customer's likely enterprise procurement path). Tradeoff: vision
  tokens are the dominant cost and latency - accepted because extraction is the one
  step that genuinely needs this capability.
  **Fallback for bad docs:** images are upscaled at 200 DPI on ingest; the grounded
  prompt forces `source_quote` and caps confidence when text can't be located, so a
  bad scan degrades to *low confidence → human review* rather than a confident wrong
  read. (Future: a second pass at higher detail / a stronger model only on
  low-confidence fields - escalate, don't blanket-upgrade.)
- **Text model (Validator semantic checks, Router reasoning, NL→SQL): `gpt-4o-mini`-class.**
  Cheap, fast, sufficient for short semantic-equality judgements and email drafting.
  ~15-40× cheaper than the vision model; using it for the non-perception work is the
  single biggest cost lever.
- **Orchestration framework: custom controller on the raw SDK - a deliberate,
  scoped call.** LangGraph is Nova's own agent-orchestration layer and the right
  tool once a flow is cyclic or has dynamic branching - but this pipeline is a fixed
  three-step DAG with deterministic branching, expressible in ~150 lines. For that
  shape a custom controller keeps the retry/budget/checkpoint logic *explicit and
  auditable*, where a graph framework would add a dependency and abstraction that
  make crash recovery *harder* to reason about, not easier. I'd adopt LangGraph the
  moment the pipeline needs cyclic branching, human-in-the-loop interrupts, or shared
  mutable agent state - i.e. at Nova scale, not at this POC's scope.
- **Model-agnostic LLM layer.** Agents call `vision_json` / `text_json` and never
  touch a vendor SDK; provider is an env switch (`azure` | `openai`). Avoids vendor
  lock-in and lets us route by cost/latency later.
- **Structured output: used for every LLM call** (JSON mode) - extraction,
  validation semantics, routing, NL→SQL. The whole system runs on typed contracts.
  **Where we avoid it:** the actual validation *verdict* and routing *decision* are
  NOT produced by the LLM at all - they are deterministic Python over the structured
  data. The LLM only judges fuzzy semantic equality and writes prose. Function/tool
  calling is deliberately not used: there are no external tools to call mid-reason;
  the "tools" are our own deterministic functions, invoked by the orchestrator.

---

## 6. Trust, Failure Handling & Evals

- **Reducing hallucinated fields (evidence, not proof):** the `source_quote`
  contract. The model must return the text it read each value from; no quote ⇒ the
  value is *ungrounded* and the validator forces it to `uncertain`. The extractor is
  also told that `value=null` ("not present") is a correct, rewarded answer -
  removing the pressure to invent a value to fill a blank. **Honest limitation:** a
  model can fabricate a value *and* a plausible quote together (we observed gpt-4o
  read an ambiguous `8471.3O` as `8471.30` and report it as a verbatim quote). The
  quote is therefore *evidence*, not proof - we do not yet verify it against an
  independent OCR pass / bounding boxes. The real backstops are the **deterministic
  rules** (which reject a wrong value regardless of confidence) and the
  escalate-on-doubt policy below. **Confidence is treated as a routing signal, not
  truth** - self-reported confidence from current models is poorly calibrated (in
  testing it clustered at ~0.95 even on a degraded scan).
- **Low-confidence / wrong-unit / missing values:** not silently approved. Pre-checks
  force `uncertain` before any rule runs (missing value, ungrounded value, confidence
  below the customer threshold), and rule evaluation adds unit-safety (a weight in the
  wrong unit, e.g. `lb` where `kg` is expected, is a mismatch - never a silent pass)
  and inconclusive-semantic escalation (a malformed LLM verdict becomes `uncertain`,
  not a false mismatch). Uncertain anywhere ⇒ the router sends the document to human
  review. The design goal is that no low-confidence, missing, wrong-unit, or
  ungrounded field is ever silently approved.
- **Stopping loops / runaway cost / infinite retries:** all enforced on the
  orchestrator, not the agents - bounded per-step retries (`MAX_RETRIES_PER_STEP`),
  and a **hard per-document USD budget cap** (`RUN_BUDGET_USD`) checked before and
  after every LLM call; exceeding it aborts the run to `failed` rather than burning
  budget. The pipeline is a finite DAG, so there is no agent loop to run away.
- **Evals:**
  - **Offline (regression):** *seeded today* - a trust-invariant suite
    (`backend/tests/`, 13 tests) asserts the safety properties with the LLM faked:
    wrong units never auto-approve, missing/ungrounded values become uncertain,
    malformed/stringy semantic verdicts become uncertain (not a silent match),
    provider failure preserves deterministic routing, all three outcomes are
    reachable, unknown customers are rejected. **Next:** extend to a labelled set of
    ~30-50 real documents with gold field values and expected outcomes, scoring
    per-field **extraction accuracy**, **confidence calibration**, end-to-end
    **routing accuracy**, and above all the **false-auto-approve rate** (the worst
    error class) as a release gate. Run on every prompt/model/rule change.
  - **Online (production):** **human-override rate** - of documents the system
    auto-approved or auto-routed, how often did a human disagree? Rising override
    rate is the early-warning signal that extraction quality or rules have drifted.
    Paired with cost-per-document and p95 latency dashboards.

---

## 7. Metrics & Success Criteria

**North-star:** *Percentage of documents correctly cleared without human touch*
(auto-approved **and** later confirmed correct). One number that captures the whole
value proposition - automation that is also trustworthy.

**Supporting metrics**
- *Agent quality:* per-field extraction accuracy; confidence calibration (ECE);
  routing accuracy vs gold labels.
- *Trust:* **false-auto-approve rate** (must trend to ~0); human-override rate.
- *System health:* p95 end-to-end latency; cost per document; pipeline success rate
  (non-`failed` runs).
- *Business outcome:* operator documents/hour; amendment-cycle count (target: 4→1);
  customs holds attributable to doc errors.

**Go / No-Go for a 2-week single-customer pilot**
- **Go** if: false-auto-approve rate < 1%; ≥ 60% of documents auto-cleared
  correctly; cost/document under target; operator reports net time saved; amendment
  drafts usable without rewrite ≥ 80% of the time.
- **No-Go** if: any silent wrong approval reaches customs; auto-clear rate so low
  the operator double-checks everything anyway (no time saved); cost/doc exceeds the
  manual baseline.

---

## 8. What's Next (after Part 1)

If I had two more weeks, in priority order:

1. **Confidence-triggered escalation + the real exception loop (Part 2 territory).**
   Wire the amendment draft into an actual supplier email round-trip with state
   tracking ("which shipments are waiting on the supplier, in which cycle"). This is
   where the largest human cost lives - the 4-cycle loop - and where outcome impact
   is highest.
2. **A labelled eval set + CI gate.** Make the offline eval above real and block
   any prompt/rule change that raises false-auto-approve rate. Trust is the product;
   it needs a regression test.
