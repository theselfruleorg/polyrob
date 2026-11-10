/**
 * money-usage.js — Money › Cash › AI cost (113 W6, G-3).
 *
 * Reads `GET /api/webgate/usage?days=N`: the `/usage` verb's view
 * (`core.status_economics.spend_view`) and its ONE render (`spend_view_lines`),
 * so the console prints the lines Telegram and the terminal print. A period
 * picker (24 hours, 7 days, 30 days) re-reads. An unreadable record is its own
 * entry with the reason behind a disclosure, never a zero. It reads when the
 * Cash pane is first shown. Words come from `#money-usage-copy`.
 */
import { stateBlock } from "./state.js";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

export const PERIODS = [1, 7, 30];

/** Draw one answer of the reader into `root`. Returns the state drawn. */
export function renderUsage(root, body, copy, days, pick) {
  root.replaceChildren();
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", copy.title || ""));
  const chips = el("div", "chips");
  chips.setAttribute("role", "group");
  chips.setAttribute("aria-label", copy.period || "");
  PERIODS.forEach((n) => {
    const chip = el("button", "chip", copy[`days_${n}`] || String(n));
    chip.type = "button";
    chip.setAttribute("aria-pressed", String(n === days));
    chip.addEventListener("click", () => pick(n));
    chips.appendChild(chip);
  });
  head.appendChild(chips);
  root.appendChild(head);
  if (!body || body.readable !== true) {
    root.appendChild(stateBlock({ body: copy.unreadable, why: copy.why,
      reason: (body && body.error) || "" }));
    return "unreadable";
  }
  const lines = Array.isArray(body.lines) ? body.lines : [];
  const list = el("div", "entry usage-lines");
  lines.forEach((line) => list.appendChild(el("p", "entry-meta", line)));
  root.appendChild(list);
  return body.no_ledger ? "empty" : "usage";
}

export async function loadUsage(root, copy, days = 7, fetcher = fetch) {
  const pick = (n) => loadUsage(root, copy, n, fetcher);
  let body = null;
  try {
    const resp = await fetcher(`/api/webgate/usage?days=${days}`, { credentials: "include" });
    body = await resp.json().catch(() => null);
    if (!resp.ok) body = { readable: false, error: (body && (body.detail || body.error)) || String(resp.status) };
  } catch (err) {
    console.error("[money-usage] the AI cost read did not reach the console", err);
    body = { readable: false, error: String((err && err.message) || err) };
  }
  return renderUsage(root, body, copy, days, pick);
}

export function mount(doc = document) {
  const root = doc.getElementById("money-usage");
  const pane = root && root.closest("[data-pane]");
  if (!root || !pane) return null;
  const copy = { ...(doc.getElementById("money-usage-copy")?.dataset || {}) };
  let read = false;
  const go = () => {
    if (read || pane.hidden) return;
    read = true;
    root.replaceChildren(el("p", "state-body", copy.loading || ""));
    loadUsage(root, copy);
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
