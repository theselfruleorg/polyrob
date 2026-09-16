// 043 WS-P1 / §5 — Work › Now & next and On a clock render a screen a person
// can act on, honestly.
//
// What it must get right is small and specific:
//
//   * a running goal renders with how long it has run AND a Stop it can act on;
//   * the Next count comes from status_counts, never from the goals array's
//     order (that array is list_recent, not the dispatcher's board.list);
//   * an unreadable store is a dashed entry with its reason, never a silent
//     drop and never a confident empty list;
//   * under read-only it draws no verb it cannot act on.
//
// It lives beside worklog.test.js because the vitest rig's root IS webview/dev/.
import { describe, it, expect } from "vitest";
import {
  activeTab,
  copyFrom,
  cronRow,
  elapsed,
  format,
  nextSummary,
  noticeEntry,
  queuedGoalEntry,
  cronRunEntry,
  sessionEntry,
  renderNowNext,
  renderSchedule,
  runningGoalEntry,
  showTab,
  unreadableEntry,
  workerEntry,
} from "../static/app/work-now.js";

const COPY = {
  loading: "Reading what Rob is doing.",
  empty_now: "Rob is not running anything right now.",
  empty_next: "Nothing is waiting its turn.",
  disabled: "Rob's own work is switched off.",
  unreadable: "I could not read the goal board.",
  unreadable_why: "Why it failed",
  running_count: "{count} running",
  next_queued: "{count} queued",
  elapsed_started: "started {elapsed} ago",
  stop: "Stop",
  pause: "Pause",
  drop: "Drop it",
  retry: "Try it again",
  helper_lead: "A helper is doing part of this for Rob.",
  helper_body: "Helpers cannot spend money or change your files.",
  helper_goal: "Working on {goal}",
  status_ready: "waiting its turn",
  status_waiting: "waiting",
  status_triage: "being sorted",
  status_blocked: "stopped after repeated failures",
  unreachable: "The console could not reach the server.",
  secs: "{count}s",
  mins: "{count}m",
  hours: "{count}h",
  days: "{count}d",
  sched_loading: "Reading the clock.",
  sched_empty: "Nothing runs on a clock yet.",
  sched_disabled: "Nothing runs on a clock here.",
  sched_unreadable: "I could not read the schedule.",
  sched_unreadable_why: "Why it failed",
  sched_cancel: "Cancel",
  sched_last_never: "not yet run",
  sched_ran_ago: "{elapsed} ago",
  sched_unreachable: "The console could not reach the server.",
};

const NOW_MS = 1_000_000_000_000; // fixed clock for deterministic elapsed

function nowCtx() {
  document.body.innerHTML =
    '<div class="ledger" id="run"></div><div class="ledger" id="next"></div>'
    + '<span id="ra"></span><span id="na"></span>';
  return {
    running: document.getElementById("run"),
    next: document.getElementById("next"),
    runningAside: document.getElementById("ra"),
    nextAside: document.getElementById("na"),
  };
}

function schedCtx() {
  document.body.innerHTML =
    '<div class="ledger" id="notice"></div>'
    + '<table id="table" hidden><tbody id="rows"></tbody></table>';
  return {
    notice: document.getElementById("notice"),
    table: document.getElementById("table"),
    rows: document.getElementById("rows"),
  };
}

describe("copy and durations cross from the server", () => {
  it("copyFrom is a plain object of the dataset", () => {
    const node = document.createElement("div");
    node.dataset.stop = "Stop";
    expect(copyFrom(node).stop).toBe("Stop");
  });

  it("format fills only the named fields it is given", () => {
    expect(format("{count} running", { count: 3 })).toBe("3 running");
    expect(format("started {elapsed} ago", { elapsed: "4m" }))
      .toBe("started 4m ago");
    // an unfilled field is left as-is, never blanked into a lie
    expect(format("{count} of {total}", { count: 2 })).toBe("2 of {total}");
  });

  it("elapsed counts s, m, h, d from copy templates", () => {
    expect(elapsed(30, COPY)).toBe("30s");
    expect(elapsed(120, COPY)).toBe("2m");
    expect(elapsed(7200, COPY)).toBe("2h");
    expect(elapsed(172800, COPY)).toBe("2d");
    expect(elapsed(NaN, COPY)).toBe("0s");
  });
});

describe("a running goal renders with elapsed and a Stop", () => {
  it("carries the title, how long it has run, and a Stop it can act on", () => {
    const goal = { id: "g1", title: "Build the app", status: "running",
                   created_at: NOW_MS / 1000 - 120 };
    const entry = runningGoalEntry(goal, COPY, NOW_MS, false);
    expect(entry.querySelector(".entry-title").textContent).toBe("Build the app");
    expect(entry.querySelector(".entry-body").textContent).toBe("started 2m ago");
    const stop = entry.querySelector("button[data-verb]");
    expect(stop.textContent).toBe("Stop");
    expect(stop.dataset.verb).toBe("cancel"); // Stop is the decisive verb
    expect(stop.dataset.id).toBe("g1");
    expect(entry.dataset.goalId).toBe("g1");
  });

  it("draws no Stop under read-only — an absent button, not a dead one", () => {
    const goal = { id: "g1", title: "x", status: "running", created_at: 0 };
    const entry = runningGoalEntry(goal, COPY, NOW_MS, true);
    expect(entry.querySelector("button")).toBeNull();
  });
});

describe("a helper is a live line, never a Stop", () => {
  it("names the goal it serves and how long it has run, with no verb", () => {
    const row = { delegation_id: "d1", goal: "the funding rates",
                  dispatched_at: NOW_MS / 1000 - 60 };
    const entry = workerEntry(row, COPY, NOW_MS);
    expect(entry.textContent).toContain("Working on the funding rates");
    expect(entry.textContent).toContain("started 1m ago");
    expect(entry.querySelector("button")).toBeNull();
  });
});

describe("the Next count is status_counts, never the goals array order", () => {
  it("sums the queued statuses from counts and ignores everything else", () => {
    // running/done/blocked are present and MUST NOT be counted as queued.
    const counts = { ready: 2, waiting: 1, triage: 1, running: 5, done: 99,
                     blocked: 3 };
    expect(nextSummary(counts, COPY)).toBe("4 queued");
  });

  it("is zero, honestly, when nothing is queued", () => {
    expect(nextSummary({ running: 2 }, COPY)).toBe("0 queued");
    expect(nextSummary(undefined, COPY)).toBe("0 queued");
  });

  it("renderNowNext's Next aside reads counts, not the length of goals", () => {
    const ctx = nowCtx();
    // Ten goal rows in the window, but counts says only 3 are queued: the aside
    // must say 3, proving it did not measure the array.
    const goals = Array.from({ length: 10 }, (_, i) =>
      ({ id: `g${i}`, title: `g${i}`, status: "ready", created_at: 0 }));
    renderNowNext(ctx, { enabled: true, goals, counts: { ready: 3 } },
                  { running: [] }, COPY, { nowMs: NOW_MS });
    expect(ctx.nextAside.textContent).toBe("3 queued");
  });
});

describe("Now & next distinguishes its answers", () => {
  it("goals switched off is a plain notice, not an error", () => {
    const ctx = nowCtx();
    const drew = renderNowNext(ctx, { enabled: false }, null, COPY);
    expect(drew).toBe("disabled");
    expect(ctx.running.textContent).toContain("switched off");
    expect(ctx.running.querySelector(".is-unknown")).toBeNull();
  });

  it("an unreadable board is a dashed entry with its reason", () => {
    const ctx = nowCtx();
    const drew = renderNowNext(
      ctx, { enabled: true, error: "OperationalError: locked" }, null, COPY);
    expect(drew).toBe("unreadable");
    const dashed = ctx.running.querySelector(".entry.is-unknown");
    expect(dashed).not.toBeNull();
    expect(dashed.querySelector(".entry-title").textContent)
      .toBe("I could not read the goal board.");
    expect(ctx.running.querySelector(".entry-meta").textContent)
      .toContain("locked");
  });

  it("splits running from queued, and counts each", () => {
    const ctx = nowCtx();
    const goals = [
      { id: "r1", title: "running one", status: "running", created_at: NOW_MS / 1000 },
      { id: "q1", title: "queued one", status: "ready", created_at: 0 },
      { id: "b1", title: "blocked one", status: "blocked", created_at: 0 },
    ];
    const drew = renderNowNext(
      ctx, { enabled: true, goals, counts: { ready: 1 } },
      { running: [{ delegation_id: "d1", goal: "a helper task", dispatched_at: NOW_MS / 1000 }] },
      COPY, { nowMs: NOW_MS });
    expect(drew).toBe("rows");
    // Now = one running goal + one helper.
    expect(ctx.running.querySelectorAll(".entry.is-running").length).toBe(2);
    expect(ctx.runningAside.textContent).toBe("2 running");
    // Next = the queued goal + the blocked (stopped) one.
    expect(ctx.next.querySelectorAll(".entry").length).toBe(2);
    expect(ctx.next.querySelector(".entry.is-stopped")).not.toBeNull();
    expect(ctx.nextAside.textContent).toBe("1 queued");
  });

  it("an empty but readable board is the empty state, not a dash", () => {
    const ctx = nowCtx();
    renderNowNext(ctx, { enabled: true, goals: [], counts: {} },
                  { running: [] }, COPY, { nowMs: NOW_MS });
    expect(ctx.running.textContent).toContain("not running anything");
    expect(ctx.next.textContent).toContain("Nothing is waiting");
    expect(ctx.running.querySelector(".is-unknown")).toBeNull();
  });

  // 2026-09-16 audit B1: prod's day is CRON runs, and Now said "not running
  // anything" mid-run. The live reader's cron runs and live sessions are
  // drawn as running entries and counted; a session a running goal already
  // owns is not drawn twice; an unreadable live source is named.
  it("draws cron runs and live sessions from the live reader, and counts them", () => {
    const ctx = nowCtx();
    const goals = [
      { id: "r1", title: "running one", status: "running", created_at: NOW_MS / 1000,
        session_id: "s-goal" },
    ];
    const live = {
      cron: [{ id: "c1", task: "SCOUT-ENTRY run", since: NOW_MS / 1000 - 90 }],
      sessions: [
        { session_id: "s-goal", task: "the goal's own session", status: "running" },
        { session_id: "s-cron", task: "EXIT-MONITOR run", status: "running",
          since: NOW_MS / 1000 - 30 },
      ],
      unreadable: {},
      count: 3,
    };
    renderNowNext(ctx, { enabled: true, goals, counts: {} }, { running: [] },
                  { ...COPY, cron_title: "A scheduled run is in progress.",
                    session_title: "A chat is in progress.", open_chat: "Open it",
                    live_unreadable: "Part of what is running could not be read.",
                    live_unreadable_why: "Why" },
                  { nowMs: NOW_MS, live });
    const running = ctx.running.querySelectorAll(".entry.is-running");
    expect(running.length).toBe(3); // goal + cron + the one session the goal does not own
    expect(ctx.runningAside.textContent).toBe("3 running");
    expect(ctx.running.textContent).toContain("SCOUT-ENTRY run");
    expect(ctx.running.textContent).toContain("EXIT-MONITOR run");
    expect(ctx.running.textContent).not.toContain("the goal's own session");
    const open = ctx.running.querySelector('a[href="/c/s-cron"]');
    expect(open).not.toBeNull();
    expect(open.textContent).toBe("Open it");
    expect(ctx.running.textContent).not.toContain("not running anything");
  });

  it("an unreadable live source is named, beside what could be read", () => {
    const ctx = nowCtx();
    renderNowNext(ctx, { enabled: true, goals: [], counts: {} }, { running: [] },
                  { ...COPY, live_unreadable: "Part of what is running could not be read.",
                    live_unreadable_why: "Why" },
                  { nowMs: NOW_MS,
                    live: { cron: [], sessions: null,
                            unreadable: { sessions: "FileNotFoundError: session_registry.db" },
                            count: 0 } });
    const dashed = ctx.running.querySelector(".entry.is-unknown");
    expect(dashed).not.toBeNull();
    expect(dashed.textContent).toContain("could not be read");
    expect(dashed.textContent).toContain("session_registry.db");
  });

  it("cronRunEntry and sessionEntry are running entries with elapsed", () => {
    const c = cronRunEntry({ id: "c1", task: "Digest", since: NOW_MS / 1000 - 120 },
                           { ...COPY, cron_title: "A scheduled run is in progress." }, NOW_MS);
    expect(c.className).toBe("entry is-running");
    expect(c.querySelector(".entry-title").textContent).toBe("A scheduled run is in progress.");
    expect(c.textContent).toContain("Digest");
    const s = sessionEntry({ session_id: "abc", task: "hello", status: "running" },
                           { ...COPY, session_title: "A chat is in progress.", open_chat: "Open it" }, NOW_MS);
    expect(s.dataset.sessionId).toBe("abc");
    expect(s.querySelector(".entry-actions a").getAttribute("href")).toBe("/c/abc");
  });

  it("a failed helper read is named beside the goals it could read", () => {
    const ctx = nowCtx();
    renderNowNext(
      ctx, { enabled: true, goals: [], counts: {} },
      { error: "TimeoutError: store busy" }, COPY, { nowMs: NOW_MS });
    expect(ctx.running.querySelector(".entry.is-unknown")).not.toBeNull();
    expect(ctx.running.textContent).toContain("store busy");
  });
});

describe("a queued goal offers the verb its state allows", () => {
  it("a blocked goal offers Try again and Drop, never a bare Pause", () => {
    const entry = queuedGoalEntry(
      { id: "b1", title: "x", status: "blocked" }, COPY, false);
    const verbs = [...entry.querySelectorAll("button[data-verb]")]
      .map((b) => b.dataset.verb);
    expect(verbs).toEqual(["retry", "cancel"]);
    expect(entry.className).toContain("is-stopped");
  });

  it("a ready goal offers Pause", () => {
    const entry = queuedGoalEntry(
      { id: "q1", title: "x", status: "ready" }, COPY, false);
    const btn = entry.querySelector("button[data-verb]");
    expect(btn.dataset.verb).toBe("pause");
    expect(entry.querySelector(".entry-body").textContent).toBe("waiting its turn");
  });

  it("read-only draws no verb", () => {
    const entry = queuedGoalEntry(
      { id: "q1", title: "x", status: "ready" }, COPY, true);
    expect(entry.querySelector("button")).toBeNull();
  });
});

describe("On a clock lists jobs and offers Cancel", () => {
  it("renders a job, its schedule, and a Cancel it can act on", () => {
    const ctx = schedCtx();
    const drew = renderSchedule(ctx, { enabled: true, jobs: [
      { id: "j1", task: "Morning check", schedule_spec: "every weekday 09:00",
        last_run_at: new Date(NOW_MS - 7200 * 1000).toISOString() },
    ] }, COPY, { nowMs: NOW_MS });
    expect(drew).toBe("rows");
    expect(ctx.table.hidden).toBe(false);
    const row = ctx.rows.querySelector("tr");
    expect(row.querySelector(".what").textContent).toContain("Morning check");
    expect(row.textContent).toContain("every weekday 09:00");
    expect(row.textContent).toContain("2h ago");
    const cancel = row.querySelector("button[data-cron]");
    expect(cancel.textContent).toBe("Cancel");
    expect(cancel.dataset.id).toBe("j1");
  });

  it("a job that never ran says so, it does not dash or invent a run", () => {
    const ctx = schedCtx();
    renderSchedule(ctx, { enabled: true, jobs: [
      { id: "j2", task: "Backup", schedule_spec: "every day 03:00", last_run_at: null },
    ] }, COPY, { nowMs: NOW_MS });
    expect(ctx.rows.querySelector("tr").textContent).toContain("not yet run");
  });

  it("cron off is a plain notice; a failed read is a dashed reason", () => {
    let ctx = schedCtx();
    expect(renderSchedule(ctx, { enabled: false }, COPY)).toBe("disabled");
    expect(ctx.table.hidden).toBe(true);
    expect(ctx.notice.textContent).toContain("Nothing runs on a clock here");

    ctx = schedCtx();
    expect(renderSchedule(ctx, { enabled: true, error: "IOError: gone" }, COPY))
      .toBe("unreadable");
    expect(ctx.notice.querySelector(".entry.is-unknown")).not.toBeNull();
    expect(ctx.notice.textContent).toContain("gone");

    ctx = schedCtx();
    expect(renderSchedule(ctx, { enabled: true, jobs: [] }, COPY)).toBe("empty");
    expect(ctx.notice.textContent).toContain("Nothing runs on a clock yet");
  });

  it("read-only lists jobs but offers no Cancel", () => {
    const ctx = schedCtx();
    renderSchedule(ctx, { enabled: true, jobs: [
      { id: "j1", task: "x", schedule_spec: "daily", last_run_at: null },
    ] }, COPY, { readOnly: true });
    expect(ctx.rows.querySelector("button")).toBeNull();
  });
});

describe("the tabs are panes in one page", () => {
  function frame() {
    document.body.innerHTML =
      '<nav class="subnav">'
      + '<a data-tab="now">Now</a><a data-tab="schedule">Clock</a>'
      + '<a data-tab="log">Log</a></nav>'
      + '<div data-pane="now"></div><div data-pane="schedule" hidden></div>'
      + '<div data-pane="log" hidden></div>';
    return {
      links: [...document.querySelectorAll(".subnav a[data-tab]")],
      panes: [...document.querySelectorAll("[data-pane]")],
    };
  }

  it("an unknown or empty hash is the first tab, never a blank screen", () => {
    expect(activeTab("", ["now", "schedule", "log"])).toBe("now");
    expect(activeTab("#nope", ["now", "schedule", "log"])).toBe("now");
    expect(activeTab("#schedule", ["now", "schedule", "log"])).toBe("schedule");
  });

  it("showTab reveals one pane, hides the rest, and moves aria-current", () => {
    const { links, panes } = frame();
    showTab("schedule", links, panes);
    const shown = panes.filter((p) => !p.hidden).map((p) => p.dataset.pane);
    expect(shown).toEqual(["schedule"]);
    const current = links.filter((a) => a.getAttribute("aria-current") === "page");
    expect(current.map((a) => a.dataset.tab)).toEqual(["schedule"]);
  });
});

describe("the honest-state primitives", () => {
  it("unreadableEntry is a dashed entry with the reason behind a disclosure", () => {
    const node = unreadableEntry("OSError: disk is on fire", COPY);
    expect(node.className).toContain("is-unknown");
    expect(node.querySelector(".entry-title").textContent)
      .toBe("I could not read the goal board.");
    expect(node.querySelector("details .entry-meta").textContent)
      .toContain("disk is on fire");
  });

  it("noticeEntry is a plain entry, never dashed", () => {
    const node = noticeEntry("nothing here");
    expect(node.className).toBe("entry");
    expect(node.querySelector(".entry-body").textContent).toBe("nothing here");
  });
});
