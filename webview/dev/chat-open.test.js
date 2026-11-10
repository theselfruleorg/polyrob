// 070 W0.13 — a cold open with no model says so instead of a silent 200.
import { describe, it, expect, beforeEach } from "vitest";
import { startSession } from "../static/app/chat-open.js";

const NO_MODEL = "The agent cannot answer from this console yet. Your text is still here.";

function page() {
  document.body.innerHTML =
    '<div class="thread" id="chat-open-notes"></div>' +
    `<form id="chat-composer" data-unreachable="did not reach" data-no_model="${NO_MODEL}"` +
    ' data-no_model_link="Open Agent › Settings" data-no_model_href="/agent#settings">' +
    '<textarea id="chat-input"></textarea><button id="chat-send-btn">Send</button></form>' +
    '<p id="chat-create-status"></p>' +
    '<div id="transcript-copy" hidden data-card_ok="Confirm" data-card_no="Cancel" data-card_re="Refresh"></div>';
  document.getElementById("chat-input").value = "are u here?";
}

describe("chat-open startSession", () => {
  beforeEach(() => page());

  it("a 503 no_model is a note with the Settings link, and keeps the text (113 W5, CH-3)", async () => {
    const fetcher = async () => ({
      ok: false, status: 503,
      json: async () => ({ detail: { code: "no_model", message: "no language model in this process" } }),
    });
    await startSession("are u here?", { fetcher });
    const note = document.querySelector("#chat-open-notes [data-no-model]");
    expect(note.textContent).toContain(NO_MODEL);
    expect(note.querySelector("a").getAttribute("href")).toBe("/agent#settings");
    expect(document.getElementById("chat-input").value).toBe("are u here?");
    expect(document.getElementById("chat-send-btn").disabled).toBe(false);
    expect(document.getElementById("chat-composer").getAttribute("aria-busy")).toBe(null);
  });

  it("another 503 is not read as no_model", async () => {
    const fetcher = async () => ({ ok: false, status: 503, json: async () => ({ detail: "busy" }) });
    await startSession("hi", { fetcher });
    expect(document.getElementById("chat-create-status").textContent).not.toBe(NO_MODEL);
    expect(document.querySelector("[data-no-model]")).toBeNull();
  });

  it("a verb answer on the cold open is a Transcript note with its card buttons (C-06)", async () => {
    const reply = "Send 1 USDC to the address? /card_0123456789_ok /card_0123456789_no";
    const calls = [];
    const fetcher = async (url, init) => {
      calls.push(JSON.parse(init.body));
      return { ok: true, status: 200, json: async () => ({ success: true, command_reply: reply }) };
    };
    document.getElementById("chat-input").value = "/send 1 USDC to x";
    expect(await startSession("/send 1 USDC to x", { fetcher })).toEqual({ ok: true });
    const note = document.querySelector("#chat-open-notes [data-command-reply]");
    expect(note.textContent).toBe(reply);
    expect(document.getElementById("chat-input").value).toBe("");
    const buttons = document.querySelectorAll("#chat-open-notes [data-card-taps] button");
    expect(buttons.length).toBeGreaterThan(0);
    buttons[0].click();
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    expect(calls[1].task).toMatch(/^\/card_0123456789_/);   // the tap runs on the create route
  });
});

// 113 W6 item 2 (C-02): attach on the start page.
import { vi } from "vitest";
import { held } from "../static/app/file-attach.js";

describe("chat-open with a held file", () => {
  let assign;
  beforeEach(() => {
    page();
    document.body.insertAdjacentHTML("beforeend",
      '<div id="attach-copy" hidden data-session_id="" data-attach_sending="Sending {name}"' +
      ' data-attach_done="{name} is in this chat\'s folder." data-attach_failed="I could not take {name}."></div>');
    held.files = [{ file: new File(["x"], "notes.txt"), line: document.createElement("p") }];
    held.paths = []; held.sessionId = "";
    assign = vi.fn();
    Object.defineProperty(window, "location", { value: { assign }, writable: true, configurable: true });
  });

  it("creates the chat unstarted, sends the file, then starts it with the line", async () => {
    const calls = [];
    const fetcher = async (url, init) => {
      calls.push({ url, body: typeof init.body === "string" ? JSON.parse(init.body) : "form" });
      if (url === "/api/task/sessions") return { ok: true, status: 200, json: async () => ({ success: true, session_id: "abc12345" }) };
      if (url.endsWith("/workspace/upload")) return { ok: true, status: 200, json: async () => ({ path: "inbound/notes.txt" }) };
      return { ok: true, status: 200, json: async () => ({ success: true }) };
    };
    expect(await startSession("read this", { fetcher })).toEqual({ ok: true });
    expect(calls[0].body).toEqual({ task: "read this", auto_start: false, wait_for_uploads: true });
    expect(calls[1].url).toBe("/api/task/sessions/abc12345/workspace/upload");
    expect(calls[2].url).toBe("/api/session/abc12345/messages");
    expect(calls[2].body.attached_files).toEqual(["inbound/notes.txt"]);
    expect(calls[2].body.text).toBe("read this");
    expect(assign).toHaveBeenCalledWith("/c/abc12345");
  });

  it("a 503 no_model at create is the note, and no file is sent", async () => {
    const calls = [];
    const fetcher = async (url) => {
      calls.push(url);
      return { ok: false, status: 503, json: async () => ({ detail: { code: "no_model" } }) };
    };
    await startSession("read this", { fetcher });
    expect(calls).toEqual(["/api/task/sessions"]);
    expect(document.querySelector("#chat-open-notes [data-no-model]")).not.toBeNull();
    expect(held.files.length).toBe(1);
    expect(assign).not.toHaveBeenCalled();
  });

  it("a file that did not arrive keeps the page, and the next Send goes to the same chat", async () => {
    const calls = [];
    let refuse = true;
    const fetcher = async (url) => {
      calls.push(url);
      if (url === "/api/task/sessions") return { ok: true, status: 200, json: async () => ({ session_id: "abc12345" }) };
      if (url.endsWith("/workspace/upload")) {
        return refuse ? { ok: false, status: 413, json: async () => ({ detail: "too big" }) }
          : { ok: true, status: 200, json: async () => ({ path: "inbound/notes.txt" }) };
      }
      return { ok: true, status: 200, json: async () => ({ success: true }) };
    };
    expect(await startSession("read this", { fetcher })).toEqual({ ok: false });
    expect(held.files[0].line.textContent).toBe("too big");
    expect(assign).not.toHaveBeenCalled();
    refuse = false;
    expect(await startSession("read this", { fetcher })).toEqual({ ok: true });
    expect(calls.filter((u) => u === "/api/task/sessions").length).toBe(1);
    expect(assign).toHaveBeenCalledWith("/c/abc12345");
  });
});
