"use client";

import { useEffect, useState } from "react";
import { startRun, getRun, getCustomers, askData } from "./api";

const STATUS_STEPS = ["queued", "extracting", "validating", "routing", "stored"];

const FIELD_ORDER = [
  "doc_type", "consignee_name", "hs_code", "port_of_loading", "port_of_discharge",
  "incoterms", "description_of_goods", "gross_weight", "invoice_number",
];

function confColor(c) {
  if (c >= 0.75) return "var(--green)";
  if (c >= 0.5) return "var(--amber)";
  return "var(--red)";
}

function Badge({ value }) {
  return <span className={`badge ${value}`}>{value.replace(/_/g, " ")}</span>;
}

export default function Home() {
  const [file, setFile] = useState(null);
  const [customers, setCustomers] = useState([]);
  const [customer, setCustomer] = useState("");
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  const [question, setQuestion] = useState("How many shipments were flagged for human review?");
  const [qres, setQres] = useState(null);
  const [qloading, setQloading] = useState(false);

  useEffect(() => {
    getCustomers().then((d) => {
      setCustomers(d.customers || []);
      if (d.customers?.length) setCustomer(d.customers[0]);
    });
  }, []);

  async function onRun() {
    if (!file || !customer) return;
    setLoading(true); setErr(null); setData(null);
    try {
      const { run_id } = await startRun(file, customer);
      // Poll until the pipeline reaches a terminal state, rendering partials live.
      for (;;) {
        const res = await getRun(run_id);
        setData(res);
        const s = res.run.status;
        if (s === "stored" || s === "failed") break;
        await new Promise((r) => setTimeout(r, 900));
      }
    } catch (e) { setErr(e.message); }
    finally { setLoading(false); }
  }

  async function onAsk() {
    setQloading(true); setQres(null);
    try { setQres(await askData(question)); }
    catch (e) { setQres({ error: e.message }); }
    finally { setQloading(false); }
  }

  const run = data?.run;
  const extracted = run?.extracted;
  const validation = run?.validation;
  const decision = run?.decision;

  return (
    <div className="wrap">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h1>Nova · Trade-Doc Pipeline</h1>
          <p className="sub">Extractor → Validator → Router. One engine, per-customer config.</p>
        </div>
        <a href="/inbox" className="small">CG review queue (Part 2) →</a>
      </div>

      <div className="card">
        <h2>Run a document</h2>
        <div className="row">
          <input type="file" accept=".pdf,.png,.jpg,.jpeg,.webp"
                 onChange={(e) => setFile(e.target.files[0])} />
          <select value={customer} onChange={(e) => setCustomer(e.target.value)}
                  style={{ padding: "9px 12px", borderRadius: 8, border: "1px solid var(--border)" }}>
            {customers.length === 0 && <option value="">no customers configured</option>}
            {customers.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <button onClick={onRun} disabled={!file || !customer || loading}>
            {loading ? "Running…" : "Run pipeline"}
          </button>
        </div>
        {loading && data?.run && (
          <div className="row" style={{ marginTop: 14, gap: 6 }}>
            {STATUS_STEPS.map((s) => {
              const cur = data.run.status;
              const reached = STATUS_STEPS.indexOf(cur) >= STATUS_STEPS.indexOf(s);
              const active = cur === s;
              return (
                <span key={s} className="small"
                      style={{ padding: "3px 10px", borderRadius: 999,
                               background: reached ? "var(--green-bg)" : "#eef1f6",
                               color: reached ? "var(--green)" : "var(--muted)",
                               fontWeight: active ? 700 : 500 }}>
                  {active ? `▸ ${s}…` : s}
                </span>
              );
            })}
          </div>
        )}
        {err && <p className="err" style={{ marginTop: 12 }}>{err}</p>}
      </div>

      {run && (
        <>
          {/* Decision banner */}
          <div className="card">
            <h2>Decision</h2>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div className="row">
                <Badge value={decision?.outcome || run.status} />
                <span className="muted small">run {run.run_id.slice(0, 8)} · {run.filename}</span>
              </div>
              <div className="muted small">
                cost ${Number(data.cost_usd).toFixed(4)} ·{" "}
                {data.ledger?.reduce((a, l) => a + (l.duration_ms || 0), 0)} ms
              </div>
            </div>
            <p style={{ marginTop: 12 }}>{decision?.reasoning}</p>
            {decision?.amendment_draft && (
              <>
                <h2 style={{ marginTop: 16 }}>Drafted amendment request</h2>
                <pre className="draft">{decision.amendment_draft}</pre>
              </>
            )}
            {run.error && <p className="err">{run.error}</p>}
          </div>

          <div className="grid">
            {/* Extracted fields */}
            <div className="card">
              <h2>Extracted fields + confidence</h2>
              {extracted && FIELD_ORDER.map((k) => {
                const f = extracted[k];
                if (!f) return null;
                return (
                  <div className="field" key={k}>
                    <div className="k">{k.replace(/_/g, " ")}</div>
                    <div className="v">{f.value ?? <span className="muted">— not found —</span>}</div>
                    {f.source_quote
                      ? <div className="q">“{f.source_quote}”</div>
                      : <div className="q" style={{ color: "var(--red)" }}>ungrounded — no source quote</div>}
                    <div className="confbar">
                      <div style={{ width: `${Math.round(f.confidence * 100)}%`, background: confColor(f.confidence) }} />
                    </div>
                    <div className="small muted">confidence {(f.confidence * 100).toFixed(0)}%</div>
                  </div>
                );
              })}
            </div>

            {/* Validation */}
            <div className="card">
              <h2>Validation vs {customer} rules</h2>
              <table>
                <thead>
                  <tr><th>Field</th><th>Status</th><th>Found / Expected</th></tr>
                </thead>
                <tbody>
                  {validation?.results.map((r) => (
                    <tr key={r.field}>
                      <td>{r.field.replace(/_/g, " ")}</td>
                      <td><Badge value={r.status} /></td>
                      <td>
                        <div>{r.found ?? "—"}</div>
                        {r.status !== "match" && <div className="muted small">exp: {r.expected}</div>}
                        <div className="reason small">{r.reason}</div>
                        <div className="small" style={{ color: "var(--purple)" }}>{r.method}</div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Trace / cost ledger */}
          <div className="card">
            <h2>Trace + cost ledger (run_id {run.run_id.slice(0, 8)})</h2>
            <table className="ledger">
              <thead>
                <tr><th>Step</th><th>Model</th><th>Tokens (in/out)</th><th>Cost</th><th>Latency</th><th>Detail</th></tr>
              </thead>
              <tbody>
                {data.ledger?.map((l, i) => (
                  <tr key={i}>
                    <td>{l.step}</td>
                    <td>{l.model || "—"}</td>
                    <td>{l.prompt_tokens}/{l.output_tokens}</td>
                    <td>${Number(l.cost_usd).toFixed(4)}</td>
                    <td>{l.duration_ms} ms</td>
                    <td className="muted">{l.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {/* Ask your data */}
      <div className="card">
        <h2>Ask your data (NL → SQL, read-only)</h2>
        <div className="row">
          <input type="text" value={question} onChange={(e) => setQuestion(e.target.value)}
                 onKeyDown={(e) => e.key === "Enter" && onAsk()} />
          <button className="ghost" onClick={onAsk} disabled={qloading}>
            {qloading ? "Asking…" : "Ask"}
          </button>
        </div>
        {qres && (
          <div>
            {qres.error
              ? <p className="err" style={{ marginTop: 10 }}>{qres.error}</p>
              : <>
                  <div className="answer">{qres.answer}</div>
                  {qres.sql && <code className="sql">{qres.sql}</code>}
                  <div className="small muted" style={{ marginTop: 6 }}>
                    {qres.rows?.length ?? 0} row(s) returned
                  </div>
                </>}
          </div>
        )}
      </div>
    </div>
  );
}
