// 070 E.31 — a live-update refusal shows the console's words, never the server's.
import { describe, it, expect } from "vitest";
import { liveRefusalLine } from "../static/app/pause.js";
import { liveNotice } from "../static/app/transcript.js";

const COPY = {
  live_busy: "Too many reconnects. Live updates start again in a minute.",
  live_refused: "Live updates are off here. Reload the page to try again.",
  live_unavailable: "Reconnecting…",
};
const SERVER = "Rate limit exceeded. Please wait before reconnecting.";

describe("the head's live notice", () => {
  it("a rate_limited refusal shows the console words, never the server message", () => {
    const line = liveRefusalLine({ code: "rate_limited", message: SERVER }, COPY);
    expect(line).toBe(COPY.live_busy);
    expect(line).not.toContain("Rate limit");
  });

  it("any other refusal is the refused line", () => {
    expect(liveRefusalLine({ code: "not_yours", message: "Not authorized" }, COPY)).toBe(COPY.live_refused);
    expect(liveRefusalLine({ message: "anything" }, COPY)).toBe(COPY.live_refused);
    expect(liveRefusalLine(null, COPY)).toBe(COPY.live_refused);
  });
});

describe("the chat's live notice", () => {
  it("no_chat reads as unavailable; rate_limited as busy; the server text never shows", () => {
    expect(liveNotice({ code: "no_chat", message: "No session ID provided" }, COPY)).toBe(COPY.live_unavailable);
    expect(liveNotice({ code: "rate_limited", message: SERVER }, COPY)).toBe(COPY.live_busy);
    expect(liveNotice({ message: "boom" }, COPY)).toBe(COPY.live_unavailable);
  });
});
