"use client";

/*
 * CG verification screen (Part 2).
 *
 * Four states, matching how a CG operator actually works an email:
 *   1. Incoming      — SU email arrived, agent is processing (live stepper)
 *   2. Verification  — per-document field checks + cross-document consistency
 *   3. Discrepancy   — click any flagged field: found vs expected + source snippet
 *   4. Draft reply   — the agent's email to SU, editable; CG clicks send.
 *
 * The agent never sends. `sent` is reachable only through the button here.
 */

import { useEffect, useRef, useState } from "react";
import { getShipment, getShipments, sendReply, simulateEmail } from "../api";

const SHIP_STEPS = ["received", "processing", "cross_validating", "drafting", "pending_review", "sent"];
const STEP_LABEL = {
  received: "received", processing: "reading docs", cross_validating: "cross-checking",
  drafting: "drafting reply", pending_review: "awaiting you", sent: "sent",
};

function Badge({ value }) {
  return <span className={`badge ${value}`}>{String(value).replace(/_/g, " ")}</span>;
}

function confColor(c) {
  if (c >= 0.75) return "var(--green)";
  if (c >= 0.5) return "var(--amber)";
  return "var(--red)";
}

function ConfCell({ value }) {
  const c = value ?? 0;
  return (
    <td style={{ width: 110 }}>
      <div className="confbar"><div style={{ width: `${Math.round(c * 100)}%`, background: confColor(c) }} /></div>
      <span className="small muted">{(c * 100).toFixed(0)}%</span>
    </td>
  );
}

function timeAgo(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

export default function InboxPage() {
  const [list, setList] = useState([]);
  const [pending, setPending] = useState(0);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [focus, setFocus] = useState(null); // {kind:'cross'|'doc', field, runId?}
  const [draft, setDraft] = useState("");
  const draftEdited = useRef(false);
  const [sending, setSending] = useState(false);
  const [err, setErr] = useState(null);

  // Poll the queue — new SU emails appear without any user action (the trigger).
  useEffect(() => {
    let live = true;
    const tick = async () => {
      try {
        const d = await getShipments();
        if (!live) return;
        setList(d.shipments || []);
        setPending(d.pending_review || 0);
      } catch { /* backend not up yet */ }
    };
    tick();
    const t = setInterval(tick, 2500);
    return () => { live = false; clearInterval(t); };
  }, []);

  // Poll the selected shipment while the agent is still working on it.
  useEffect(() => {
    if (!selectedId) return;
    let live = true;
    draftEdited.current = false;
    setDetail(null); setFocus(null); setErr(null);
    const tick = async () => {
      try {
        const d = await getShipment(selectedId);
        if (!live) return;
        setDetail(d);
        if (!draftEdited.current) {
          setDraft(d.shipment.draft_final ?? d.shipment.decision?.draft ?? "");
        }
      } catch (e) { if (live) setErr(e.message); }
    };
    tick();
    const t = setInterval(tick, 1500);
    return () => { live = false; clearInterval(t); };
  }, [selectedId]);

  async function onSimulate(sample) {
    setErr(null);
    try { await simulateEmail(sample); }
    catch (e) { setErr(e.message); }
  }

  async function onSend() {
    setSending(true); setErr(null);
    try {
      const d = await sendReply(selectedId, draft);
      setDetail((prev) => ({ ...prev, shipment: d.shipment }));
    } catch (e) { setErr(e.message); }
    finally { setSending(false); }
  }

  const ship = detail?.shipment;
  const runs = detail?.runs || [];
  const working = ship && !["pending_review", "sent", "failed"].includes(ship.status);

  return (
    <div className="wrap wide">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h1>Nova · CG Review Queue</h1>
          <p className="sub">SU email → agent verification → your review → your send. Nothing leaves without you.</p>
        </div>
        <a href="/" className="small">← single-document screen</a>
      </div>

      <div className="inbox-grid">
        {/* ------------------------------ queue ------------------------------ */}
        <div>
          <div className="card">
            <h2>Simulate SU email</h2>
            <div className="row">
              <button className="ghost" onClick={() => onSimulate("clean_shipment")}>📥 Clean shipment</button>
              <button className="ghost" onClick={() => onSimulate("messy_shipment")}>📥 Messy shipment</button>
            </div>
            <p className="small muted" style={{ marginTop: 8 }}>
              Drops a sample email into the watched <code>inbox/</code> folder — the agent picks it up itself.
            </p>
          </div>

          <div className="card">
            <h2>Inbox {pending > 0 && <span className="badge human_review">{pending} awaiting review</span>}</h2>
            {list.length === 0 && <p className="muted small">No shipments yet — simulate an SU email.</p>}
            {list.map((s) => (
              <div key={s.shipment_id}
                   className={`mailitem ${s.shipment_id === selectedId ? "selected" : ""}`}
                   onClick={() => setSelectedId(s.shipment_id)}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <strong className="small">{s.subject}</strong>
                  <Badge value={s.status === "pending_review" && s.decision ? s.decision.outcome : s.status} />
                </div>
                <div className="small muted">{s.from_addr} · {timeAgo(s.received_at)}</div>
              </div>
            ))}
          </div>
        </div>

        {/* ----------------------------- detail ------------------------------ */}
        <div>
          {!ship && (
            <div className="card"><p className="muted">Select a shipment from the inbox.</p></div>
          )}

          {ship && (
            <>
              {/* State 1: incoming / processing */}
              <div className="card">
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <div>
                    <strong>{ship.subject}</strong>
                    <div className="small muted">from {ship.from_addr} · customer {ship.customer}</div>
                  </div>
                  <div className="small muted">
                    cost ${Number(detail.cost_usd).toFixed(4)} · {runs.length} doc(s)
                  </div>
                </div>
                <div className="row" style={{ marginTop: 12, gap: 6 }}>
                  {SHIP_STEPS.map((s) => {
                    const cur = ship.status;
                    const reached = SHIP_STEPS.indexOf(cur) >= SHIP_STEPS.indexOf(s);
                    const active = cur === s;
                    return (
                      <span key={s} className="small step"
                            style={{
                              background: reached ? "var(--green-bg)" : "#eef1f6",
                              color: reached ? "var(--green)" : "var(--muted)",
                              fontWeight: active ? 700 : 500,
                            }}>
                        {active && working ? `▸ ${STEP_LABEL[s]}…` : STEP_LABEL[s]}
                      </span>
                    );
                  })}
                </div>
                {ship.status === "failed" && <p className="err" style={{ marginTop: 10 }}>{ship.error}</p>}
                {ship.decision && (
                  <div style={{ marginTop: 12 }}>
                    <Badge value={ship.decision.outcome} />
                    <p style={{ margin: "8px 0 0" }}>{ship.decision.reasoning}</p>
                  </div>
                )}
              </div>

              {/* State 2a: per-document verification */}
              {runs.length > 0 && (
                <div className="card">
                  <h2>Documents vs {ship.customer} rules</h2>
                  {runs.map((r) => (
                    <details key={r.run_id} className="docblock" open={r.validation?.has_mismatch || r.validation?.has_uncertain}>
                      <summary>
                        <span className="row" style={{ display: "inline-flex", gap: 8 }}>
                          <strong className="small">{r.filename}</strong>
                          <span className="small muted">{r.extracted?.doc_type?.value || ""}</span>
                          <Badge value={r.decision?.outcome || r.status} />
                          {r.validation && <span className="small muted">{summaryOf(r.validation)}</span>}
                        </span>
                      </summary>
                      {r.error && <p className="err small">{r.error}</p>}
                      {r.validation && (
                        <table>
                          <thead>
                            <tr><th>Field</th><th>Status</th><th>Confidence</th><th>Found</th></tr>
                          </thead>
                          <tbody>
                            {r.validation.results.map((v) => (
                              <tr key={v.field}
                                  className={v.status !== "match" ? "clickable" : ""}
                                  onClick={() => v.status !== "match" && setFocus({ kind: "doc", field: v.field, runId: r.run_id })}>
                                <td style={{ width: 160 }}>{v.field.replace(/_/g, " ")}</td>
                                <td style={{ width: 110 }}><Badge value={v.status} /></td>
                                <ConfCell value={v.confidence} />
                                <td className="small">{v.found ?? <span className="muted">— not found —</span>}
                                  {v.status !== "match" && <span className="muted"> · click for detail</span>}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                    </details>
                  ))}
                </div>
              )}

              {/* State 2b: cross-document consistency */}
              {ship.cross_validation && (
                <div className="card">
                  <h2>Cross-document consistency</h2>
                  <table>
                    <thead>
                      <tr><th>Field</th><th>Status</th><th>What each document says</th></tr>
                    </thead>
                    <tbody>
                      {ship.cross_validation.checks.map((c) => (
                        <tr key={c.field}
                            className={c.status !== "consistent" ? "clickable" : ""}
                            onClick={() => c.status !== "consistent" && setFocus({ kind: "cross", field: c.field })}>
                          <td>{c.field.replace(/_/g, " ")}</td>
                          <td><Badge value={c.status} /></td>
                          <td className="small">
                            {c.values.filter((v) => v.value != null).map((v) => (
                              <span key={v.run_id} className={`chip ${c.status}`}>{v.filename}: {v.value}</span>
                            ))}
                            {c.status !== "consistent" && <span className="muted"> · click for detail</span>}
                          </td>
                        </tr>
                      ))}
                      {ship.cross_validation.checks.length === 0 && (
                        <tr><td colSpan={3} className="muted small">No field appeared in two or more documents.</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              )}

              {/* State 3: discrepancy detail */}
              {focus && <FocusPanel focus={focus} ship={ship} runs={runs} onClose={() => setFocus(null)} />}

              {/* State 4: draft reply — CG edits, CG sends */}
              {ship.decision && (
                <div className="card">
                  <h2>
                    Draft reply to SU{" "}
                    {ship.status === "sent"
                      ? <span className="badge stored">sent {timeAgo(ship.sent_at)}</span>
                      : <span className="badge human_review">not sent — awaiting your review</span>}
                  </h2>
                  {ship.status === "sent" ? (
                    <>
                      <pre className="draft">{ship.draft_final}</pre>
                      {ship.draft_final !== ship.decision.draft && (
                        <p className="small muted">Edited by CG before sending (agent draft differed).</p>
                      )}
                    </>
                  ) : (
                    <>
                      <textarea
                        className="drafteditor"
                        value={draft}
                        rows={13}
                        onChange={(e) => { draftEdited.current = true; setDraft(e.target.value); }}
                      />
                      <div className="row" style={{ marginTop: 10, justifyContent: "space-between" }}>
                        <span className="small muted">
                          The agent never sends on its own — this button is the only way out.
                        </span>
                        <button onClick={onSend} disabled={sending || ship.status !== "pending_review"}>
                          {sending ? "Sending…" : "Approve & send to SU"}
                        </button>
                      </div>
                    </>
                  )}
                </div>
              )}

              {err && <p className="err">{err}</p>}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function summaryOf(validation) {
  const n = (s) => validation.results.filter((r) => r.status === s).length;
  return `${n("match")} match / ${n("mismatch")} mismatch / ${n("uncertain")} uncertain`;
}

/* State 3 panel: what was found vs expected, with the verbatim source snippet
 * from each document — the evidence a CG operator checks before trusting it. */
function FocusPanel({ focus, ship, runs, onClose }) {
  let title, rows, reason;
  if (focus.kind === "cross") {
    const check = ship.cross_validation?.checks.find((c) => c.field === focus.field);
    if (!check) return null;
    title = `${focus.field.replace(/_/g, " ")} — across documents`;
    reason = check.reason;
    rows = check.values.map((v) => ({
      key: v.run_id, label: `${v.filename}${v.doc_type ? ` (${v.doc_type})` : ""}`,
      found: v.value, expected: null, quote: v.source_quote, confidence: v.confidence,
    }));
  } else {
    const run = runs.find((r) => r.run_id === focus.runId);
    const v = run?.validation?.results.find((x) => x.field === focus.field);
    if (!run || !v) return null;
    const ef = run.extracted?.[focus.field];
    title = `${focus.field.replace(/_/g, " ")} — ${run.filename}`;
    reason = v.reason;
    rows = [{
      key: run.run_id, label: run.filename, found: v.found,
      expected: v.expected, quote: ef?.source_quote, confidence: v.confidence ?? ef?.confidence ?? 0,
    }];
  }
  return (
    <div className="card focuspanel">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 style={{ margin: 0 }}>Discrepancy detail · {title}</h2>
        <button className="ghost" onClick={onClose}>close</button>
      </div>
      <p className="small" style={{ marginTop: 10 }}>{reason}</p>
      {rows.map((r) => (
        <div key={r.key} className="field">
          <div className="k small">{r.label}</div>
          <div className="v">
            found: <strong>{r.found ?? "— not found —"}</strong>
            {r.expected != null && <> · expected: <strong>{r.expected}</strong></>}
            <span className="muted small"> · confidence {(r.confidence * 100).toFixed(0)}%</span>
          </div>
          {r.quote
            ? <div className="q">source snippet: “{r.quote}”</div>
            : <div className="q" style={{ color: "var(--red)" }}>ungrounded — no source snippet in the document</div>}
        </div>
      ))}
    </div>
  );
}
