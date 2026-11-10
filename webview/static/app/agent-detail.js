/**
 * agent-detail.js — two read-only views that gave a caller to a reader that had
 * none (113 W5, C-14):
 *
 *  - Agent › Abilities: a skill row opens its own text (`knowledge/skill/{id}`),
 *    read only when the person opens it.
 *  - Agent › Memory: "What the agent did", the episode ledger
 *    (`knowledge/episodes`): day, kind, task, outcome, summary.
 *
 * Honest states: a read that failed is named (`items: null` / `body: null`), a
 * store that holds nothing says so, and a skill that is gone says that.
 * Every word comes from `#agent-copy` (`copy`); this file holds none.
 */
import { getJson } from "./http.js";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

const DASH = "—";

/** The skill's text behind a disclosure; fetched on the first open. */
export function skillDetail(skillId, copy, opts = {}) {
  const details = el("details", "skill-detail");
  details.appendChild(el("summary", null, (copy && copy.cap_skill_read) || ""));
  const body = el("div");
  details.appendChild(body);
  let loaded = false;
  details.addEventListener("toggle", async () => {
    if (!details.open || loaded) return;
    loaded = true;
    body.replaceChildren(el("p", "entry-meta", (copy && copy.cap_loading) || ""));
    await fillSkill(body, skillId, copy, opts.fetcher);
  });
  return details;
}

/** Fill *body* with one skill's text. Exported for tests. Returns the state. */
export async function fillSkill(body, skillId, copy, fetcher) {
  let res;
  try {
    res = await getJson(`/api/webgate/knowledge/skill/${encodeURIComponent(skillId)}`, { fetcher });
  } catch (err) {
    res = { ok: false, status: 0, body: null };
  }
  const data = res.body || {};
  body.replaceChildren();
  if (res.status === 404) {
    body.appendChild(el("p", "entry-meta", (copy && copy.cap_skill_gone) || ""));
    return "gone";
  }
  if (!res.ok || data.body === null || data.error) {
    const line = el("p", "entry-meta", (copy && copy.cap_skill_unreadable) || "");
    body.appendChild(line);
    if (data.error) body.appendChild(el("p", "unknown-why", String(data.error)));
    return "unreadable";
  }
  body.appendChild(el("pre", "skill-text", String(data.body || "")));
  return "text";
}

/** Draw "What the agent did" from `knowledge/episodes`. Returns the state. */
export function renderEpisodes(root, data, copy) {
  root.replaceChildren();
  const read = Boolean(data) && Array.isArray(data.items);
  const items = read ? data.items : [];
  const sec = el("div", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", (copy && copy.mem_ep_title) || ""));
  head.appendChild(el("span", "section-aside",
    String((copy && copy.mem_ep_aside) || "").replace("{count}", read ? String(items.length) : DASH)));
  sec.appendChild(head);
  root.appendChild(sec);
  if (!read || data.error) {
    const entry = el("div", "entry is-unknown");
    entry.appendChild(el("h3", "entry-title", (copy && copy.mem_ep_unreadable) || ""));
    const why = (data && (data.error || data.reason)) || "";
    if (why) entry.appendChild(el("p", "entry-meta", String(why)));
    sec.appendChild(entry);
    return "unreadable";
  }
  if (!items.length) {
    const entry = el("div", "entry");   // the same notice the other Memory lists draw
    entry.appendChild(el("p", "entry-body", data.reason || (copy && copy.mem_ep_empty) || ""));
    sec.appendChild(entry);
    return "empty";
  }
  const table = el("table", "table");
  const tbody = el("tbody");
  items.forEach((ep) => {
    const tr = el("tr");
    const what = el("td", "what");
    what.dataset.label = (copy && copy.mem_ep_col_what) || "";
    what.appendChild(document.createTextNode(String(ep.task || ep.summary || DASH)));
    if (ep.summary && ep.task) what.appendChild(el("span", "why", String(ep.summary)));
    tr.appendChild(what);
    const outcome = el("td");
    outcome.dataset.label = (copy && copy.mem_ep_col_outcome) || "";
    outcome.textContent = String(ep.outcome || DASH);
    tr.appendChild(outcome);
    const day = el("td", "val");
    day.dataset.label = (copy && copy.mem_ep_col_day) || "";
    day.textContent = String(ep.day || DASH);
    tr.appendChild(day);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  sec.appendChild(table);
  return "episodes";
}

/** The episode ledger, newest first, for the Memory tab. */
export async function loadEpisodes(fetcher) {
  const res = await getJson("/api/webgate/knowledge/episodes?limit=20", { fetcher });
  return res.ok ? res.body : { items: null, error: String((res.body && res.body.error) || res.status) };
}
