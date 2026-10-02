// 070 W0.13 — a cold open with no model says so instead of a silent 200.
import { describe, it, expect, beforeEach } from "vitest";
import { startSession } from "../static/app/chat-open.js";

const NO_MODEL = "Rob cannot answer from this console yet. Your text is still here.";

function page() {
  document.body.innerHTML =
    `<form id="chat-composer" data-unreachable="did not reach" data-no_model="${NO_MODEL}">` +
    '<textarea id="chat-input"></textarea><button id="chat-send-btn">Send</button></form>' +
    '<p id="chat-create-status"></p>';
  document.getElementById("chat-input").value = "are u here?";
}

describe("chat-open startSession", () => {
  beforeEach(() => page());

  it("a 503 no_model shows the copy line and keeps the text", async () => {
    const fetcher = async () => ({
      ok: false, status: 503,
      json: async () => ({ detail: { code: "no_model", message: "no language model in this process" } }),
    });
    await startSession("are u here?", { fetcher });
    expect(document.getElementById("chat-create-status").textContent).toBe(NO_MODEL);
    expect(document.getElementById("chat-input").value).toBe("are u here?");
    expect(document.getElementById("chat-send-btn").disabled).toBe(false);
    expect(document.getElementById("chat-composer").getAttribute("aria-busy")).toBe(null);
  });

  it("another 503 is not read as no_model", async () => {
    const fetcher = async () => ({ ok: false, status: 503, json: async () => ({ detail: "busy" }) });
    await startSession("hi", { fetcher });
    expect(document.getElementById("chat-create-status").textContent).not.toBe(NO_MODEL);
  });
});
