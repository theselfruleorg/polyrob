// 113 W5 item 5 (C-05, C-07, CH-4) — a transcript that keeps its history: a
// chat-box verb's answer comes back from the feed on reload, "Load older" pages
// back through the feed, and the thread is a log region that is busy while a
// turn works.
import { describe, it, expect } from "vitest";
import { Transcript, loadOlder } from "../static/app/transcript.js";

const COPY = { load_older: "Load older messages", load_older_failed: "Could not load." };

function makeThread() {
  document.body.innerHTML = '<div class="thread" id="chat-messages"></div>';
  return document.getElementById("chat-messages");
}

const kept = (ts, text, line) => ({ type: "command_reply", timestamp: ts,
  data: { text, line, inline: true } });

describe("a kept verb answer", () => {
  it("is drawn on reload with the line it answers", () => {
    const tx = new Transcript(makeThread(), COPY);
    tx.applyAll([kept(5, "All good.", "/status")]);
    const note = tx.thread.querySelector("[data-command-reply]");
    expect(note.querySelector(".why").textContent).toBe("/status");
    expect(note.textContent).toContain("All good.");
  });

  it("is not drawn twice in the tab that typed it", () => {
    const tx = new Transcript(makeThread(), COPY);
    tx._commandNote({ command_reply: "All good." });            // the HTTP answer
    tx.receive(kept(5, "All good.", "/status"));                  // the feed's copy
    expect(tx.thread.querySelectorAll("[data-command-reply]")).toHaveLength(1);
    tx.receive(kept(9, "All good.", "/status"));                  // a second, later /status
    expect(tx.thread.querySelectorAll("[data-command-reply]")).toHaveLength(2);
  });

  it("brings its card buttons back", () => {
    const tx = new Transcript(makeThread(), { card_ok: "Confirm", card_no: "Cancel" });
    tx.applyAll([kept(5, "Send 1 USDC? /card_0123456789_ok /card_0123456789_no", "/send 1 USDC to x")]);
    expect(tx.thread.querySelectorAll("[data-card-taps] button").length).toBeGreaterThan(0);
  });
});

describe("Load older", () => {
  const msg = (ts, text) => ({ type: "agent_message", timestamp: ts, data: { message: text } });

  it("shows only when the feed has more, and draws the page above", async () => {
    const tx = new Transcript(makeThread(), COPY, { sessionId: "s1" });
    tx.applyAll([msg(100, "newest")]);
    expect(tx.olderControl(false, () => {})).toBeNull();
    tx.feedRead = 1;
    const ctl = tx.olderControl(true, () => {});
    expect(ctl.querySelector("button").textContent).toBe("Load older messages");
    expect(tx.olderControl(true, () => {})).toBe(ctl);           // one control
    const calls = [];
    const fetcher = async (url) => {
      calls.push(url);
      return { ok: true, status: 200, json: async () => ({ events: [msg(10, "older")], has_more: false, read: 1 }) };
    };
    expect(await loadOlder(tx, "s1", fetcher)).toBe(true);
    expect(calls[0]).toContain("skip=1");
    const text = tx.thread.textContent;
    expect(text.indexOf("older")).toBeLessThan(text.indexOf("newest"));
    expect(tx.thread.querySelector("[data-load-older]")).toBeNull();   // no more pages
    expect(tx.feedRead).toBe(2);
  });

  it("a failed page says so and keeps the control", async () => {
    const tx = new Transcript(makeThread(), COPY, { sessionId: "s1" });
    tx.olderControl(true, () => {});
    const fetcher = async () => ({ ok: false, status: 500, json: async () => ({}) });
    expect(await loadOlder(tx, "s1", fetcher)).toBe(false);
    expect(tx.thread.textContent).toContain("Could not load.");
    expect(tx.thread.querySelector("[data-load-older]")).not.toBeNull();
  });
});

describe("the log region (CH-4)", () => {
  it("is busy while the backfill loads, idle once drawn", () => {
    const tx = new Transcript(makeThread(), COPY);
    tx.startBuffer();
    expect(tx.thread.getAttribute("aria-busy")).toBe("true");
    tx.flush();
    expect(tx.thread.getAttribute("aria-busy")).toBe("false");
  });
});
