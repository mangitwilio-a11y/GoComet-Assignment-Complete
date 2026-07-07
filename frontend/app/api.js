export const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

// Kicks off a run; returns { run_id, status } immediately (pipeline runs in background).
export async function startRun(file, customer) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("customer", customer);
  const res = await fetch(`${API}/api/runs`, { method: "POST", body: fd });
  if (!res.ok) throw new Error((await res.json()).detail || "run failed");
  return res.json();
}

// Poll a run's current state: { run, ledger, cost_usd }.
export async function getRun(runId) {
  const res = await fetch(`${API}/api/runs/${runId}`);
  if (!res.ok) throw new Error("could not fetch run");
  return res.json();
}

export async function getCustomers() {
  const res = await fetch(`${API}/api/customers`);
  if (!res.ok) return { customers: [] };
  return res.json();
}

// ---- Part 2: shipments (SU email -> CG review queue) ----

export async function getShipments() {
  const res = await fetch(`${API}/api/shipments`);
  if (!res.ok) return { shipments: [], pending_review: 0 };
  return res.json();
}

// Full shipment detail: { shipment, runs, shipment_ledger, cost_usd }.
export async function getShipment(id) {
  const res = await fetch(`${API}/api/shipments/${id}`);
  if (!res.ok) throw new Error("could not fetch shipment");
  return res.json();
}

// CG clicks send — the only path to 'sent'. The agent never calls this.
export async function sendReply(id, draft) {
  const res = await fetch(`${API}/api/shipments/${id}/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ draft }),
  });
  if (!res.ok) throw new Error((await res.json()).detail || "send failed");
  return res.json();
}

// Demo helper: drop a sample SU email into the watched inbox folder.
export async function simulateEmail(sample) {
  const res = await fetch(`${API}/api/simulate-email`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sample }),
  });
  if (!res.ok) throw new Error((await res.json()).detail || "simulate failed");
  return res.json();
}

export async function askData(question) {
  const res = await fetch(`${API}/api/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!res.ok) throw new Error((await res.json()).detail || "query failed");
  return res.json();
}
