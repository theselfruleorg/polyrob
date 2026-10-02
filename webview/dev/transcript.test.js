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
  eventMs,
  mmss,
  narrationLine,
} from "../static/app/transcript.js";

const COPY = {
  stop: "Stop",
  steer: "Steer",
  receipt_working: "Working for {elapsed}. {steps} so far.",
  receipt_done: "Done in {elapsed} with {steps}. {cost}",
  receipt_stopped: "Stopped after {elapsed} and {steps}. {cost}",
  receipt_so_far: "{elapsed} and {steps} before your message.",
  receipt_waiting: "Waiting for your answer. {elapsed} so far.",
  receipt_failed: "This did not finish. It failed after {elapsed} and {steps}.",
  receipt_ended: "Stopped after {elapsed} and {steps}, without a final word.",
  turn_failed: "This chat failed before Rob could answer.",
  steps_none: "no steps",
  steps_one: "1 step",
  steps_many: "{count} steps",
  cost_known: "Cost {amount}.",
  cost_unknown: "Cost not known yet.",
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

  it("draws a late console verb answer as a note, once, never an agent bubble", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    const ev = { type: "command_reply", timestamp: 7, data: { text: "Sent 0.1 ETH." } };
    tx.apply(ev);
    tx.apply(ev); // the socket may repeat it
    const notes = thread.querySelectorAll('.note[data-command-reply="1"]');
    expect(notes.length).toBe(1);
    expect(notes[0].textContent).toBe("Sent 0.1 ETH.");
    expect(thread.querySelector(".turn.turn-rob")).toBe(null);
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
    // the receipt counted one step and, having read no cost, a working
    // receipt names no cost at all
    expect(receipt.textContent).toContain("1 step so far");
    expect(receipt.textContent).not.toContain("$0.00");
  });

  it("shows a cost only once it reads one", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.apply({ type: "user_message", timestamp: 1, data: { text: "go" } });
    tx.apply({ type: "tool_result", data: { call_id: "c1", narration: "Working" } });
    tx.apply({ type: "llm_request", data: { cost_estimate: 0.02 } });
    // A working receipt names no cost; the closed one does.
    expect(thread.querySelector(".receipt").textContent).not.toContain("$0.02");
    tx.apply({ type: "status", data: { status: "completed" } });
    expect(thread.querySelector(".receipt").textContent).toContain("Cost $0.02.");
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

// Action cards (core/surfaces/cards.py): a money quote answered by the console
// carries its card's tap tokens; they become buttons that SEND the token as if
// typed — never for an answer to any other verb.
import { cardTaps, cardLabel } from "../static/app/transcript.js";

describe("action card buttons", () => {
  const QUOTE = "Confirm: /card_0123456789_ok   Refresh: /card_0123456789_re   Cancel: /card_0123456789_no";

  it("reads the taps only from an answer to a card verb", () => {
    expect(cardTaps("/send 1 native to 0xabc on base", QUOTE).map((t) => t.act))
      .toEqual(["ok", "re", "no"]);
    expect(cardTaps("/card_0123456789_re", QUOTE).length).toBe(3);
    expect(cardTaps("/thread", QUOTE)).toEqual([]);   // quoted words are never buttons
    expect(cardTaps("hello", QUOTE)).toEqual([]);
    const step = [1, 2, 3, 4, 5, 6].map((n) => `/card_0123456789_${n}`).join(" ")
      + " Cancel: /card_0123456789_no";
    expect(cardTaps("/card_0123456789_1", step).map((t) => t.act).pop()).toBe("no");
  });

  it("labels come from the copy node", () => {
    const copy = { card_ok: "Confirm", card_pick: "Pick {n}" };
    expect(cardLabel("ok", copy)).toBe("Confirm");
    expect(cardLabel("2", copy)).toBe("Pick 2");
  });

  it("a button posts the tap token and the row goes after one tap", async () => {
    const thread = document.createElement("div");
    const calls = [];
    const fetcher = async (url, init) => {
      calls.push({ url, body: JSON.parse(init.body) });
      return new Response(JSON.stringify({ success: true, command_reply: "Sent." }),
        { status: 200, headers: { "Content-Type": "application/json" } });
    };
    const tx = new Transcript(thread, { ...COPY, card_ok: "Confirm", card_re: "Refresh",
                                        card_no: "Cancel" }, { sessionId: "s1", fetcher });
    const note = tx._commandNote({ command_reply: QUOTE });
    const row = tx._cardButtons(note, "/send 1 native to 0xabc on base",
                                { command_reply: QUOTE });
    const buttons = row.querySelectorAll("button");
    expect([...buttons].map((b) => b.textContent)).toEqual(["Confirm", "Refresh", "Cancel"]);
    buttons[0].click();
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    expect(calls[0].body.text).toBe("/card_0123456789_ok");
    expect(thread.querySelector('[data-card-taps="1"]')).toBe(null);
  });
});


// 070 W0.5 — act and receipt clocks come from the events' own timestamps, not
// the render time: a backfill applies a whole chat in one tick.
describe("clocks from event timestamps", () => {
  let thread;
  const T0 = 1790000000; // seconds
  const renderNow = () => (T0 + 9999) * 1000; // the render time: far later
  beforeEach(() => {
    thread = makeThread();
  });

  it("eventMs reads seconds and ms timestamps, else the fallback", () => {
    expect(eventMs({ timestamp: T0 }, 5)).toBe(T0 * 1000);
    expect(eventMs({ timestamp: T0 * 1000 }, 5)).toBe(T0 * 1000);
    expect(eventMs({}, 5)).toBe(5);
  });

  it("a backfilled turn shows the real act offsets", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now: renderNow });
    tx.applyAll([
      { type: "user_message", timestamp: T0 - 1, data: { text: "go" } },
      { type: "tool_result", timestamp: T0, data: { call_id: "a", success: true, narration: "one" } },
      { type: "tool_result", timestamp: T0 + 6, data: { call_id: "b", success: true, narration: "two" } },
      { type: "tool_result", timestamp: T0 + 72, data: { call_id: "c", success: true, narration: "three" } },
    ]);
    const times = [...thread.querySelectorAll(".act-time")].map((n) => n.textContent);
    expect(times).toEqual(["0:00", "0:06", "1:12"]);
  });

  it("a closed backfilled receipt says the real duration", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now: renderNow });
    tx.applyAll([
      { type: "tool_result", timestamp: T0, data: { call_id: "a", success: true, narration: "one" } },
      { type: "tool_result", timestamp: T0 + 6, data: { call_id: "b", success: true, narration: "two" } },
      { type: "tool_result", timestamp: T0 + 72, data: { call_id: "c", success: true, narration: "three" } },
      { type: "status", timestamp: T0 + 72, data: { status: "completed" } },
    ]);
    expect(thread.querySelector(".receipt").textContent)
      .toBe("Done in 1m 12s with 3 steps. Cost not known yet.");
  });

  it("one step is singular", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now: renderNow });
    tx.applyAll([
      { type: "tool_result", timestamp: T0, data: { call_id: "a", success: true, narration: "one" } },
      { type: "llm_request", timestamp: T0 + 4, data: { cost_estimate: 0.02 } },
      { type: "status", timestamp: T0 + 4, data: { status: "completed" } },
    ]);
    expect(thread.querySelector(".receipt").textContent).toBe("Done in 4s with 1 step. Cost $0.02.");
  });

  it("an unknown cost says not known", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now: renderNow });
    tx.applyAll([
      { type: "tool_result", timestamp: T0, data: { call_id: "a", success: false, narration: "x" } },
      { type: "status", timestamp: T0 + 1, data: { status: "cancelled" } },
    ]);
    const text = thread.querySelector(".receipt").textContent;
    expect(text).toContain("Cost not known yet.");
    expect(text).not.toContain("—");
  });

  it("070 E.13: a run that is not live and never closed does not say Working forever", () => {
    const clock = (T0 + 3600) * 1000;
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now: () => clock, runLive: false });
    tx.startBuffer();
    tx.applyAll([
      { type: "tool_result", timestamp: T0, data: { call_id: "a", narration: "one" } },
      { type: "tool_result", timestamp: T0 + 5, data: { call_id: "b", narration: "two" } },
    ]);
    tx.flush();
    const receipt = thread.querySelector(".receipt");
    expect(receipt.textContent).toBe("Stopped after 5s and 2 steps, without a final word.");
    expect(receipt.textContent).not.toContain("Working");
    expect(receipt.querySelector("button")).toBeNull(); // no Stop, no Steer
    expect(tx.turn).toBeNull(); // a later event opens a fresh turn
  });

  it("070 E.13: a live run, or one not known, still reads as working", () => {
    for (const runLive of [true, undefined]) {
      const t = makeThread();
      const tx = new Transcript(t, COPY, { sessionId: "s1", now: () => (T0 + 10) * 1000, runLive });
      tx.startBuffer();
      tx.applyAll([{ type: "tool_result", timestamp: T0, data: { call_id: "a", narration: "one" } }]);
      tx.flush();
      expect(t.querySelector(".receipt").textContent).toContain("Working for 10s.");
    }
  });

  it("070 E.13: a closed turn of an ended run keeps its own receipt", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now: () => (T0 + 99) * 1000, runLive: false });
    tx.startBuffer();
    tx.applyAll([
      { type: "tool_result", timestamp: T0, data: { call_id: "a", narration: "one" } },
      { type: "task_complete", timestamp: T0 + 2, data: {} },
    ]);
    tx.flush();
    expect(thread.querySelector(".receipt").textContent).toContain("Done in 2s with 1 step.");
  });

  it("a live working turn counts up to now", () => {
    let clock = (T0 + 10) * 1000;
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now: () => clock, live: true });
    tx.apply({ type: "tool_result", timestamp: T0, data: { call_id: "a", narration: "one" } });
    expect(thread.querySelector(".receipt").textContent).toContain("Working for 10s.");
  });
});


// 070 W0.6 — the controller verbs are never rows; the reply sits under its
// steps; `done` (task_complete) closes the turn.
describe("controller verbs and turn closing", () => {
  let thread;
  const T0 = 1790000000;
  const now = () => (T0 + 999) * 1000;
  beforeEach(() => {
    thread = makeThread();
  });
  const tool = (ts, id, name, extra = {}) => ({
    type: "tool_result", timestamp: ts,
    data: { call_id: id, action_name: name, success: true, narration: `did ${name}`, ...extra },
  });

  it("send_message and done draw no row", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.applyAll([
      tool(T0, "a", "read_file"),
      tool(T0 + 1, "b", "send_message"),
      tool(T0 + 2, "c", "done"),
    ]);
    expect(thread.querySelectorAll(".act").length).toBe(1);
    expect(thread.querySelector(".receipt").textContent).toContain("1 step");
  });

  it("message still draws a row", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.apply(tool(T0, "a", "message"));
    expect(thread.querySelectorAll(".act").length).toBe(1);
  });

  it("the reply follows its steps in the DOM", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.applyAll([
      tool(T0, "a", "read_file"),
      { type: "agent_message", timestamp: T0 + 2, data: { text: "Here it is." } },
    ]);
    const rob = thread.querySelector(".turn-rob");
    const kids = [...rob.children];
    const actsAt = kids.findIndex((k) => k.querySelector(".act"));
    const saidAt = kids.findIndex((k) => k.classList.contains("said"));
    const receiptAt = kids.findIndex((k) => k.classList.contains("receipt"));
    expect(actsAt).toBeLessThan(saidAt);
    expect(saidAt).toBeLessThan(receiptAt);
  });

  it("task_complete closes the turn and the next step opens a new one", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.applyAll([
      tool(T0, "a", "read_file"),
      { type: "task_complete", timestamp: T0 + 3, data: {} },
      tool(T0 + 10, "b", "read_file"),
    ]);
    const turns = thread.querySelectorAll(".turn-rob");
    expect(turns.length).toBe(2);
    expect(turns[0].querySelector(".receipt").textContent).toContain("Done in 3s with 1 step.");
    expect(turns[0].querySelectorAll(".receipt button").length).toBe(0);
  });

  it("an old receipt has no Stop or Steer after a new question", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.applyAll([
      { type: "user_message", timestamp: T0 - 1, data: { text: "one" } },
      tool(T0, "a", "read_file", { success: undefined }),
      { type: "user_message", timestamp: T0 + 5, data: { text: "two" } },
    ]);
    const receipt = thread.querySelector(".turn-rob .receipt");
    expect(receipt.querySelectorAll("button").length).toBe(0);
    expect(receipt.textContent).toBe("5s and 1 step before your message.");
  });

  it("two REPL turns are two receipts", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.applyAll([
      { type: "user_message", timestamp: T0, data: { text: "one" } },
      tool(T0 + 1, "a", "read_file"),
      tool(T0 + 2, "b", "send_message"),
      { type: "agent_message", timestamp: T0 + 2, data: { text: "first" } },
      tool(T0 + 3, "c", "done"),
      { type: "task_complete", timestamp: T0 + 3, data: {} },
      { type: "user_message", timestamp: T0 + 20, data: { text: "two" } },
      tool(T0 + 21, "d", "read_file"),
      tool(T0 + 22, "e", "write_file"),
      { type: "agent_message", timestamp: T0 + 23, data: { text: "second" } },
      { type: "task_complete", timestamp: T0 + 24, data: {} },
    ]);
    const receipts = [...thread.querySelectorAll(".receipt")].map((r) => r.textContent);
    expect(receipts).toEqual([
      "Done in 2s with 1 step. Cost not known yet.",
      "Done in 3s with 2 steps. Cost not known yet.",
    ]);
  });
});


// 070 W0.7 — the latest status is kept; suspended is waiting; a failed run
// says so.
describe("status words", () => {
  let thread;
  const T0 = 1790000000;
  const now = () => (T0 + 999) * 1000;
  beforeEach(() => {
    thread = makeThread();
  });
  const step = (ts, id) => ({ type: "tool_result", timestamp: ts,
    data: { call_id: id, action_name: "read_file", narration: "Read a file" } });
  const status = (ts, value) => ({ type: "status", timestamp: ts, data: { status: value } });

  it("suspended shows waiting, not stopped", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.applyAll([step(T0, "a"), status(T0 + 4, "suspended")]);
    const receipt = thread.querySelector(".receipt");
    expect(receipt.textContent).toContain("Waiting for your answer. 4s so far.");
    expect(receipt.textContent).not.toContain("Stopped");
    // Stop and Steer stay on a waiting turn.
    expect(receipt.querySelectorAll("button").length).toBe(2);
  });

  it("running after suspended returns to working", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.applyAll([step(T0, "a"), status(T0 + 4, "suspended"), status(T0 + 9, "running")]);
    expect(tx.turn.state).toBe("working");
    expect(thread.querySelector(".receipt").textContent).toContain("Working for 9s.");
  });

  it("a failed status with no events draws one failed line", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.applyAll([status(T0, "initializing"), status(T0 + 1, "failed")]);
    tx.apply(status(T0 + 2, "failed"));
    const lines = thread.querySelectorAll(".note.is-failed");
    expect(lines.length).toBe(1);
    expect(lines[0].textContent).toBe("This chat failed before Rob could answer.");
    expect(tx.lastStatus).toBe("failed");
  });

  it("cancelled after two done turns changes nothing", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.applyAll([
      step(T0, "a"), { type: "task_complete", timestamp: T0 + 1, data: {} },
      step(T0 + 5, "b"), { type: "task_complete", timestamp: T0 + 6, data: {} },
      status(T0 + 7, "cancelled"),
    ]);
    const receipts = [...thread.querySelectorAll(".receipt")].map((r) => r.textContent);
    expect(receipts.every((r) => r.startsWith("Done in"))).toBe(true);
    expect(thread.querySelector(".note.is-failed")).toBe(null);
  });

  it("a failed status inside a turn closes it as failed", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.applyAll([step(T0, "a"), status(T0 + 3, "failed"), step(T0 + 9, "b")]);
    const turns = thread.querySelectorAll(".turn-rob");
    expect(turns.length).toBe(2);
    const first = turns[0].querySelector(".receipt");
    expect(first.textContent).toBe("This did not finish. It failed after 3s and 1 step.");
    expect(first.querySelectorAll("button").length).toBe(0);
    // Rob drew a turn, so no "failed before Rob could answer" line.
    expect(thread.querySelector(".note.is-failed")).toBe(null);
  });
});

// 070 W0.8 — a follow-up typed while Rob works is the owner's bubble, drawn once.
describe("follow-ups during a run", () => {
  let thread;
  const T0 = 1790000000;
  const now = () => (T0 + 999) * 1000;
  beforeEach(() => {
    thread = makeThread();
  });

  it("a during-execution message draws a bubble", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.applyAll([
      { type: "tool_result", timestamp: T0, data: { call_id: "a", action_name: "read_file", narration: "Read" } },
      { type: "user_message_during_execution", timestamp: T0 + 3, data: { message_text: "also check the tests" } },
      { type: "tool_result", timestamp: T0 + 6, data: { call_id: "b", action_name: "run", narration: "Ran" } },
    ]);
    const turns = [...thread.querySelectorAll(".turn")];
    expect(turns.map((t) => t.classList.contains("turn-you"))).toEqual([false, true, false]);
    expect(turns[1].textContent).toBe("also check the tests");
    // the follow-up closed the first turn as "so far"
    expect(turns[0].querySelector(".receipt").textContent).toBe("3s and 1 step before your message.");
  });

  it("user_message and during-execution with the same text draw one bubble", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.applyAll([
      { type: "user_message_during_execution", timestamp: T0, data: { message_text: "stop  after this" } },
      { type: "user_message", timestamp: T0 + 1, data: { text: "stop after this" } },
    ]);
    expect(thread.querySelectorAll(".turn-you").length).toBe(1);
  });

  it("the full user_message text replaces the cut copy", () => {
    const full = "x".repeat(250);
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.apply({ type: "user_message_during_execution", timestamp: T0, data: { message_text: full.slice(0, 200) } });
    expect(thread.querySelector(".turn-you .bubble").textContent).toBe(`${"x".repeat(200)}…`);
    tx.apply({ type: "user_message", timestamp: T0 + 2, data: { text: full } });
    const bubbles = thread.querySelectorAll(".turn-you .bubble");
    expect(bubbles.length).toBe(1);
    expect(bubbles[0].textContent).toBe(full);
  });

  it("the same words a minute later are a new message", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.apply({ type: "user_message", timestamp: T0, data: { text: "yes" } });
    tx.apply({ type: "user_message", timestamp: T0 + 120, data: { text: "yes" } });
    expect(thread.querySelectorAll(".turn-you").length).toBe(2);
  });
});

// 070 W0.9 — one event, one row; join first, then backfill.
describe("one event, one row", () => {
  let thread;
  const T0 = 1790000000;
  const now = () => (T0 + 999) * 1000;
  beforeEach(() => {
    thread = makeThread();
  });
  const ev = (id, ts, call) => ({ _id: id, type: "tool_result", timestamp: ts,
    data: { call_id: call, action_name: "read_file", narration: `step ${call}` } });

  it("the same event from the fast push and the watcher draws one row", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.apply(ev("e1", T0, "a"));
    tx.apply(ev("e1", T0, "a"));
    // a distinct event with no call id would otherwise be a second row
    tx.apply({ _id: "e2", type: "tool_result", timestamp: T0 + 1, data: { narration: "x" } });
    tx.apply({ _id: "e2", type: "tool_result", timestamp: T0 + 1, data: { narration: "x" } });
    expect(thread.querySelectorAll(".act").length).toBe(2);
  });

  it("an event that arrives during the backfill is drawn once and in order", () => {
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now });
    tx.startBuffer();
    tx.receive(ev("e3", T0 + 3, "c"));    // live, while the backfill loads
    tx.receive(ev("e4", T0 + 4, "d"));    // live, not in the backfill
    expect(thread.querySelectorAll(".act").length).toBe(0);
    tx.applyAll([ev("e1", T0, "a"), ev("e2", T0 + 2, "b"), ev("e3", T0 + 3, "c")]);
    tx.flush();
    const lines = [...thread.querySelectorAll(".act-what")].map((n) => n.textContent);
    expect(lines).toEqual(["step a", "step b", "step c", "step d"]);
    expect(tx.live).toBe(true);
    // after the flush a live event applies at once
    tx.receive(ev("e5", T0 + 5, "e"));
    expect(thread.querySelectorAll(".act").length).toBe(5);
  });
});

describe("a late cost belongs to the turn it paid for", () => {
  it("a cost after done adds to the closed turn, never opens an empty one", () => {
    const thread = makeThread();
    const T0 = 1790000000;
    const tx = new Transcript(thread, COPY, { sessionId: "s1", now: () => (T0 + 999) * 1000 });
    tx.applyAll([
      { type: "tool_result", timestamp: T0, data: { call_id: "a", action_name: "read_file", success: true, narration: "Read" } },
      { type: "task_complete", timestamp: T0 + 2, data: {} },
      { type: "llm_request", timestamp: T0 + 3, data: { cost_estimate: 0.013 } },
      { type: "user_message", timestamp: T0 + 60, data: { text: "next" } },
    ]);
    expect(thread.querySelectorAll(".turn-rob").length).toBe(1);
    expect(thread.querySelector(".receipt").textContent).toBe("Done in 2s with 1 step. Cost $0.01.");
  });
});


describe("070 W0.12 — the stored first line", () => {
  it("the stored first line becomes the first bubble", () => {
    const thread = document.createElement("div");
    const t = new Transcript(thread, COPY, { now: () => 5_000_000 });
    t.seedFirstLine("are u here?");
    t.applyAll([
      { type: "agent_message", timestamp: 4000, data: { text: "Yes." } },
    ]);
    const turns = thread.querySelectorAll(".turn");
    expect(turns[0].classList.contains("turn-you")).toBe(true);
    expect(turns[0].textContent).toBe("are u here?");
  });

  it("a later user_message with the same text does not double it", () => {
    const thread = document.createElement("div");
    const t = new Transcript(thread, COPY, { now: () => 5_000_000 });
    t.seedFirstLine("are u here?");
    t.applyAll([
      { type: "user_message", timestamp: 4000, data: { text: "are u here?" } },
      { type: "agent_message", timestamp: 4001, data: { text: "Yes." } },
    ]);
    expect(thread.querySelectorAll(".turn-you").length).toBe(1);
    // a real repeat, later, is a new message
    t.apply({ type: "user_message", timestamp: 9000, data: { text: "are u here?" } });
    expect(thread.querySelectorAll(".turn-you").length).toBe(2);
  });
});
