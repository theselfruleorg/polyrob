/**
 * money-offers.js — Money › Invoices › offers and orders (113 W6, G-2).
 *
 * Reads `GET /api/webgate/offers`: the owner's offers (state, what each sold) and
 * the newest orders — the rows `/offer` and `/offer orders` print. A live offer has
 * Pause, a paused one Resume: `POST /api/webgate/offers/{id}/{pause|resume}` runs the
 * `/offer` line on the owner-verb plane, and its answer is shown verbatim. Setup, go
 * live, resolve and retire stay on the cards and the `/offer` verb. A store that will
 * not read is its own entry, never "no offers". Words come from `#money-offers-copy`.
 */
import { postJson } from "./http.js";
import { stateBlock } from "./state.js";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function fill(template, vars) {
  return String(template || "").replace(/\{(\w+)\}/g, (m, k) => (vars && vars[k] != null ? String(vars[k]) : m));
}

const PILL = { live: "is-running", paused: "is-needs-you", setup: "is-needs-you", draft: "is-done",
  retired: "is-stopped" };

function head(title) {
  const h = el("div", "section-head");
  h.appendChild(el("h2", "section-title", title || ""));
  return h;
}

/** Draw one read. `act(id, verb)` posts a change. Returns the state drawn. */
export function renderOffers(root, body, copy, act) {
  root.replaceChildren();
  root.appendChild(head(copy.title));
  if (!body || body.readable !== true) {
    root.appendChild(stateBlock({ body: copy.unreadable, why: copy.why, reason: (body && body.error) || "" }));
    return "unreadable";
  }
  const offers = Array.isArray(body.offers) ? body.offers : [];
  if (!offers.length) root.appendChild(stateBlock({ body: [copy.empty, copy.empty_how] }));
  const status = el("p", "why");
  status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
  offers.forEach((o) => {
    const entry = el("div", "entry");
    entry.dataset.offer = o.id;
    entry.appendChild(el("h3", "entry-title", o.title || o.id));
    const meta = el("p", "entry-meta");
    const pill = el("span", `pill ${PILL[o.state] || "is-unknown"}`);
    pill.appendChild(el("span", "dot"));
    pill.appendChild(document.createTextNode(copy[`state_${o.state}`] || o.state));
    meta.append(pill, document.createTextNode(" "), el("span", "val", fill(copy.sold, { count: o.sold })));
    entry.appendChild(meta);
    const verb = o.state === "live" ? "pause" : o.state === "paused" ? "resume" : "";
    if (verb && copy.can_write === "1" && typeof act === "function") {
      const button = el("button", "btn btn-quiet", copy[verb] || verb);
      button.type = "button";
      button.setAttribute("aria-label", `${copy[verb] || verb}: ${o.title || o.id}`);
      button.addEventListener("click", async () => {
        button.disabled = true;
        const out = await act(o.id, verb);
        status.textContent = out.message || copy.unreachable || "";
        button.disabled = false;
      });
      const row = el("div", "entry-actions");
      row.appendChild(button);
      entry.appendChild(row);
    }
    root.appendChild(entry);
  });
  if (offers.length) root.appendChild(el("p", "entry-meta", copy.decide_note || ""));
  root.appendChild(status);

  root.appendChild(head(copy.orders_title));
  const orders = Array.isArray(body.orders) ? body.orders : [];
  if (!orders.length) {
    root.appendChild(el("p", "state-body", copy.orders_empty || ""));
    return "offers";
  }
  const table = el("table", "table");
  const tbody = el("tbody");
  orders.forEach((r) => {
    const tr = el("tr");
    tr.dataset.order = r.id;
    tr.appendChild(el("td", "what", r.id));
    tr.appendChild(el("td", null, r.offer_id));
    tr.appendChild(el("td", null, copy[`order_${r.status}`] || r.status));
    tr.appendChild(el("td", "val", r.amount_usd != null ? `$${r.amount_usd}` : "—"));
    tr.appendChild(el("td", null, r.channel || ""));
    tbody.appendChild(tr);
    if (r.error) {
      const why = el("tr");
      const cell = el("td", "why", r.error);
      cell.colSpan = 5;
      why.appendChild(cell);
      tbody.appendChild(why);
    }
  });
  table.appendChild(tbody);
  root.appendChild(table);
  if (body.orders_truncated) root.appendChild(el("p", "entry-meta", copy.orders_more || ""));
  return "offers";
}

export async function act(id, verb, fetcher) {
  try {
    const res = await postJson(`/api/webgate/offers/${encodeURIComponent(id)}/${verb}`, {},
      fetcher ? { fetcher } : {});
    const body = res.body || {};
    return { ok: res.ok && body.ok === true, message: body.message || body.detail || "" };
  } catch (err) {
    console.error("[money-offers] the change did not reach the console", err);
    return { ok: false, message: "" };
  }
}

export async function loadOffers(root, copy, fetcher = fetch) {
  let body = null;
  try {
    const resp = await fetcher("/api/webgate/offers", { credentials: "include" });
    body = await resp.json().catch(() => null);
    if (!resp.ok) body = { readable: false, error: (body && (body.detail || body.error)) || String(resp.status) };
  } catch (err) {
    console.error("[money-offers] the offers read did not reach the console", err);
    body = { readable: false, error: String((err && err.message) || err) };
  }
  const post = fetcher === fetch ? undefined : fetcher;
  return renderOffers(root, body, copy, async (id, verb) => {
    const out = await act(id, verb, post);
    if (out.ok) await loadOffers(root, copy, fetcher).then(() => {
      const status = root.querySelector("[role=status]");
      if (status) status.textContent = out.message;
    });
    return out;
  });
}

export function mount(doc = document) {
  const root = doc.getElementById("money-offers");
  const pane = root && root.closest("[data-pane]");
  if (!root || !pane) return null;
  const copy = { ...(doc.getElementById("money-offers-copy")?.dataset || {}) };
  let read = false;
  const go = () => {
    if (read || pane.hidden) return;
    read = true;
    root.replaceChildren(el("p", "state-body", copy.loading || ""));
    loadOffers(root, copy);
  };
  if (typeof MutationObserver === "function") {
    new MutationObserver(go).observe(pane, { attributes: true, attributeFilter: ["hidden"] });
  }
  go();
  return root;
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => mount());
  else mount();
}
