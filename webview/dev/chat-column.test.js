// 113 W6 item 3 (W2 item 6, IA-1): the chat column on the Chat destination.
import { describe, it, expect, vi } from "vitest";
import { columnChats, columnRow, renderColumn, loadColumn, mount, WIDE } from "../static/app/chat-column.js";

const COPY = { main: "Main chat", untitled: "Untitled chat", status_running: "Working",
  status_done: "Done", status_unknown: "Not known", unreadable: "I could not load your chats.",
  empty: "No chats yet." };

const SESSIONS = [
  { id: "run1", status: "completed", task: "a cron run" },                                  // not a chat
  { id: "s2", status: "completed", chat: { slug: "c2", label: "tax", main: false, active_iso: "2026-11-10T09:00:00+00:00" } },
  { id: "s3", status: "running", chat: { slug: "c3", label: "", main: false, active_iso: "2026-11-10T11:00:00+00:00" }, task: "plan the trip" },
  { id: "s1", status: "completed", chat: { slug: "main", label: "", main: true, active_iso: "2026-11-09T00:00:00+00:00" } },
];

function frame() {
  document.body.innerHTML = '<aside id="chat-column"><ul id="chat-column-list"></ul><p id="chat-column-state"></p></aside>' +
    '<div id="chats-copy" hidden data-main="Main chat" data-unreadable="I could not load your chats." data-empty="No chats yet."' +
    ' data-status_running="Working" data-status_done="Done"></div>';
  document.body.dataset.sessionId = "s2";
  return document.getElementById("chat-column");
}

describe("the chat column", () => {
  it("lists only your chats: main first, then by last activity", () => {
    expect(columnChats(SESSIONS).map((s) => s.id)).toEqual(["s1", "s3", "s2"]);
  });

  it("a row is the name and the state pill; the open chat is aria-current", () => {
    const row = columnRow(SESSIONS[1], COPY, "s2");
    expect(row.getAttribute("href")).toBe("/c/s2");
    expect(row.getAttribute("aria-current")).toBe("page");
    expect(row.querySelector(".chat-column-name").textContent).toBe("tax");
    expect(row.querySelector(".pill").textContent).toBe("Done");
    expect(columnRow(SESSIONS[3], COPY, "s2").querySelector(".chat-column-name").textContent).toBe("Main chat");
    expect(columnRow(SESSIONS[2], COPY, "s2").getAttribute("aria-current")).toBeNull();
    expect(columnRow(SESSIONS[2], COPY, "s2").querySelector(".chat-column-name").textContent).toBe("plan the trip");
  });

  it("a failed read says so; no chats is not a failure", () => {
    const root = frame();
    const list = root.querySelector("ul"); const state = root.querySelector("p");
    expect(renderColumn(list, state, null, COPY, "")).toBe("unreadable");
    expect(state.textContent).toBe("I could not load your chats.");
    expect(renderColumn(list, state, [SESSIONS[0]], COPY, "")).toBe("empty");
    expect(state.textContent).toBe("No chats yet.");
  });

  it("reads the one catalog and draws the rows", async () => {
    const root = frame();
    const fetcher = vi.fn(async () => ({ ok: true, json: async () => ({ sessions: SESSIONS }) }));
    expect(await loadColumn(root, { fetcher })).toBe("rows");
    expect(fetcher.mock.calls[0][0]).toBe("/api/webgate/chats?offset=0&limit=50");
    expect(root.querySelectorAll("li").length).toBe(3);
    expect(root.querySelector('[aria-current="page"]').getAttribute("href")).toBe("/c/s2");
    const bad = vi.fn(async () => ({ ok: false, status: 500 }));
    expect(await loadColumn(root, { fetcher: bad })).toBe("unreadable");
  });

  it("below 1024 px it reads nothing", () => {
    frame();
    const fetchSpy = vi.fn();
    globalThis.fetch = fetchSpy;
    const win = { matchMedia: (q) => ({ matches: false, media: q, addEventListener() {} }) };
    expect(WIDE).toBe("(min-width: 1024px)");
    mount({ win });
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
