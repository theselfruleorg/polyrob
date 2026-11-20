/**
 * money-standing.js — Money › Limits › Standing rules.
 *
 * Reads `GET /api/webgate/standing`: the status snapshot's `income_rule`
 * and `mandates` sections (the ONE section registration,
 * core/status_sections.py), and draws each the way the console draws a status
 * section — its title, then its own lines. A section that could not be read is
 * its own entry with the reason behind a disclosure, never "none"; a health
 * item is drawn as needing you. It reads when the Limits pane is first shown.
 * Words come from `#money-standing-copy`; the lines are the section's own.
 */
import { stateBlock } from "./state.js";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

/** One section: title, then its lines (or the unreadable entry), then its health. */
export function sectionBlock(section, copy) {
  const box = el("div", "entry");
  box.dataset.section = String((section && section.name) || "");
  box.appendChild(el("h3", "entry-title", copy[section && section.name] || (section && section.name) || ""));
  if (!section || section.state === "unavailable") {
    box.dataset.state = "unavailable";
    box.appendChild(stateBlock({ body: copy.unreadable, why: copy.why,
      reason: (section && section.reason) || "" }));
    return box;
  }
  box.dataset.state = String(section.state || "ok");
  for (const line of section.lines || []) box.appendChild(el("p", "entry-meta", line));
  for (const item of section.health || []) {
    box.appendChild(el("p", "entry-meta is-needs-you",
      item.remedy ? `${item.text} (${item.remedy})` : item.text));
  }
  return box;
}

/** Draw one answer of the reader into `root`. Returns the state drawn. */
export function renderStanding(root, body, copy) {
  root.replaceChildren();
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", copy.title || ""));
  head.appendChild(el("span", "section-aside", copy.aside || ""));
  root.appendChild(head);
  if (!body || body.error || !Array.isArray(body.sections)) {
    root.appendChild(stateBlock({ body: copy.unreadable, why: copy.why,
      reason: (body && (body.detail || body.error)) || "" }));
    return "unreadable";
  }
  const ledger = el("div", "ledger");
  body.sections.forEach((section) => ledger.appendChild(sectionBlock(section, copy)));
  root.appendChild(ledger);
  return "standing";
}

export async function loadStanding(root, copy, fetcher = fetch) {
  let body = null;
  try {
    const resp = await fetcher("/api/webgate/standing", { credentials: "include" });
    body = await resp.json().catch(() => null);
    if (!resp.ok) body = { error: (body && (body.detail || body.error)) || String(resp.status) };
  } catch (err) {
    console.error("[money-standing] the standing rules read did not reach the console", err);
    body = { error: String((err && err.message) || err) };
  }
  return renderStanding(root, body, copy);
}

export function mount(doc = document) {
  const root = doc.getElementById("money-standing");
  const pane = root && root.closest("[data-pane]");
  if (!root || !pane) return null;
  const copy = { ...(doc.getElementById("money-standing-copy")?.dataset || {}) };
  let read = false;
  const go = () => {
    if (read || pane.hidden) return;
    read = true;
    root.replaceChildren(el("p", "state-body", copy.loading || ""));
    loadStanding(root, copy);
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
