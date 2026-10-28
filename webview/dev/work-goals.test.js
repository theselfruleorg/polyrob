// Work › Goals — the goal board draws every goal by status, says when it could
// not read, offers only the existing verbs, and keeps its filters in the URL.
import { describe, it, expect } from "vitest";
import {
  costText, eventWords, filterQuery, filterState, groupChildren, matches,
  money, renderBoard, renderDetail, renderFiles, runLine, verbsFor,
} from "../static/app/work-goals.js";

const COPY = {
  loading: "Reading the goal board.", empty: "There are no goals on the board.",
  col_ask: "Your decisions", col_triage: "Being sorted", col_waiting: "Waiting for other goals",
  col_ready: "Ready", col_running: "Running", col_blocked: "Needs a fix", col_paused: "Paused",
  col_empty: "None", finished_title: "Finished", finished_summary: "{done} done, {cancelled} stopped",
  finished_none: "Nothing has finished yet.", show_more: "Show more",
  kind_goal: "Goal", kind_ask: "Decision",
  origin_owner: "You started it", origin_granted: "Granted by you: {name}",
  origin_granted_any: "Granted by you", origin_self: "Rob started it",
  origin_legacy: "Not recorded who started it", origin_asked: "You asked for it in a chat",
  added: "added {elapsed} ago", changed: "last change {elapsed} ago",
  cost: "last run {cost} of the {limit} limit", cost_no_limit: "last run {cost}",
  cost_none: "no AI cost recorded yet", waits_on: "Waits for: {goals}",
  waited_on_by: "Goals waiting for it: {goals}", held_by: "Waiting for your decision: {title}",
  holds_one: "One goal waits for this.", holds: "{count} goals wait for this.",
  unknown_goal: "a goal I cannot see", failures: "{count} of {max} tries failed",
  filtered_none: "No goal matches this filter.", part_unreadable: "Part of the board could not be read.",
  truncated: "There are more goals than this board shows.",
  disabled: "Rob's own work is switched off.", unreadable: "I could not read the goal board.",
  unreadable_why: "Why it failed", d_unreadable: "I could not read this goal.",
  stop: "Stop", pause: "Pause", drop: "Drop it", retry: "Try it again", resume: "Resume",
  yes: "Yes", no: "No", answer_label: "Your answer", answer_placeholder: "Type your answer",
  d_decide_inbox: "Decide this one in the Inbox.", d_runs: "Runs", d_runs_none: "No run recorded yet.",
  d_run_steps: "{count} steps", d_files: "Files", d_files_none: "No files recorded for its chats.",
  d_files_unreadable: "I could not read the files of one of its chats.",
  d_history: "History", d_history_none: "No history recorded.", d_what: "What it is",
  d_done_when: "Done when", d_checks: "Checks", d_decision: "Your decision",
  d_waits_on: "Waits for", d_waited_on_by: "Goals waiting for it", d_sub_goals: "Sub-goals",
  d_parent: "Part of", d_result: "Result", d_failure: "Last failure", d_tries: "Earlier tries",
  d_limits: "Each run may use up to {steps} steps and {limit} of AI cost.",
  open_chat: "Open it", event_created: "Added",
  secs: "{count}s", mins: "{count}m", hours: "{count}h", days: "{count}d",
};

const NOW = 1_800_000_000_000;
const T = NOW / 1000;
const COLUMNS = ["triage", "waiting", "ready", "running", "blocked", "paused"];

function g(id, status, extra = {}) {
  return { id, kind: "goal", title: `goal ${id}`, status, status_reason: `why ${id}`,
           origin: "owner", created_at: T - 7200, last_activity: T - 60, cost_usd: null,
           failures: 0, max_retries: 2, depends_on: [], blocks: [], held_by: [],
           parent_id: null, ...extra };
}

function board(extra = {}) {
  return {
    enabled: true, columns: COLUMNS, counts: { done: 1, cancelled: 0 },
    limits: { run_usd: 2, max_steps: 150 }, unreadable: {}, titles: {},
    goals: [
      g("t", "triage"), g("w", "waiting", { depends_on: ["r"] }),
      g("r", "running", { blocks: ["w"], cost_usd: 0.42, origin: "granted", origin_name: "digest" }),
      g("c", "running", { parent_id: "r" }), g("b", "blocked", { held_by: ["a1"], failures: 2 }),
      g("p", "paused"), g("q", "ready"),
    ],
    finished: [g("d", "done")], more_finished: true,
    asks: [{ id: "a1", kind: "ask", title: "Which page?", body: "", holds: ["b"],
             own_place: false, created_at: T - 3600 }],
    ...extra,
  };
}

function frame() {
  document.body.innerHTML = '<div id="n"></div><div id="b"></div><div id="f"></div>';
  return { notice: document.getElementById("n"), board: document.getElementById("b"),
           finished: document.getElementById("f") };
}

describe("the board", () => {
  it("draws the decisions lane then one lane per live status, in order", () => {
    const ctx = frame();
    expect(renderBoard(ctx, board(), COPY, { nowMs: NOW })).toBe("rows");
    const lanes = [...ctx.board.querySelectorAll("details.lane")].map((l) => l.dataset.lane);
    expect(lanes).toEqual(["ask", ...COLUMNS]);
    const lane = (name) => ctx.board.querySelector(`[data-lane="${name}"]`);
    expect(lane("blocked").textContent).toContain("Waiting for your decision: Which page?");
    expect(lane("blocked").textContent).toContain("2 of 2 tries failed");
    expect(lane("waiting").textContent).toContain("Waits for: goal r");
    expect(lane("running").textContent).toContain("Goals waiting for it: goal w");
    expect(lane("running").textContent).toContain("last run $0.42 of the $2.00 limit");
    expect(lane("running").textContent).toContain("Granted by you: digest");
    expect(lane("ready").textContent).toContain("no AI cost recorded yet");
    expect(lane("ask").textContent).toContain("One goal waits for this.");
  });

  it("groups a sub-goal inside its parent only when they share a lane", () => {
    const ctx = frame();
    renderBoard(ctx, board(), COPY, { nowMs: NOW });
    const running = ctx.board.querySelector('[data-lane="running"] .ledger');
    expect(running.children.length).toBe(1);
    expect(running.querySelector(".card-group [data-goal-id=c]")).not.toBeNull();
    const { top, children } = groupChildren([g("x", "ready"), g("y", "blocked", { parent_id: "x" })]);
    expect(top.length).toBe(2);
    expect(children).toEqual({});
  });

  it("a parent loop never hides its goals", () => {
    const { top } = groupChildren([g("x", "ready", { parent_id: "y" }),
                                   g("y", "ready", { parent_id: "x" })]);
    expect(top.map((r) => r.id).sort()).toEqual(["x", "y"]);
  });

  it("a folded lane stays folded on a redraw, and a server answer is shown", () => {
    const ctx = frame();
    renderBoard(ctx, board(), COPY, { nowMs: NOW, closed: new Set(["paused"]),
                                      flash: "Paused goal p." });
    expect(ctx.board.querySelector('[data-lane="paused"]').open).toBe(false);
    expect(ctx.board.querySelector('[data-lane="running"]').open).toBe(true);
    expect(ctx.notice.textContent).toContain("Paused goal p.");
  });

  it("a goal with no recorded time says nothing about its age (never 1970)", () => {
    const ctx = frame();
    renderBoard(ctx, board({ goals: [g("z", "ready", { created_at: null, last_activity: 0 })] }),
                COPY, { nowMs: NOW });
    const card = ctx.board.querySelector("[data-goal-id=z]").textContent;
    expect(card).not.toContain("added");
    expect(card).not.toContain("last change");
  });

  it("offers only the existing verbs, and none read-only", () => {
    expect(verbsFor("running")).toEqual(["pause", "stop"]);
    expect(verbsFor("blocked")).toEqual(["retry", "stop", "drop"]);
    expect(verbsFor("paused")).toEqual(["resume", "drop"]);
    expect(verbsFor("done")).toEqual([]);
    const ctx = frame();
    renderBoard(ctx, board(), COPY, { nowMs: NOW });
    const paused = ctx.board.querySelector('[data-goal-id=p]');
    expect([...paused.querySelectorAll("button[data-verb]")].map((b) => b.dataset.verb))
      .toEqual(["retry", "drop"]);                      // Resume is retry
    const ro = frame();
    renderBoard(ro, board(), COPY, { nowMs: NOW, readOnly: true });
    expect(ro.board.querySelector("button[data-verb], button[data-decide]")).toBeNull();
  });

  it("an unreadable board is a dashed entry with its reason, never empty lanes", () => {
    const ctx = frame();
    expect(renderBoard(ctx, { error: "goals.db is locked" }, COPY, { nowMs: NOW })).toBe("unreadable");
    expect(ctx.notice.querySelector(".is-unknown").textContent).toContain("I could not read the goal board.");
    expect(ctx.notice.textContent).toContain("goals.db is locked");
    expect(ctx.board.children.length).toBe(0);
  });

  it("names a failed side read, and the truncation, and still draws", () => {
    const ctx = frame();
    renderBoard(ctx, board({ unreadable: { dependencies: "locked" }, truncated: true }), COPY, { nowMs: NOW });
    expect(ctx.notice.textContent).toContain("Part of the board could not be read.");
    expect(ctx.notice.textContent).toContain("dependencies: locked");
    expect(ctx.notice.textContent).toContain("There are more goals than this board shows.");
    expect(ctx.board.querySelectorAll("[data-goal-id]").length).toBeGreaterThan(0);
  });

  it("an empty board says so; the off switch is drawn above the goals", () => {
    const ctx = frame();
    expect(renderBoard(ctx, board({ goals: [], finished: [], asks: [] }), COPY, { nowMs: NOW })).toBe("empty");
    expect(ctx.notice.textContent).toContain("There are no goals on the board.");
    const off = frame();
    renderBoard(off, board({ enabled: false }), COPY, { nowMs: NOW });
    expect(off.notice.textContent).toContain("switched off");
    expect(off.board.querySelectorAll("[data-goal-id]").length).toBeGreaterThan(0);
  });

  it("folds the finished goals away until Show finished, with Show more", () => {
    const ctx = frame();
    renderBoard(ctx, board(), COPY, { nowMs: NOW });
    const fin = ctx.finished.querySelector('[data-lane="finished"]');
    expect(fin.open).toBe(false);
    expect(fin.textContent).toContain("1 done, 0 stopped");
    expect(fin.querySelector("button[data-more]")).not.toBeNull();
    const open = frame();
    renderBoard(open, board(), COPY, { nowMs: NOW, filter: { ...filterState(""), finished: true } });
    expect(open.finished.querySelector('[data-lane="finished"]').open).toBe(true);
  });
});

describe("the filters", () => {
  it("round-trip through the URL and drop unknown values", () => {
    const f = filterState("?q=digest&kind=goal&source=granted&finished=1&goal=r");
    expect(f).toEqual({ q: "digest", kind: "goal", source: "granted", finished: true, goal: "r" });
    expect(filterState(filterQuery(f))).toEqual(f);
    expect(filterState("?kind=nope&source=x")).toEqual(
      { q: "", kind: "", source: "", finished: false, goal: "" });
    expect(filterQuery(filterState(""))).toBe("");
  });

  it("match on text, kind and source", () => {
    const base = filterState("");
    const r = g("r", "running", { origin: "granted" });
    expect(matches(r, { ...base, q: "goal R" })).toBe(true);
    expect(matches(r, { ...base, q: "nothing" })).toBe(false);
    expect(matches(r, { ...base, kind: "ask" })).toBe(false);
    expect(matches(r, { ...base, source: "owner" })).toBe(false);
    expect(matches({ kind: "ask", title: "x" }, { ...base, source: "owner" })).toBe(false);
  });

  it("a filter that hides everything says so", () => {
    const ctx = frame();
    const out = renderBoard(ctx, board(), COPY, { nowMs: NOW, filter: { ...filterState(""), q: "zzz" } });
    expect(out).toBe("filtered");
    expect(ctx.notice.textContent).toContain("No goal matches this filter.");
  });
});

describe("the detail", () => {
  const detail = {
    ok: true, id: "b", title: "goal b", status: "blocked", origin: "owner",
    status_reason: "it needs something from you", body: "Fix the page.",
    acceptance: "the page loads", acceptance_checks: [], held_by: ["a1"],
    decisions: [{ id: "a1", title: "Which page?", own_place: false }],
    waits_on: [{ id: "x", title: "goal x", status: "done" }], waited_on_by: [],
    sub_goals: [], parent: null, result: null, last_failure_error: "boom", attempts: [],
    runs: [{ ts: T, outcome: "failed", steps: 4, spend_usd: 0.1, stop_text: "stopped at the step limit",
             session_id: "s1", reason: "boom" }],
    events: [{ kind: "created", ts: T }, { kind: "made_up_kind", ts: T }],
    sessions: ["s1"], limits: { run_usd: 2, max_steps: 150 }, cost_usd: 0.1, unreadable: {},
  };

  it("shows what it is, the decision, the runs and the history", () => {
    document.body.innerHTML = '<div id="d"></div>';
    const body = document.getElementById("d");
    expect(renderDetail(body, detail, COPY)).toBe("rows");
    const text = body.textContent;
    expect(text).toContain("Fix the page.");
    expect(text).toContain("the page loads");
    expect(text).toContain("Which page?");
    expect(body.querySelector("button[data-decide=decide]")).not.toBeNull();
    expect(text).toContain("4 steps");
    expect(text).toContain("stopped at the step limit");
    expect(body.querySelector('a[href="/c/s1"]')).not.toBeNull();
    expect(text).toContain("Added");
    expect(text).toContain("made up kind");               // an unknown kind is kept
    expect([...body.querySelectorAll("button[data-verb]")].map((b) => b.dataset.verb))
      .toEqual(["retry", "stop", "drop"]);
  });

  it("a decision with its own place links to the Inbox", () => {
    document.body.innerHTML = '<div id="d"></div>';
    const body = document.getElementById("d");
    renderDetail(body, { ...detail, decisions: [{ id: "a2", title: "Spend $5?", own_place: true }] }, COPY);
    expect(body.querySelector('a[href="/inbox"]').textContent).toBe("Decide this one in the Inbox.");
  });

  it("an unreadable goal says so", () => {
    document.body.innerHTML = '<div id="d"></div>';
    const body = document.getElementById("d");
    expect(renderDetail(body, { error: "locked" }, COPY)).toBe("unreadable");
    expect(body.textContent).toContain("I could not read this goal.");
  });

  it("files: listed, none, or named unreadable", () => {
    document.body.innerHTML = '<div id="d"></div>';
    const box = document.getElementById("d");
    renderFiles(box, [{ body: { artifacts: [{ name: "report.md", kind: "report" }] } }], COPY);
    expect(box.textContent).toContain("report.md");
    renderFiles(box, [{ body: { artifacts: [] } }], COPY);
    expect(box.textContent).toContain("No files recorded");
    renderFiles(box, [{ body: { artifacts: null, error: "unreadable" } }], COPY);
    expect(box.textContent).toContain("I could not read the files");
  });
});

describe("the small words", () => {
  it("money keeps a sub-cent cost, and a missing cost is not $0", () => {
    expect(money(0.004)).toBe("$0.004");
    expect(money(1.5)).toBe("$1.50");
    expect(costText({ cost_usd: null }, { run_usd: 2 }, COPY)).toBe("no AI cost recorded yet");
    expect(costText({ cost_usd: 0.5 }, { run_usd: 0 }, COPY)).toBe("last run $0.50");
  });

  it("event and run lines", () => {
    expect(eventWords("created", COPY)).toBe("Added");
    expect(eventWords("deps_satisfied", COPY)).toBe("deps satisfied");
    expect(runLine({ ts: null, outcome: "done", steps: 3, spend_usd: null }, COPY)).toBe("done · 3 steps");
  });
});
