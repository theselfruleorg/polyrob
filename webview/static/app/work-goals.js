/**
 * work-goals.js — Work › Goals, the goal board (2026-10-27).
 *
 * One lane per live status (`columns`, which the server sends from
 * core.goal_vocab.LIVE_STATUS_ORDER), the open decisions first, the finished
 * goals folded away under the board. A card opens the goal's detail in a
 * native <dialog> (#goal-detail).
 *
 *   GET  /api/webgate/goals/board?finished=N   the whole board (one read)
 *   GET  /api/webgate/goals/{id}/detail        one goal's record
 *   POST /api/webgate/goals/{id}/{verb}        pause | retry | stop | drop
 *   POST /api/webgate/inbox/ask/{id}/{decide|reject}   a decision (the Inbox's
 *        route — GoalBoard.decide_ask is the ONE writer of an answer)
 *   GET  /api/webgate/artifacts?session_id=    the files of a goal's chats
 *
 * The rules work-now.js keeps, kept here too:
 *
 * 1. Every word comes from the copy layer (#work-goals-copy's data attributes).
 * 2. An unreadable store is a dashed entry with its reason — never an empty
 *    board. A side read that failed (the edges, the history) is named and the
 *    rest still draws.
 * 3. It draws no verb it cannot act on (read-only draws none), and a lane is
 *    never a status write: there is no drag.
 * 4. It polls nothing. It redraws on `polyrob:activity` (live.js re-dispatches
 *    the socket's events) and on `polyrob:tick`, which live.js fires only while
 *    the socket is down — and then the board says it may be out of date.
 *
 * The filters (text, kind, source, show finished) and the open goal live in
 * the URL query (`?q=&kind=&source=&finished=1&goal=<id>`), so a reload or a
 * shared link shows the same board; the tab stays the hash (`#goals`).
 */
import { copyFrom, elapsed, format, noticeEntry, unreadableEntry } from "./work-now.js";
import { latestOnly, postJson } from "./http.js";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** Seconds since `ts`, or `null` when there is no time (never "since 1970"). */
function since(ts, nowMs) {
  const t = Number(ts);
  if (ts === null || ts === undefined || !Number.isFinite(t) || t <= 0) return null;
  return Math.max(0, nowMs / 1000 - t);
}

/** "added … ago", or nothing when the time is not known. */
function agoLine(key, ts, nowMs, copy) {
  const s = since(ts, nowMs);
  return s === null ? "" : format(copy[key], { elapsed: elapsed(s, copy) });
}

/** `$0.42`, or `$0.004` under a cent — never a rounded-away zero. */
export function money(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return "";
  return `$${v > 0 && v < 0.01 ? v.toFixed(3) : v.toFixed(2)}`;
}

// --- filters (URL state) ---------------------------------------------------- #

const SOURCES = ["owner", "granted", "asked", "self", "legacy"];

/** The filter state from a `location.search` string. Unknown values drop out. */
export function filterState(search) {
  const p = new URLSearchParams(String(search || ""));
  const kind = p.get("kind");
  const source = p.get("source");
  return {
    q: (p.get("q") || "").trim(),
    kind: kind === "goal" || kind === "ask" ? kind : "",
    source: SOURCES.includes(source) ? source : "",
    finished: p.get("finished") === "1",
    goal: p.get("goal") || "",
  };
}

/** The query string for a filter state (empty parts omitted), `""` or `?…`. */
export function filterQuery(state) {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  if (state.kind) p.set("kind", state.kind);
  if (state.source) p.set("source", state.source);
  if (state.finished) p.set("finished", "1");
  if (state.goal) p.set("goal", state.goal);
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** Does a card (a goal or a decision) pass the filter? */
export function matches(item, f) {
  if (!item) return false;
  const kind = item.kind === "ask" ? "ask" : "goal";
  if (f.kind && f.kind !== kind) return false;
  if (f.source && (kind === "ask" || item.origin !== f.source)) return false;
  if (f.q) {
    const hay = [item.title, item.status_reason, item.body, item.id]
      .map((x) => String(x || "").toLowerCase()).join(" ");
    if (!f.q.toLowerCase().split(/\s+/).every((w) => hay.includes(w))) return false;
  }
  return true;
}

// --- cards ------------------------------------------------------------------ #

/** The owner words for who started a goal. */
export function originText(goal, copy) {
  const o = goal && goal.origin;
  if (o === "granted") {
    return goal.origin_name
      ? format(copy.origin_granted, { name: goal.origin_name }) : (copy.origin_granted_any || "");
  }
  return copy[`origin_${o}`] || copy.origin_legacy || "";
}

/** One "cost against the limit" line. `null` cost is "not recorded", not $0. */
export function costText(goal, limits, copy) {
  const cost = goal && goal.cost_usd;
  if (cost === null || cost === undefined) return copy.cost_none || "";
  const limit = Number(limits && limits.run_usd);
  if (Number.isFinite(limit) && limit > 0) {
    return format(copy.cost, { cost: money(cost), limit: money(limit) });
  }
  return format(copy.cost_no_limit, { cost: money(cost) });
}

/** A title for a referenced goal id, from the board's rows or `titles`. */
function titleOf(id, index, copy) {
  const row = index[id];
  return (row && row.title) || copy.unknown_goal || id;
}

const STATE_CLASS = {
  running: "is-running", blocked: "is-stopped", paused: "is-stopped",
  done: "is-done", cancelled: "is-done",
};

/** The verbs a status offers — each one an existing `/goal` verb. */
export function verbsFor(status) {
  switch (status) {
    case "running": case "ready": case "triage": case "waiting":
      return ["pause", "stop"];
    case "blocked": return ["retry", "stop", "drop"];
    case "paused": return ["resume", "drop"];
    case "cancelled": return ["drop"];
    default: return [];
  }
}

function verbButton(label, verb, id, quiet) {
  const btn = el("button", quiet ? "btn btn-quiet" : "btn", label || "");
  btn.type = "button";
  // Resume IS retry on a paused goal (core.goal_owner_verbs.retry_goal).
  btn.dataset.verb = verb === "resume" ? "retry" : verb;
  btn.dataset.id = String(id || "");
  return btn;
}

function verbRow(goal, copy) {
  const verbs = verbsFor(goal.status);
  if (!verbs.length) return null;
  const actions = el("div", "entry-actions");
  verbs.forEach((v, i) => actions.appendChild(verbButton(copy[v], v, goal.id, i > 0)));
  return actions;
}

/** One goal card. `ctx` = {index, limits, nowMs, readOnly, children}. */
export function goalCard(goal, copy, ctx) {
  const nowMs = ctx.nowMs;
  const index = ctx.index || {};
  const held = (goal.held_by || []).length > 0;
  const cls = held ? "is-needs-you" : (STATE_CLASS[goal.status] || "");
  const card = el("article", `entry goal-card ${cls}`.trim());
  card.dataset.goalId = String(goal.id || "");
  card.appendChild(el("p", "card-kind",
    [copy.kind_goal, originText(goal, copy)].filter(Boolean).join(" · ")));
  const h = el("h3", "entry-title");
  const open = el("button", "card-open", goal.title || goal.id);
  open.type = "button";
  open.dataset.open = String(goal.id || "");
  h.appendChild(open);
  card.appendChild(h);
  if (goal.status_reason) card.appendChild(el("p", "entry-body", goal.status_reason));
  (goal.held_by || []).forEach((askId) => card.appendChild(el("p", "entry-meta",
    format(copy.held_by, { title: titleOf(askId, index, copy) }))));
  const deps = goal.depends_on || [];
  if (deps.length) card.appendChild(el("p", "entry-meta", format(copy.waits_on,
    { goals: deps.map((d) => titleOf(d, index, copy)).join(", ") })));
  const blocks = goal.blocks || [];
  if (blocks.length) card.appendChild(el("p", "entry-meta", format(copy.waited_on_by,
    { goals: blocks.map((d) => titleOf(d, index, copy)).join(", ") })));
  const when = [agoLine("added", goal.created_at, nowMs, copy),
    agoLine("changed", goal.last_activity, nowMs, copy)].filter(Boolean);
  if (when.length) card.appendChild(el("p", "entry-meta", when.join(" · ")));
  card.appendChild(el("p", "entry-meta", costText(goal, ctx.limits, copy)));
  if (Number(goal.failures) > 0) {
    card.appendChild(el("p", "entry-meta",
      format(copy.failures, { count: goal.failures, max: goal.max_retries })));
  }
  const kids = (ctx.children && ctx.children[goal.id]) || [];
  if (kids.length) {
    const group = el("div", "card-group");
    kids.forEach((k) => group.appendChild(goalCard(k, copy, { ...ctx, children: ctx.children })));
    card.appendChild(group);
  }
  if (!ctx.readOnly) {
    const row = verbRow(goal, copy);
    if (row) card.appendChild(row);
  }
  return card;
}

/** The decision controls: an answer, Yes and No — or, for a decision that has
 *  its own place (a spend approval, the token question), a link to the Inbox. */
export function decisionControls(ask, copy) {
  const wrap = el("div", "decision");
  wrap.dataset.askId = String(ask.id || "");
  if (ask.own_place) {
    const a = el("a", "btn btn-quiet", copy.d_decide_inbox || "");
    a.href = "/inbox";
    wrap.appendChild(a);
    return wrap;
  }
  const label = el("label", "entry-meta", copy.answer_label || "");
  const box = el("textarea");
  box.name = "answer";
  box.rows = 2;
  box.placeholder = copy.answer_placeholder || "";
  label.appendChild(box);
  wrap.appendChild(label);
  const actions = el("div", "entry-actions");
  [["decide", copy.yes], ["reject", copy.no]].forEach(([act, word], i) => {
    const b = el("button", i ? "btn btn-quiet" : "btn", word || "");
    b.type = "button";
    b.dataset.decide = act;
    b.dataset.id = String(ask.id || "");
    actions.appendChild(b);
  });
  wrap.appendChild(actions);
  return wrap;
}

/** One open decision, in the first lane. */
export function askCard(ask, copy, ctx) {
  const card = el("article", "entry goal-card is-needs-you");
  card.dataset.askId = String(ask.id || "");
  card.appendChild(el("p", "card-kind", copy.kind_ask || ""));
  card.appendChild(el("h3", "entry-title", ask.title || ask.id));
  if (ask.body) card.appendChild(el("p", "entry-body", ask.body));
  const n = (ask.holds || []).length;
  if (n) card.appendChild(el("p", "entry-meta",
    n === 1 ? copy.holds_one : format(copy.holds, { count: n })));
  const added = agoLine("added", ask.created_at, ctx.nowMs, copy);
  if (added) card.appendChild(el("p", "entry-meta", added));
  if (!ctx.readOnly) card.appendChild(decisionControls(ask, copy));
  return card;
}

function lane(name, title, cards, copy, closed) {
  const box = el("details", "lane");
  box.open = !(closed && closed.has(name));
  box.dataset.lane = name;
  const sum = el("summary");
  sum.appendChild(el("span", null, title));
  sum.appendChild(el("span", "lane-count", String(cards.length)));
  box.appendChild(sum);
  const list = el("div", "ledger");
  cards.forEach((c) => list.appendChild(c));
  box.appendChild(list);
  if (!cards.length) box.appendChild(el("p", "lane-empty", copy.col_empty || ""));
  return box;
}

/** Group sub-goals under a parent in the SAME lane; elsewhere a sub-goal is a
 *  card of its own (its status is its lane, never its parent's). */
export function groupChildren(goals) {
  const byId = {};
  goals.forEach((g) => { byId[g.id] = g; });
  // A parent chain that comes back to the goal (a bad row) must not hide the
  // goals in it: a goal nests only when walking up its parents ends.
  const rooted = (g) => {
    const seen = new Set([g.id]);
    for (let p = byId[g.parent_id]; p; p = byId[p.parent_id]) {
      if (seen.has(p.id)) return false;
      seen.add(p.id);
    }
    return true;
  };
  const children = {};
  const top = [];
  goals.forEach((g) => {
    const parent = g.parent_id && byId[g.parent_id];
    if (parent && parent.status === g.status && parent.id !== g.id && rooted(g)) {
      (children[parent.id] = children[parent.id] || []).push(g);
    } else top.push(g);
  });
  return { top, children };
}

/**
 * Draw the board. Returns which answer it drew, for tests:
 * "unreadable" | "empty" | "filtered" | "rows".
 */
export function renderBoard(ctx, data, copy, opts = {}) {
  const nowMs = Number.isFinite(opts.nowMs) ? opts.nowMs : Date.now();
  const f = opts.filter || filterState("");
  const readOnly = Boolean(opts.readOnly);
  const closed = opts.closed || new Set();
  [ctx.notice, ctx.board, ctx.finished].forEach((n) => n && n.replaceChildren());
  if (ctx.finished) ctx.finished.hidden = true;
  const notice = (node) => ctx.notice && ctx.notice.appendChild(node);

  if (!data || data.error) {
    notice(unreadableEntry((data && data.error) || "", copy));
    return "unreadable";
  }
  if (data.enabled === false) notice(noticeEntry(copy.disabled));
  const side = data.unreadable || {};
  const sideNames = Object.keys(side);
  if (sideNames.length) {
    notice(unreadableEntry(sideNames.map((k) => `${k}: ${side[k]}`).join("; "),
                           copy, "part_unreadable", "unreadable_why"));
  }
  if (data.truncated) notice(noticeEntry(copy.truncated));

  const goals = data.goals || [];
  const finished = data.finished || [];
  const asks = data.asks || [];
  const index = { ...(data.titles || {}) };
  [...goals, ...finished, ...asks].forEach((g) => { index[g.id] = g; });
  const base = { index, limits: data.limits || {}, nowMs, readOnly };

  if (!goals.length && !finished.length && !asks.length) {
    notice(noticeEntry(copy.empty));
    return "empty";
  }

  let shown = 0;
  const lanes = [];
  const askRows = asks.filter((a) => matches(a, f));
  shown += askRows.length;
  if (!f.kind || f.kind === "ask") {
    lanes.push(lane("ask", copy.col_ask, askRows.map((a) => askCard(a, copy, base)), copy, closed));
  }
  if (f.kind !== "ask") {
    const visible = goals.filter((g) => matches(g, f));
    shown += visible.length;
    (data.columns || []).forEach((status) => {
      const { top, children } = groupChildren(visible.filter((g) => g.status === status));
      lanes.push(lane(status, copy[`col_${status}`] || status,
        top.map((g) => goalCard(g, copy, { ...base, children })), copy, closed));
    });
  }
  lanes.forEach((l) => ctx.board && ctx.board.appendChild(l));

  // The finished goals: folded under the board, opened by "Show finished".
  if (ctx.finished && f.kind !== "ask") {
    const counts = data.counts || {};
    const box = el("details", "lane");
    box.dataset.lane = "finished";
    box.open = Boolean(f.finished);
    const sum = el("summary");
    sum.appendChild(el("span", null, copy.finished_title || ""));
    sum.appendChild(el("span", "lane-count", format(copy.finished_summary,
      { done: counts.done || 0, cancelled: counts.cancelled || 0 })));
    box.appendChild(sum);
    const rows = finished.filter((g) => matches(g, f));
    shown += f.finished ? rows.length : 0;
    const list = el("div", "ledger");
    rows.forEach((g) => list.appendChild(goalCard(g, copy, { ...base, children: {} })));
    box.appendChild(list);
    if (!rows.length) box.appendChild(el("p", "lane-empty", copy.finished_none || ""));
    if (data.more_finished) {
      const more = el("button", "btn btn-quiet", copy.show_more || "");
      more.type = "button";
      more.dataset.more = "1";
      box.appendChild(more);
    }
    ctx.finished.appendChild(box);
    ctx.finished.hidden = false;
  }
  if (opts.flash) notice(noticeEntry(opts.flash));
  if (!shown && (f.q || f.kind || f.source)) {
    notice(noticeEntry(copy.filtered_none));
    return "filtered";
  }
  return "rows";
}

// --- detail ----------------------------------------------------------------- #

function section(title, ...nodes) {
  const box = el("section", "detail-section");
  box.appendChild(el("h3", null, title));
  nodes.filter(Boolean).forEach((n) => box.appendChild(n));
  return box;
}

function textBlock(text) { return el("p", "detail-text", text); }

function refList(refs, copy) {
  const ul = el("ul", "list");
  refs.forEach((r) => {
    const li = el("li");
    const b = el("button", "card-open what", r.title || copy.unknown_goal || r.id);
    b.type = "button";
    b.dataset.open = String(r.id || "");
    li.appendChild(b);
    if (r.status) li.appendChild(el("span", "why", r.status));
    ul.appendChild(li);
  });
  return ul;
}

function localTime(ts) {
  // A missing time is no time — Number(null) is 0, which is 1 January 1970.
  const t = ts === null || ts === undefined || ts === "" ? NaN : Number(ts);
  if (!Number.isFinite(t) || t <= 0) return "";
  return new Date(t * 1000).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

/** The words for one history event, from the copy layer; an unknown kind is
 *  shown as written rather than dropped. */
export function eventWords(kind, copy) {
  return copy[`event_${kind}`] || String(kind || "").replace(/_/g, " ");
}

/** One run line: when, outcome, steps, AI cost, why it stopped. */
export function runLine(run, copy) {
  const parts = [localTime(run.ts), run.outcome || ""];
  if (Number.isFinite(Number(run.steps)) && run.steps !== null) {
    parts.push(format(copy.d_run_steps, { count: run.steps }));
  }
  if (run.spend_usd !== null && run.spend_usd !== undefined) parts.push(money(run.spend_usd));
  if (run.stop_text) parts.push(run.stop_text);
  return parts.filter(Boolean).join(" · ");
}

/** Fill the detail body from a detail reader answer. Pure DOM; no fetch. */
export function renderDetail(body, data, copy, opts = {}) {
  body.replaceChildren();
  if (!data || data.error || data.ok === false) {
    body.appendChild(unreadableEntry((data && (data.error || data.message)) || "",
                                     copy, "d_unreadable", "unreadable_why"));
    return "unreadable";
  }
  const readOnly = Boolean(opts.readOnly);
  const head = el("p", "entry-meta",
    [data.status, originText(data, copy)].filter(Boolean).join(" · "));
  body.appendChild(head);
  if (data.status_reason) body.appendChild(el("p", "entry-body", data.status_reason));
  const side = data.unreadable || {};
  if (Object.keys(side).length) {
    body.appendChild(unreadableEntry(Object.keys(side).map((k) => `${k}: ${side[k]}`).join("; "),
                                     copy, "part_unreadable", "unreadable_why"));
  }
  if (!readOnly) {
    const row = verbRow(data, copy);
    if (row) body.appendChild(row);
  }
  if (data.body) body.appendChild(section(copy.d_what, textBlock(data.body)));
  if (data.acceptance || (data.acceptance_checks || []).length) {
    body.appendChild(section(copy.d_done_when,
      data.acceptance ? textBlock(data.acceptance) : null,
      (data.acceptance_checks || []).length
        ? textBlock(`${copy.d_checks}: ${JSON.stringify(data.acceptance_checks)}`) : null));
  }
  (data.decisions || []).forEach((ask) => {
    body.appendChild(section(copy.d_decision, el("p", "entry-body", ask.title),
      ask.body ? textBlock(ask.body) : null,
      readOnly ? null : decisionControls(ask, copy)));
  });
  if (data.parent) body.appendChild(section(copy.d_parent, refList([data.parent], copy)));
  if ((data.waits_on || []).length) body.appendChild(section(copy.d_waits_on, refList(data.waits_on, copy)));
  if ((data.waited_on_by || []).length) {
    body.appendChild(section(copy.d_waited_on_by, refList(data.waited_on_by, copy)));
  }
  if ((data.sub_goals || []).length) body.appendChild(section(copy.d_sub_goals, refList(data.sub_goals, copy)));
  if (data.result) body.appendChild(section(copy.d_result, textBlock(data.result)));
  if (data.last_failure_error) body.appendChild(section(copy.d_failure, textBlock(data.last_failure_error)));
  if ((data.attempts || []).length) {
    const ul = el("ul", "list");
    data.attempts.forEach((a) => ul.appendChild(el("li", "what",
      `${localTime(a.ts)} · ${a.error || ""}`)));
    body.appendChild(section(copy.d_tries, ul));
  }

  const runs = el("ul", "list");
  (data.runs || []).forEach((r) => {
    const li = el("li");
    li.appendChild(el("span", "what", runLine(r, copy)));
    if (r.reason) li.appendChild(el("span", "why", r.reason));
    if (r.session_id) {
      const a = el("a", "btn btn-quiet", copy.open_chat || "");
      a.href = `/c/${encodeURIComponent(r.session_id)}`;
      li.appendChild(a);
    }
    runs.appendChild(li);
  });
  const limits = data.limits || {};
  body.appendChild(section(copy.d_runs,
    (data.runs || []).length ? runs : el("p", "lane-empty", copy.d_runs_none || ""),
    Number(limits.run_usd) > 0 ? el("p", "entry-meta", format(copy.d_limits,
      { steps: limits.max_steps, limit: money(limits.run_usd) })) : null,
    el("p", "entry-meta", costText(data, limits, copy))));

  // The files: filled in after, from each linked chat's artifact ledger.
  const files = el("div", "detail-files");
  files.dataset.sessions = (data.sessions || []).join(",");
  const chats = el("ul", "list");
  (data.sessions || []).forEach((sid) => {
    const li = el("li");
    const a = el("a", "what", copy.open_chat || "");
    a.href = `/c/${encodeURIComponent(sid)}`;
    li.appendChild(a);
    li.appendChild(el("span", "why", sid));
    chats.appendChild(li);
  });
  body.appendChild(section(copy.d_files, (data.sessions || []).length ? chats : null, files));

  const hist = el("ul", "list");
  (data.events || []).forEach((e) => {
    const li = el("li");
    li.appendChild(el("span", "what", eventWords(e.kind, copy)));
    li.appendChild(el("span", "why", localTime(e.ts)));
    hist.appendChild(li);
  });
  body.appendChild(section(copy.d_history,
    (data.events || []).length ? hist : el("p", "lane-empty", copy.d_history_none || "")));
  return "rows";
}

/** Fill the files box from artifact answers `[{session, body}]`. */
export function renderFiles(box, answers, copy) {
  box.replaceChildren();
  const names = [];
  let failed = false;
  (answers || []).forEach(({ body }) => {
    if (!body || body.error || !Array.isArray(body.artifacts)) { failed = true; return; }
    body.artifacts.forEach((a) => names.push(a));
  });
  if (names.length) {
    const ul = el("ul", "list");
    names.forEach((a) => {
      const li = el("li");
      if (a.url) {
        const link = el("a", "what", a.name || a.path || "");
        link.href = a.url;
        li.appendChild(link);
      } else li.appendChild(el("span", "what", a.name || a.path || ""));
      if (a.kind) li.appendChild(el("span", "why", a.kind));
      ul.appendChild(li);
    });
    box.appendChild(ul);
  } else if (!failed) box.appendChild(el("p", "lane-empty", copy.d_files_none || ""));
  if (failed) box.appendChild(el("p", "entry-meta", copy.d_files_unreadable || ""));
}

// --- reads + wiring --------------------------------------------------------- #

async function getJson(url) {
  const resp = await fetch(url, { credentials: "include" });
  if (!resp.ok && resp.status !== 404) return { error: String(resp.status) };
  return await resp.json();
}

export const boardUrl = (n) => `/api/webgate/goals/board?finished=${encodeURIComponent(n)}`;
export const detailUrl = (id) => `/api/webgate/goals/${encodeURIComponent(id)}/detail`;

function say(root, ok, message) {
  if (!root) return;
  let line = root.querySelector(":scope > .entry-answer");
  if (!line) {
    line = el("p", "entry-meta entry-answer");
    root.appendChild(line);
  }
  line.textContent = message || "";
  line.dataset.ok = ok ? "true" : "false";
}

async function act(url, payload, copy) {
  try {
    const { ok, body } = await postJson(url, payload);
    const message = (body && (body.message || body.warning))
      || (ok ? "" : copy.unreachable || "");
    return { ok: Boolean(ok && body && body.ok !== false), message };
  } catch (err) {
    console.error("[work-goals] the change did not reach the console", err);
    return { ok: false, message: copy.unreachable || "" };
  }
}

function bind() {
  const copyNode = document.getElementById("work-goals-copy");
  if (!copyNode) return;
  const copy = copyFrom(copyNode);
  const readOnly = copy.read_only === "1";
  const ctx = {
    notice: document.getElementById("work-goals-notice"),
    board: document.getElementById("work-goals-board"),
    finished: document.getElementById("work-goals-finished"),
  };
  const bar = document.getElementById("work-goals-bar");
  const fresh = document.getElementById("work-goals-fresh");
  const dialog = document.getElementById("goal-detail");
  const dTitle = document.getElementById("goal-detail-title");
  const dBody = document.getElementById("goal-detail-body");
  let filter = filterState(location.search);
  let finishedN = 20;
  let last = null;
  let stale = false;
  // What a redraw must keep: the lanes the owner folded, and the server's
  // answer to the last action (the card that said it is redrawn away).
  const closed = new Set();
  let flash = "";

  const setUrl = () => {
    try {
      history.replaceState(null, "", `${location.pathname}${filterQuery(filter)}${location.hash}`);
    } catch (err) { /* ignore */ }
  };
  const setFresh = () => {
    if (!fresh) return;
    fresh.dataset.stale = stale ? "true" : "false";
    fresh.textContent = stale ? (copy.stale || "")
      : (last ? format(copy.updated, { time: new Date(last).toLocaleTimeString(undefined,
          { hour: "2-digit", minute: "2-digit" }) }) : "");
  };

  let data = null;
  const read = latestOnly(() => getJson(boardUrl(finishedN)));
  const draw = () => renderBoard(ctx, data, copy, { filter, readOnly, closed, flash });
  // A live redraw waits while the owner is in the middle of something on the
  // board: an answer typed, a box in focus, an action on its way.
  const busy = () => Boolean(ctx.board && (
    ctx.board.querySelector("button:disabled")
    || Array.from(ctx.board.querySelectorAll("textarea")).some((t) => t.value.trim())
    || ctx.board.contains(document.activeElement)
      && document.activeElement.matches("textarea, input")));
  const pane = ctx.board && ctx.board.closest("[data-pane]");
  const load = () => read()
    .then((answer) => {
      if (answer.stale) return;
      data = answer.value;
      if (!data.error) last = Date.now();
      draw();
      setFresh();
    })
    .catch((err) => {
      console.error("[work-goals] could not read the goal board", err);
      data = { error: (err && String(err.message)) || "error" };
      draw();
    });

  // Filters: the controls start from the URL and write back to it.
  if (bar) {
    const q = bar.querySelector("[name=q]");
    const kind = bar.querySelector("[name=kind]");
    const source = bar.querySelector("[name=source]");
    const fin = bar.querySelector("[name=finished]");
    if (q) q.value = filter.q;
    if (kind) kind.value = filter.kind;
    if (source) source.value = filter.source;
    if (fin) fin.checked = filter.finished;
    const apply = () => {
      flash = "";
      filter = { ...filter, q: (q && q.value.trim()) || "", kind: (kind && kind.value) || "",
                 source: (source && source.value) || "", finished: Boolean(fin && fin.checked) };
      setUrl();
      if (data) draw();
    };
    bar.addEventListener("input", apply);
    bar.addEventListener("change", apply);
  }

  // The detail dialog.
  const openDetail = (id) => {
    if (!dialog || !id) return;
    filter = { ...filter, goal: id };
    setUrl();
    const row = (data && [...(data.goals || []), ...(data.finished || [])]
      .find((g) => g.id === id)) || null;
    if (dTitle) dTitle.textContent = (row && row.title) || id;
    if (dBody) dBody.replaceChildren(noticeEntry(copy.d_loading));
    if (!dialog.open && dialog.showModal) dialog.showModal();
    getJson(detailUrl(id)).then((d) => {
      if (filter.goal !== id) return;
      if (dTitle && d && d.title) dTitle.textContent = d.title;
      renderDetail(dBody, d, copy, { readOnly });
      const box = dBody.querySelector(".detail-files");
      const sids = (d && d.sessions) || [];
      if (box && sids.length) {
        Promise.all(sids.slice(0, 3).map((sid) =>
          getJson(`/api/webgate/artifacts?session_id=${encodeURIComponent(sid)}`)
            .then((body) => ({ sid, body }), () => ({ sid, body: null }))))
          .then((answers) => renderFiles(box, answers, copy));
      } else if (box) renderFiles(box, [], copy);
    }).catch((err) => {
      console.error("[work-goals] could not read the goal", err);
      renderDetail(dBody, { error: (err && String(err.message)) || "error" }, copy);
    });
  };
  const closeDetail = () => {
    filter = { ...filter, goal: "" };
    setUrl();
    if (dialog && dialog.open) dialog.close();
  };
  if (dialog) {
    dialog.addEventListener("close", () => { if (filter.goal) { filter = { ...filter, goal: "" }; setUrl(); } });
    const x = document.getElementById("goal-detail-close");
    if (x) x.addEventListener("click", closeDetail);
  }

  // One click handler for the board and the dialog: open, verb, decide, more.
  const onClick = async (ev) => {
    const opener = ev.target.closest("[data-open]");
    if (opener) { openDetail(opener.dataset.open); return; }
    const more = ev.target.closest("button[data-more]");
    if (more) { finishedN += 20; load(); return; }
    const verb = ev.target.closest("button[data-verb]");
    const decide = ev.target.closest("button[data-decide]");
    const btn = verb || decide;
    if (!btn) return;
    const root = btn.closest(".decision") || btn.closest(".entry") || btn.parentElement;
    // Only this card's own buttons — never those of the sub-goals inside it.
    const own = btn.closest(".decision") || btn.closest(".entry-actions");
    const buttons = own ? own.querySelectorAll("button") : [btn];
    buttons.forEach((b) => { b.disabled = true; });
    let result;
    if (verb) {
      result = await act(`/api/webgate/goals/${encodeURIComponent(btn.dataset.id)}`
        + `/${encodeURIComponent(btn.dataset.verb)}`, undefined, copy);
    } else {
      const box = root && root.querySelector("textarea[name=answer]");
      result = await act(`/api/webgate/inbox/ask/${encodeURIComponent(btn.dataset.id)}`
        + `/${encodeURIComponent(btn.dataset.decide)}`,
        { answer: (box && box.value.trim()) || "" }, copy);
    }
    say(btn.closest(".entry-actions") ? btn.closest(".entry-actions").parentElement : root,
        result.ok, result.message);
    if (!result.ok) buttons.forEach((b) => { b.disabled = false; });
    else {
      flash = result.message || "";
      const said = flash;
      setTimeout(() => { if (flash === said) flash = ""; }, 15000);
      load();
      if (dialog && dialog.open && filter.goal) openDetail(filter.goal);
    }
  };
  [ctx.board, ctx.finished, dBody].forEach((n) => n && n.addEventListener("click", onClick));
  // A folded lane stays folded; the finished lane IS the "show finished" filter.
  // (`toggle` does not bubble, so listen in the capture phase.)
  const onToggle = (ev) => {
    const box = ev.target;
    if (!box.matches || !box.matches("details.lane")) return;
    if (box.dataset.lane === "finished") {
      if (box.open === filter.finished) return;
      filter = { ...filter, finished: box.open };
      const fin = bar && bar.querySelector("[name=finished]");
      if (fin) fin.checked = box.open;
      setUrl();
      return;
    }
    if (box.open) closed.delete(box.dataset.lane); else closed.add(box.dataset.lane);
  };
  [ctx.board, ctx.finished].forEach((n) => n && n.addEventListener("toggle", onToggle, true));

  if (ctx.notice) ctx.notice.replaceChildren(noticeEntry(copy.loading));
  load().then(() => { if (filter.goal) openDetail(filter.goal); });

  // Live: the activity stream, never a poll loop of our own. `polyrob:tick`
  // fires only while the socket is down, so it also marks the board stale.
  // The read waits while the Goals tab is hidden (it reads again when the tab
  // opens) and while the owner is busy on the board (`busy`).
  let pending = null;
  let dirty = false;
  const redraw = () => {
    if (pending) return;
    pending = setTimeout(() => {
      pending = null;
      if (pane && pane.hidden) { dirty = true; return; }
      if (busy()) { redraw(); return; }
      load();
    }, 1500);
  };
  if (pane && typeof MutationObserver !== "undefined") {
    new MutationObserver(() => {
      if (!pane.hidden && dirty) { dirty = false; load(); }
    }).observe(pane, { attributes: true, attributeFilter: ["hidden"] });
  }
  document.addEventListener("polyrob:activity", () => { stale = false; redraw(); });
  document.addEventListener("polyrob:live-ok", () => { stale = false; setFresh(); redraw(); });
  document.addEventListener("polyrob:live-refused", () => { stale = true; setFresh(); });
  document.addEventListener("polyrob:tick", () => { stale = true; setFresh(); redraw(); });
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
}
