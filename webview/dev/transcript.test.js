// 043 A14/A16 — the running transcript draws turns, narrated actions and a
// receipt a person can Stop or Steer from.
//
// What it must get right is small and specific:
//
//   * ONE narrator. The human action line is computed in Python and shipped on
//     the event (`data.narration`); this module renders that string and never
//     re-implements the narrator. Its only fallbacks are the scrubbed preview
//     and, last, a name-free copy line — never a tool id, never a `[step]` token.
//   * an action is ONE row, keyed by its call id: running then done UPDATES the
//     row, it does not draw a second one.
//   * Stop and Steer add no authority — they are the existing cancel and message
//     endpoints, and they ride `http.js::postJson` (`credentials:'include'`).
//   * a byte-identical repeat bubble within a turn is suppressed (the R2 rule
//     the CLI renderer also keeps).
//   * an unknown cost is a dash, never `$0.00`.
//
// ⚠️ Lives beside chats.test.js: the vitest rig's root IS `webview/dev/` and its
// include glob is rooted there — a file outside it would never run.
import { describe, it, expect, beforeEach } from "vitest";
import {
  Transcript,
  chronoTs,
  costLabel,
  elapsedWords,
  mmss,
  narrationLine,
} from "../static/app/transcript.js";

const COPY = {
  stop: "Stop",
  steer: "Steer",
  receipt_working: "Working {elapsed}, {actions} actions so far, {cost}.",
  receipt_done: "Worked {elapsed}, took {actions} actions, cost {cost}.",
  receipt_stopped: "Stopped after {elapsed}, having taken {actions} actions, cost {cost}.",
  cost_unknown: "—",
  act_did_work: "Ran a tool",
  act_failed: "An action did not finish",
};

/** A fetcher that records every call, the http.test.js pattern. */
function recorder({ ok = true, status = 200, body = { ok: true } } = {}) {
  const calls = [];
  const fetcher = async (url, init) => {
    calls.push({ url, init });
    return { ok, status, json: async () => body };
  };
  return { fetcher, calls };
}

function makeThread() {
  document.body.innerHTML = '<div class="thread" id="chat-messages"></div>';
  return document.getElementById("chat-messages");
}

/** Flush the pending microtask + macrotask queue an async click handler uses. */
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("the formatters", () => {
  it("clocks an action in M:SS", () => {
    expect(mmss(4)).toBe("0:04");
    expect(mmss(72)).toBe("1:12");
    expect(mmss(0)).toBe("0:00");
    expect(mmss(-5)).toBe("0:00");
  });

  it("phrases the receipt's elapsed in words", () => {
    expect(elapsedWords(41000)).toBe("41s");
    expect(elapsedWords(72000)).toBe("1m 12s");
    expect(elapsedWords(60000)).toBe("1m");
  });

  it("shows a cost only when it read one, a dash otherwise", () => {
    expect(costLabel(0.02)).toBe("$0.02");
    expect(costLabel(null)).toBe("—");
    expect(costLabel(undefined)).toBe("—");
    expect(costLabel(NaN)).toBe("—");
    // never a confident $0.00 for an unknown cost
    expect(costLabel(null)).not.toBe("$0.00");
  });
});

describe("the narration line", () => {
  it("prefers the server-computed narration", () => {
    expect(narrationLine({ narration: "Ran the test suite and all 18 passed", result_preview: "raw" }, COPY))
      .toBe("Ran the test suite and all 18 passed");
  });

  it("falls back to the scrubbed preview, first line only", () => {
    expect(narrationLine({ result_preview: "line one\nline two" }, COPY)).toBe("line one");
  });

  it("falls back to a name-free copy line, never a machine name", () => {
    expect(narrationLine({}, COPY)).toBe("Ran a tool");
    expect(narrationLine({ success: false }, COPY)).toBe("An action did not finish");
  });
});

describe("the transcript", () => {
  let thread;
  let clock;
  const now = () => clock;
  beforeEach(() => {
    thread = makeThread();
    clock = 0;
  });

  it("draws a person's turn as a bubble", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.apply({ type: "user_message", timestamp: 1, data: { text: "Build it, then deploy." } });
    const you = thread.querySelector(".turn.turn-you .bubble");
    expect(you).not.toBe(null);
    expect(you.textContent).toBe("Build it, then deploy.");
  });

  it("draws one action row, and running→done updates it in place", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.apply({ type: "user_message", timestamp: 1, data: { text: "go" } });
    clock = 4000;
    tx.apply({ type: "tool_result", data: { call_id: "c1", narration: "Building the container" } });
    let acts = thread.querySelectorAll(".act");
    expect(acts.length).toBe(1);
    expect(acts[0].classList.contains("is-running")).toBe(true);
    expect(acts[0].querySelector(".act-what").textContent).toBe("Building the container");

    clock = 72000;
    tx.apply({ type: "tool_result", data: { call_id: "c1", success: true, narration: "Deployed the app" } });
    acts = thread.querySelectorAll(".act");
    expect(acts.length).toBe(1); // same call id → same row, not a second
    expect(acts[0].classList.contains("is-running")).toBe(false);
    expect(acts[0].querySelector(".act-what").textContent).toBe("Deployed the app");
  });

  it("marks a failed action stopped", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.apply({ type: "user_message", timestamp: 1, data: { text: "go" } });
    tx.apply({ type: "tool_result", data: { call_id: "c1", success: false, narration: "An action did not finish" } });
    const act = thread.querySelector(".act");
    expect(act.classList.contains("is-stopped")).toBe(true);
  });

  it("draws the agent's reply and a receipt with Stop and Steer while live", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.apply({ type: "user_message", timestamp: 1, data: { text: "go" } });
    tx.apply({ type: "tool_result", data: { call_id: "c1", narration: "Working" } });
    tx.apply({ type: "agent_message", timestamp: 2, data: { text: "On it." } });
    const said = thread.querySelector(".turn-rob .said");
    expect(said.textContent).toContain("On it.");
    const receipt = thread.querySelector(".receipt");
    expect(receipt.hidden).toBe(false);
    const buttons = [...receipt.querySelectorAll("button")].map((b) => b.textContent);
    expect(buttons).toContain("Stop");
    expect(buttons).toContain("Steer");
    // the receipt counted one action and, having read no cost, shows a dash
    expect(receipt.textContent).toContain("1 actions");
    expect(receipt.textContent).toContain("—");
    expect(receipt.textContent).not.toContain("$0.00");
  });

  it("shows a cost only once it reads one", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.apply({ type: "user_message", timestamp: 1, data: { text: "go" } });
    tx.apply({ type: "tool_result", data: { call_id: "c1", narration: "Working" } });
    tx.apply({ type: "llm_request", data: { cost_estimate: 0.02 } });
    expect(thread.querySelector(".receipt").textContent).toContain("$0.02");
  });

  it("suppresses a byte-identical repeat bubble within a turn (R2)", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.apply({ type: "user_message", timestamp: 1, data: { text: "go" } });
    tx.apply({ type: "agent_message", timestamp: 2, data: { text: "Same line." } });
    tx.apply({ type: "agent_message", timestamp: 3, data: { text: "Same line." } });
    const paras = thread.querySelectorAll(".turn-rob .said p");
    expect(paras.length).toBe(1);
  });

  it("is idempotent: the backfill and the socket can carry the same event", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    const ev = { type: "user_message", timestamp: 1, data: { text: "go" } };
    tx.apply(ev);
    tx.apply(ev);
    expect(thread.querySelectorAll(".turn-you").length).toBe(1);
  });

  it("Stop calls the cancel endpoint, same-origin, with the cookie", async () => {
    const { fetcher, calls } = recorder();
    const tx = new Transcript(thread, COPY, { sessionId: "abc", now, fetcher });
    tx.apply({ type: "user_message", timestamp: 1, data: { text: "go" } });
    tx.apply({ type: "tool_result", data: { call_id: "c1", narration: "Working" } });
    const stop = [...thread.querySelectorAll(".receipt button")].find((b) => b.textContent === "Stop");
    stop.click(); // dispatches the addEventListener handler (async this.stop())
    await flush();
    expect(calls.length).toBeGreaterThan(0);
    expect(calls[0].url).toBe("/api/task/sessions/abc/cancel");
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].init.credentials).toBe("include");
  });

  it("Steer sends the composer text to the message endpoint", async () => {
    const { fetcher, calls } = recorder();
    const input = document.createElement("textarea");
    input.value = "wait, do the tests first";
    const tx = new Transcript(thread, COPY, { sessionId: "abc", now, fetcher, input });
    tx.apply({ type: "user_message", timestamp: 1, data: { text: "go" } });
    tx.apply({ type: "tool_result", data: { call_id: "c1", narration: "Working" } });
    const steer = [...thread.querySelectorAll(".receipt button")].find((b) => b.textContent === "Steer");
    steer.click();
    await flush();
    expect(calls[0].url).toBe("/api/session/abc/messages");
    expect(JSON.parse(calls[0].init.body).text).toBe("wait, do the tests first");
    expect(input.value).toBe("");
  });

  it("a person's turn closes the agent's turn — the next action opens a fresh one", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.apply({ type: "user_message", timestamp: 1, data: { text: "one" } });
    tx.apply({ type: "tool_result", data: { call_id: "c1", narration: "a" } });
    tx.apply({ type: "user_message", timestamp: 2, data: { text: "two" } });
    tx.apply({ type: "tool_result", data: { call_id: "c2", narration: "b" } });
    expect(thread.querySelectorAll(".turn-rob").length).toBe(2);
  });
});

// I-1 — a reopened chat's backfill is drawn CHRONOLOGICALLY, not in the
// endpoint's filename order. GET /feed/events sorts by filename, which mixes
// telemetry `{seq}_{type}.json` (float-seconds timestamp, has _seq) and
// add_to_feed `{type}_{ms}.json` (no _seq) — a name sort clusters by TYPE, which
// would put every user question below the answer. applyAll must re-sort by time.
describe("the backfill order", () => {
  let thread;
  const now = () => 0;
  beforeEach(() => {
    thread = makeThread();
  });

  it("normalises a millisecond timestamp to seconds before sorting", () => {
    expect(chronoTs({ timestamp: 1726000000 })).toBe(1726000000); // seconds, kept
    expect(chronoTs({ timestamp: 1726000000000 })).toBe(1726000000); // ms, halved down
    expect(chronoTs({})).toBe(0);
  });

  it("draws a filename-clustered backfill in time order (question before answer)", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    // Input order mimics the endpoint's filename clustering: replies grouped,
    // then a tool row, then every user turn at the bottom. Timestamps are real
    // magnitudes; "question one" arrives in the MILLISECOND scheme on purpose.
    tx.applyAll([
      { type: "agent_message", timestamp: 1726000010.0, data: { text: "answer two" } },
      { type: "agent_message", timestamp: 1726000004.0, data: { text: "answer one" } },
      { type: "status", timestamp: 1726000001000, _seq: 1, data: { status: "running" } },
      { type: "tool_result", timestamp: 1726000003.0, data: { call_id: "c1", narration: "did a thing" } },
      { type: "user_message", timestamp: 1726000000000, data: { text: "question one" } },
      { type: "user_message", timestamp: 1726000006.0, data: { text: "question two" } },
    ]);
    const turns = [...thread.querySelectorAll(".turn")];
    expect(turns.length).toBe(4);
    expect(turns[0].classList.contains("turn-you")).toBe(true);
    expect(turns[0].textContent).toContain("question one");
    expect(turns[1].classList.contains("turn-rob")).toBe(true);
    expect(turns[1].textContent).toContain("did a thing");
    expect(turns[1].textContent).toContain("answer one");
    expect(turns[2].classList.contains("turn-you")).toBe(true);
    expect(turns[2].textContent).toContain("question two");
    expect(turns[3].textContent).toContain("answer two");
  });
});

// I-2 — a live tool_result (narration) and tool_execution (no narration) arrive
// in the same watcher batch, unordered, and collapse to one act row by call_id.
// The narrated line must survive regardless of which lands last.
describe("narration survives the untyped tool_execution", () => {
  let thread;
  const now = () => 0;
  beforeEach(() => {
    thread = makeThread();
  });

  it("keeps the narration when tool_execution follows tool_result", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.apply({ type: "user_message", timestamp: 1, data: { text: "go" } });
    tx.apply({ type: "tool_result", data: { call_id: "c1", success: true, narration: "Ran the test suite and all 18 passed" } });
    tx.apply({ type: "tool_execution", data: { call_id: "c1", success: true, result_preview: "raw preview line" } });
    const acts = thread.querySelectorAll(".act");
    expect(acts.length).toBe(1); // still one row
    expect(acts[0].querySelector(".act-what").textContent).toBe("Ran the test suite and all 18 passed");
  });

  it("upgrades to the narration when tool_execution precedes tool_result", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.apply({ type: "user_message", timestamp: 1, data: { text: "go" } });
    tx.apply({ type: "tool_execution", data: { call_id: "c1", success: true, result_preview: "raw preview line" } });
    tx.apply({ type: "tool_result", data: { call_id: "c1", success: true, narration: "Ran the test suite and all 18 passed" } });
    const acts = thread.querySelectorAll(".act");
    expect(acts.length).toBe(1);
    expect(acts[0].querySelector(".act-what").textContent).toBe("Ran the test suite and all 18 passed");
  });
});

// A32 — a Steer that comes back 409 is an HONEST STATE, not a failure. The
// session is live in Rob's OWN process, not this console, so the console cannot
// steer it. The transcript renders the copy line (with owner_pid + a retry
// hint) once, as a `.note` — never a `.turn-rob` failure bubble, never a
// confident $0.00-shaped lie about the pid.
describe("the 409 remote-session state", () => {
  let thread;
  const now = () => 0;
  const COPY_409 = {
    ...COPY,
    live_at_agent:
      "This chat is live in Rob's own process (pid {pid}), so I cannot steer it. Try again in a moment.",
  };
  beforeEach(() => {
    thread = makeThread();
  });

  it("renders the live-in-the-agent line with owner_pid, never a failure bubble", async () => {
    const { fetcher } = recorder({
      ok: false,
      status: 409,
      body: { success: false, error: "…", detail: { session_id: "abc", owner_pid: 4242 } },
    });
    const tx = new Transcript(thread, COPY_409, { sessionId: "abc", now, fetcher });
    const res = await tx.send("do the tests first");
    expect(res.status).toBe(409);
    const note = thread.querySelector(".note");
    expect(note).not.toBe(null);
    expect(note.textContent).toContain("4242"); // the owner_pid
    expect(note.textContent).toContain("Try again"); // the retry hint
    // It is a state, not the agent speaking or a failure.
    expect(thread.querySelector(".turn-rob")).toBe(null);
    expect(note.textContent).not.toContain("{pid}");
  });

  it("Stop returning 409 renders the live-at-agent notice, not a silent no-op", async () => {
    const { fetcher } = recorder({
      ok: false,
      status: 409,
      body: { detail: { session_id: "abc", owner_pid: 4242 } },
    });
    const tx = new Transcript(thread, COPY_409, { sessionId: "abc", now, fetcher });
    // A live turn is open, and yet a remote 409 must NOT flip it to "stopped".
    tx.apply({ type: "tool_result", data: { call_id: "c1", narration: "Working" } });
    await tx.stop();
    const note = thread.querySelector(".note");
    expect(note).not.toBe(null);
    expect(note.textContent).toContain("4242"); // the owner_pid
    expect(note.textContent).toContain("Try again"); // the retry hint
    // The turn is still live at the agent — the console did not stop it.
    expect(tx.turn.state).toBe("working");
    expect(thread.querySelector(".act.is-stopped")).toBe(null);
  });

  it("reads owner_pid from a bare FastAPI 409 shape too", async () => {
    const { fetcher } = recorder({
      ok: false,
      status: 409,
      body: { detail: { session_id: "abc", owner_pid: 909 } },
    });
    const tx = new Transcript(thread, COPY_409, { sessionId: "abc", now, fetcher });
    await tx.send("steer it");
    expect(thread.querySelector(".note").textContent).toContain("909");
  });

  it("shows a dash for an unknown pid rather than a confident number", async () => {
    const { fetcher } = recorder({ ok: false, status: 409, body: { detail: {} } });
    const tx = new Transcript(thread, COPY_409, { sessionId: "abc", now, fetcher });
    await tx.send("steer it");
    const note = thread.querySelector(".note");
    expect(note.textContent).toContain("—");
    expect(note.textContent).not.toContain("{pid}");
  });

  it("reuses one note across repeated 409s rather than stacking them", async () => {
    const { fetcher } = recorder({ ok: false, status: 409, body: { detail: { owner_pid: 7 } } });
    const tx = new Transcript(thread, COPY_409, { sessionId: "abc", now, fetcher });
    await tx.send("one");
    await tx.send("two");
    expect(thread.querySelectorAll(".note").length).toBe(1);
  });

  it("draws no note on a normal 200 send", async () => {
    const { fetcher } = recorder({ ok: true, status: 200, body: { success: true } });
    const tx = new Transcript(thread, COPY_409, { sessionId: "abc", now, fetcher });
    await tx.send("go");
    expect(thread.querySelector(".note")).toBe(null);
  });
});
