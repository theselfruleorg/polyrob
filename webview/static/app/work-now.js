/**
 * work-now.js — Work › Now & next and Work › On a clock (043 WS-P1 / §5).
 *
 * Two tabs of the one Work screen, drawn client-side over three tenant-scoped
 * readers:
 *
 *   - Now & next — GET /api/webgate/goals (the goal board: `list_recent` +
 *     `status_counts`) and GET /api/webgate/running (background helpers). Now is
 *     the running goals plus the helper lines; Next is what is queued, and its
 *     count is read from `status_counts`, NEVER from the goals array's order
 *     (that array is `list_recent`, newest-first, not the dispatcher's
 *     `board.list` order — but the aside is still counted, not measured off a
 *     window).
 *   - On a clock — GET /api/webgate/cron, listed with a Cancel that posts to
 *     /api/webgate/cron/{id}/cancel (the verb Telegram's owner already uses).
 *
 * Four rules it keeps, the same ones worklog.js keeps:
 *
 * 1. **Every word comes from the copy layer.** No 043 template carries an inline
 *    script (tests/unit/webview/test_no_new_inline_script.py), so the words ride
 *    on `#work-now-copy`'s data attributes and this reads them off `dataset`.
 * 2. **An unreadable store is a dashed entry with its reason, never a silent
 *    drop and never a confident empty list.** The reader answers 200 with an
 *    `error` field on a failed read; that becomes the dashed entry.
 * 3. **It draws no verb it cannot act on.** Under `WEBVIEW_READ_ONLY` the shell
 *    states the posture once and this renders no Stop / Pause / Cancel — an
 *    absent button, not a disabled-looking one that 403s.
 * 4. **It decides nothing itself.** A Stop / Cancel is a POST to an existing
 *    route (`http.js`), and the row says back exactly what the server answered.
 */
import { postJson } from "./http.js";

/** The copy the server handed over, as a plain object. */
export function copyFrom(node) {
  return node ? { ...node.dataset } : {};
}

/** Fill a `{name}` template from a vars object. Copy owns the words; this owns
 *  only the values. */
export function format(template, vars) {
  return String(template || "").replace(/\{(\w+)\}/g, (m, key) =>
    vars && Object.prototype.hasOwnProperty.call(vars, key)
      ? String(vars[key]) : m);
}

/** A compact duration in plain words, from copy templates. */
export function elapsed(seconds, copy) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  if (s < 60) return format(copy && copy.secs, { count: s });
  const m = Math.floor(s / 60);
  if (m < 60) return format(copy && copy.mins, { count: m });
  const h = Math.floor(m / 60);
  if (h < 24) return format(copy && copy.hours, { count: h });
  return format(copy && copy.days, { count: Math.floor(h / 24) });
}

/** Seconds since an epoch-seconds timestamp, floored at zero. `NaN` in → 0. */
function since(ts, nowMs) {
  const now = Number.isFinite(nowMs) ? nowMs : Date.now();
  const t = Number(ts);
  if (!Number.isFinite(t)) return 0;
  return Math.max(0, now / 1000 - t);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** A plain informational entry (empty state, goals-off, loading). */
export function noticeEntry(text) {
  const entry = el("div", "entry");
  entry.appendChild(el("p", "entry-body", text || ""));
  return entry;
}

/** The dashed entry for a store that could not be read. `reason` is the machine
 *  detail, kept behind a disclosure; the sentence a person reads is copy. */
export function unreadableEntry(reason, copy, titleKey = "unreadable",
                               whyKey = "unreadable_why") {
  const entry = el("div", "entry is-unknown");
  entry.appendChild(el("h3", "entry-title", (copy && copy[titleKey]) || ""));
  if (reason) {
    const details = el("details");
    details.appendChild(el("summary", null, (copy && copy[whyKey]) || ""));
    details.appendChild(el("p", "entry-meta", reason));
    entry.appendChild(details);
  }
  return entry;
}

/** A goal-verb / cron-cancel button, carrying only the route parts on its
 *  dataset — the click is wired by delegation, so this stays pure. */
function verbButton(label, verb, id, extra) {
  const btn = el("button", extra ? `btn ${extra}` : "btn", label || "");
  btn.type = "button";
  btn.dataset.verb = verb;
  btn.dataset.id = String(id || "");
  return btn;
}

/** One RUNNING goal: title, how long it has run, and a Stop (unless read-only).
 *  Stop is `cancel` — the decisive verb; a queued goal gets the recoverable
 *  Pause instead. */
export function runningGoalEntry(goal, copy, nowMs, readOnly) {
  const entry = el("div", "entry is-running");
  entry.dataset.goalId = String((goal && goal.id) || "");
  entry.appendChild(el("h3", "entry-title", (goal && goal.title) || ""));
  entry.appendChild(el("p", "entry-body",
    format(copy && copy.elapsed_started,
           { elapsed: elapsed(since(goal && goal.created_at, nowMs), copy) })));
  if (!readOnly) {
    const actions = el("div", "entry-actions");
    actions.appendChild(verbButton(copy && copy.stop, "cancel", goal && goal.id));
    entry.appendChild(actions);
  }
  return entry;
}

/** One background helper (delegation) as a live line. No Stop: a helper is a
 *  child of a goal, cannot spend or write, and reports back on its own. */
export function workerEntry(row, copy, nowMs) {
  const entry = el("div", "entry is-running");
  entry.dataset.delegationId = String((row && row.delegation_id) || "");
  entry.appendChild(el("h3", "entry-title", (copy && copy.helper_lead) || ""));
  const goal = (row && row.goal) || "";
  entry.appendChild(el("p", "entry-body",
    goal ? format(copy && copy.helper_goal, { goal })
         : ((copy && copy.helper_body) || "")));
  if (goal) entry.appendChild(el("p", "entry-meta", (copy && copy.helper_body) || ""));
  entry.appendChild(el("p", "entry-meta",
    format(copy && copy.elapsed_started,
           { elapsed: elapsed(since(row && row.dispatched_at, nowMs), copy) })));
  return entry;
}

/** One cron job mid-run (2026-09-16 audit, B1): the CAS in cron/jobs.py holds
 *  `status='running'` for the length of the run, so this IS a live actor. No
 *  Stop: a cron run ends on its own budget (`max_duration_seconds`). */
export function cronRunEntry(row, copy, nowMs) {
  const entry = el("div", "entry is-running");
  entry.dataset.cronId = String((row && row.id) || "");
  entry.appendChild(el("h3", "entry-title", (copy && copy.cron_title) || ""));
  if (row && row.task) entry.appendChild(el("p", "entry-body", row.task));
  if (row && Number.isFinite(Number(row.since))) {
    entry.appendChild(el("p", "entry-meta",
      format(copy && copy.elapsed_started,
             { elapsed: elapsed(since(row.since, nowMs), copy) })));
  }
  return entry;
}

/** One live session the shared registry knows about, linked to its own chat. */
export function sessionEntry(row, copy, nowMs) {
  const entry = el("div", "entry is-running");
  const sid = String((row && row.session_id) || "");
  entry.dataset.sessionId = sid;
  entry.appendChild(el("h3", "entry-title", (copy && copy.session_title) || ""));
  if (row && row.task) entry.appendChild(el("p", "entry-body", row.task));
  const meta = el("p", "entry-meta");
  if (row && row.status) meta.appendChild(el("span", "val", String(row.status)));
  if (row && Number.isFinite(Number(row.since))) {
    if (meta.childNodes.length) meta.appendChild(document.createTextNode(" "));
    meta.appendChild(document.createTextNode(
      format(copy && copy.elapsed_started,
             { elapsed: elapsed(since(row.since, nowMs), copy) })));
  }
  if (meta.childNodes.length) entry.appendChild(meta);
  if (sid) {
    const actions = el("div", "entry-actions");
    const open = el("a", "btn btn-quiet", (copy && copy.open_chat) || "");
    open.href = `/c/${encodeURIComponent(sid)}`;
    actions.appendChild(open);
    entry.appendChild(actions);
  }
  return entry;
}

//: The statuses that mean "waiting its turn". `blocked` is shown too, as a
//: stopped entry, but it is not counted as queued.
const QUEUED = ["ready", "waiting", "triage"];
const STATUS_WORD = {
  ready: "status_ready", waiting: "status_waiting",
  triage: "status_triage", blocked: "status_blocked",
};

/** One QUEUED (or blocked) goal: title, its plain status, and the verb it
 *  offers. A blocked goal is stopped after repeated failures — Try again
 *  (retry) or Drop it (cancel); a genuinely queued one offers Pause. */
export function queuedGoalEntry(goal, copy, readOnly) {
  const blocked = goal && goal.status === "blocked";
  const entry = el("div", blocked ? "entry is-stopped" : "entry");
  entry.dataset.goalId = String((goal && goal.id) || "");
  entry.appendChild(el("h3", "entry-title", (goal && goal.title) || ""));
  const word = (copy && copy[STATUS_WORD[goal && goal.status]])
    || (goal && goal.status) || "";
  entry.appendChild(el("p", "entry-body", word));
  if (!readOnly) {
    const actions = el("div", "entry-actions");
    if (blocked) {
      actions.appendChild(verbButton(copy && copy.retry, "retry", goal && goal.id));
      actions.appendChild(verbButton(copy && copy.drop, "cancel", goal && goal.id, "btn-quiet"));
    } else {
      actions.appendChild(verbButton(copy && copy.pause, "pause", goal && goal.id, "btn-quiet"));
    }
    entry.appendChild(actions);
  }
  return entry;
}

/** The Next aside, read from `status_counts` — NEVER off the goals array. The
 *  array is newest-first (`list_recent`); the count is over every row. */
export function nextSummary(counts, copy) {
  const c = counts || {};
  const n = (c.ready || 0) + (c.waiting || 0) + (c.triage || 0);
  return format(copy && copy.next_queued, { count: n });
}

/**
 * Draw Now & next. `data` is the goals reader body; `runningData` the helpers.
 * Returns which answer it drew ("disabled" | "unreadable" | "rows"), for tests.
 */
export function renderNowNext(ctx, data, runningData, copy, opts = {}) {
  const nowMs = Number.isFinite(opts.nowMs) ? opts.nowMs : Date.now();
  const readOnly = Boolean(opts.readOnly);
  if (ctx.running) ctx.running.replaceChildren();
  if (ctx.next) ctx.next.replaceChildren();

  if (data && data.enabled === false) {
    if (ctx.running) ctx.running.appendChild(noticeEntry(copy && copy.disabled));
    if (ctx.runningAside) ctx.runningAside.textContent = "";
    if (ctx.nextAside) ctx.nextAside.textContent = "";
    return "disabled";
  }
  if (data && data.error) {
    if (ctx.running) ctx.running.appendChild(unreadableEntry(data.error, copy));
    if (ctx.runningAside) ctx.runningAside.textContent = "";
    if (ctx.nextAside) ctx.nextAside.textContent = "";
    return "unreadable";
  }

  const goals = (data && data.goals) || [];
  const counts = (data && data.counts) || {};
  const runningGoals = goals.filter((g) => g.status === "running");
  const workers = (runningData && runningData.running) || [];
  const queued = goals.filter((g) => QUEUED.includes(g.status));
  const blocked = goals.filter((g) => g.status === "blocked");
  // The live reader (/api/webgate/live): cron runs + live sessions. A goal it
  // also names is already drawn above from the board, so it is not repeated.
  const live = opts.live || null;
  const goalSessions = new Set(runningGoals.map((g) => String(g.session_id || "")));
  const cronRuns = (live && live.cron) || [];
  const sessions = ((live && live.sessions) || [])
    .filter((row) => !goalSessions.has(String(row.session_id || "")));
  const liveUnreadable = (live && live.unreadable) || {};

  // Now.
  runningGoals.forEach((g) =>
    ctx.running && ctx.running.appendChild(runningGoalEntry(g, copy, nowMs, readOnly)));
  workers.forEach((w) =>
    ctx.running && ctx.running.appendChild(workerEntry(w, copy, nowMs)));
  cronRuns.forEach((c) =>
    ctx.running && ctx.running.appendChild(cronRunEntry(c, copy, nowMs)));
  sessions.forEach((row) =>
    ctx.running && ctx.running.appendChild(sessionEntry(row, copy, nowMs)));
  const total = runningGoals.length + workers.length + cronRuns.length + sessions.length;
  if (!total && ctx.running) {
    ctx.running.appendChild(noticeEntry(copy && copy.empty_now));
  }
  // A helper read that failed is NAMED here, not silently dropped.
  if (runningData && runningData.error && ctx.running) {
    ctx.running.appendChild(unreadableEntry(runningData.error, copy));
  }
  // …and so is every live source that could not be read (a missing store on a
  // fresh install is a fact, not a zero).
  const liveReasons = Object.keys(liveUnreadable)
    .map((k) => `${k}: ${liveUnreadable[k]}`);
  if (live && live.error) liveReasons.push(String(live.error));
  if (liveReasons.length && ctx.running) {
    ctx.running.appendChild(unreadableEntry(liveReasons.join("; "), copy,
                                            "live_unreadable", "live_unreadable_why"));
  }
  if (ctx.runningAside) {
    ctx.runningAside.textContent = format(copy && copy.running_count, { count: total });
  }

  // Next.
  [...queued, ...blocked].forEach((g) =>
    ctx.next && ctx.next.appendChild(queuedGoalEntry(g, copy, readOnly)));
  if (!queued.length && !blocked.length && ctx.next) {
    ctx.next.appendChild(noticeEntry(copy && copy.empty_next));
  }
  if (ctx.nextAside) ctx.nextAside.textContent = nextSummary(counts, copy);
  return "rows";
}

/** One cron job as a `<tr>`: what it does, when, last run, and Cancel. A row
 *  that has never run says so plainly — a null last-run is "not yet run", a
 *  genuine state, not an unreadable one (the whole-store read failing is the
 *  `error` path, handled in renderSchedule). */
export function cronRow(job, copy, nowMs, readOnly) {
  const tr = el("tr");
  tr.dataset.jobId = String((job && job.id) || "");
  const what = el("td", "what");
  what.dataset.label = (copy && copy.sched_header_what) || "";
  what.appendChild(document.createTextNode((job && job.task) || ""));
  tr.appendChild(what);
  tr.appendChild(el("td", null, (job && job.schedule_spec) || ""));
  const last = el("td");
  const lastMs = job && job.last_run_at ? Date.parse(job.last_run_at) : NaN;
  if (Number.isFinite(lastMs)) {
    const now = Number.isFinite(nowMs) ? nowMs : Date.now();
    last.textContent = format(copy && copy.sched_ran_ago,
      { elapsed: elapsed((now - lastMs) / 1000, copy) });
  } else {
    last.textContent = (copy && copy.sched_last_never) || "";
  }
  tr.appendChild(last);
  const act = el("td");
  if (!readOnly) {
    const btn = el("button", "btn btn-quiet", (copy && copy.sched_cancel) || "");
    btn.type = "button";
    btn.dataset.cron = "cancel";
    btn.dataset.id = String((job && job.id) || "");
    act.appendChild(btn);
  }
  tr.appendChild(act);
  return tr;
}

/** Draw On a clock. `data` is the cron reader body. Returns the answer it drew
 *  ("disabled" | "unreadable" | "empty" | "rows"). */
export function renderSchedule(ctx, data, copy, opts = {}) {
  const nowMs = Number.isFinite(opts.nowMs) ? opts.nowMs : Date.now();
  const readOnly = Boolean(opts.readOnly);
  if (ctx.notice) ctx.notice.replaceChildren();
  if (ctx.rows) ctx.rows.replaceChildren();
  const setTable = (hidden) => { if (ctx.table) ctx.table.hidden = hidden; };

  if (data && data.enabled === false) {
    setTable(true);
    if (ctx.notice) ctx.notice.appendChild(noticeEntry(copy && copy.sched_disabled));
    return "disabled";
  }
  if (data && data.error) {
    setTable(true);
    if (ctx.notice) ctx.notice.appendChild(unreadableEntry(
      data.error, copy, "sched_unreadable", "sched_unreadable_why"));
    return "unreadable";
  }
  const jobs = (data && data.jobs) || [];
  if (!jobs.length) {
    setTable(true);
    if (ctx.notice) ctx.notice.appendChild(noticeEntry(copy && copy.sched_empty));
    return "empty";
  }
  setTable(false);
  jobs.forEach((j) => ctx.rows && ctx.rows.appendChild(cronRow(j, copy, nowMs, readOnly)));
  return "rows";
}

// --- tabs ------------------------------------------------------------------- #

/** The active tab id, from the URL hash, defaulting to the first tab. An
 *  unknown hash (or none) is the first tab, never a blank screen. */
export function activeTab(hash, tabs) {
  const want = String(hash || "").replace(/^#/, "");
  return (tabs || []).includes(want) ? want : (tabs || [])[0];
}

/** Reveal the pane whose `data-pane` matches `name`, hide the rest, and move
 *  `aria-current` onto its subnav link. */
export function showTab(name, links, panes) {
  (links || []).forEach((a) => {
    if (a.dataset.tab === name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  (panes || []).forEach((p) => { p.hidden = p.dataset.pane !== name; });
}

function wireTabs() {
  const links = Array.from(document.querySelectorAll(".subnav a[data-tab]"));
  const panes = Array.from(document.querySelectorAll("[data-pane]"));
  if (!links.length || !panes.length) return;
  const tabs = links.map((a) => a.dataset.tab);
  const apply = () => showTab(activeTab(location.hash, tabs), links, panes);
  links.forEach((a) => a.addEventListener("click", (ev) => {
    ev.preventDefault();
    const name = a.dataset.tab;
    try { history.replaceState(null, "", `#${name}`); } catch (err) { /* ignore */ }
    showTab(name, links, panes);
  }));
  window.addEventListener("hashchange", apply);
  apply();
}

// --- reads ------------------------------------------------------------------ #

async function getJson(url, fetcher) {
  const call = fetcher || fetch;
  const resp = await call(url, { credentials: "include" });
  if (!resp.ok) return { error: String(resp.status) };
  return await resp.json();
}

export const loadGoals = (f) => getJson("/api/webgate/goals", f);
export const loadRunning = (f) => getJson("/api/webgate/running", f);
export const loadCron = (f) => getJson("/api/webgate/cron", f);
export const loadLive = (f) => getJson("/api/webgate/live", f);

// --- mutations -------------------------------------------------------------- #

/** Say the answer on the row, and retire the buttons a decision consumed. */
function applyResult(row, ok, message) {
  if (!row) return;
  let line = row.querySelector(".entry-answer");
  if (!line) {
    line = document.createElement("p");
    line.className = "entry-meta entry-answer";
    row.appendChild(line);
  }
  line.textContent = message || "";
  line.dataset.ok = ok ? "true" : "false";
  if (ok) {
    const actions = row.querySelector(".entry-actions");
    if (actions) actions.remove();
  }
}

async function post(url, copy) {
  try {
    const { ok, body } = await postJson(url, undefined);
    const message = (body && (body.message || body.warning))
      || (ok ? "" : (copy && copy.unreachable) || "");
    return { ok: Boolean(ok && body && body.ok), message };
  } catch (err) {
    console.error("[work-now] the change did not reach the console", err);
    return { ok: false, message: (copy && copy.unreachable) || "" };
  }
}

function wireMutations(nowCtx, schedCtx, copy) {
  const goalHandler = async (ev) => {
    const btn = ev.target.closest("button[data-verb]");
    if (!btn) return;
    const row = btn.closest(".entry");
    const buttons = row ? row.querySelectorAll("button") : [btn];
    buttons.forEach((b) => { b.disabled = true; });
    const result = await post(
      `/api/webgate/goals/${encodeURIComponent(btn.dataset.id)}`
      + `/${encodeURIComponent(btn.dataset.verb)}`, copy);
    applyResult(row, result.ok, result.message);
    if (!result.ok) buttons.forEach((b) => { b.disabled = false; });
  };
  [nowCtx.running, nowCtx.next].forEach((root) =>
    root && root.addEventListener("click", goalHandler));
  if (schedCtx.rows) {
    schedCtx.rows.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button[data-cron]");
      if (!btn) return;
      const tr = btn.closest("tr");
      const buttons = tr ? tr.querySelectorAll("button") : [btn];
      buttons.forEach((b) => { b.disabled = true; });
      const result = await post(
        `/api/webgate/cron/${encodeURIComponent(btn.dataset.id)}/cancel`, {
          ...copy, unreachable: copy.sched_unreachable || copy.unreachable,
        });
      let answer = tr && tr.querySelector(".cron-answer");
      const cell = tr && tr.lastElementChild;
      if (cell) {
        if (!answer) {
          answer = document.createElement("span");
          answer.className = "cron-answer why";
          cell.appendChild(answer);
        }
        answer.textContent = result.message || "";
      }
      if (result.ok && btn) btn.remove();
      else buttons.forEach((b) => { b.disabled = false; });
    });
  }
}

// --- wiring ----------------------------------------------------------------- #

function byId(id) { return document.getElementById(id); }

function bind() {
  const copyNode = byId("work-now-copy");
  if (!copyNode) return;
  const copy = copyFrom(copyNode);
  const readOnly = copy.read_only === "1";

  wireTabs();

  const nowCtx = {
    running: byId("work-now-running"),
    next: byId("work-now-next"),
    runningAside: byId("work-now-running-aside"),
    nextAside: byId("work-now-next-aside"),
  };
  const schedCtx = {
    notice: byId("work-schedule-notice"),
    table: byId("work-schedule-table"),
    rows: byId("work-schedule-rows"),
  };

  wireMutations(nowCtx, schedCtx, copy);

  const drawNow = () => Promise.all([loadGoals(), loadRunning(), loadLive()])
    .then(([goalsData, runningData, liveData]) =>
      renderNowNext(nowCtx, goalsData, runningData, copy, { readOnly, live: liveData }))
    .catch((err) => {
      console.error("[work-now] could not read Now & next", err);
      renderNowNext(nowCtx, { error: (err && String(err.message)) || "error" },
                    null, copy, { readOnly });
    });
  const drawSchedule = () => loadCron()
    .then((cronData) => renderSchedule(schedCtx, cronData, copy, { readOnly }))
    .catch((err) => {
      console.error("[work-now] could not read the schedule", err);
      renderSchedule(schedCtx, { error: (err && String(err.message)) || "error" },
                     copy, { readOnly });
    });

  if (nowCtx.running) nowCtx.running.replaceChildren(noticeEntry(copy.loading));
  drawNow();
  if (schedCtx.notice) schedCtx.notice.replaceChildren(noticeEntry(copy.sched_loading));
  drawSchedule();

  // Live: redraw when the activity stream says something moved (live.js joins
  // the `activity` room and re-dispatches events on `document`), and on the
  // heartbeat, so a run that starts while the page is open appears without a
  // reload. Debounced — a burst of events is one redraw.
  let pending = null;
  const redraw = () => {
    if (pending) return;
    pending = setTimeout(() => { pending = null; drawNow(); drawSchedule(); }, 1500);
  };
  document.addEventListener("polyrob:activity", redraw);
  document.addEventListener("polyrob:tick", redraw);
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
}
