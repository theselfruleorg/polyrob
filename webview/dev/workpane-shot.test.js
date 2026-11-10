// 113 W6 item 7 (C-11): the chat's last browser screenshot in the Work pane.
import { describe, it, expect, vi } from "vitest";
import { renderShot, loadShot } from "../static/app/workpane-shot.js";

const COPY = { none: "The browser took no screenshot in this chat.",
  unreadable: "I could not read this chat's screenshots.", alt: "The last screenshot" };

describe("the last browser screenshot", () => {
  it("draws this chat's own file, and not the guessed page address", () => {
    const root = document.createElement("div");
    const body = { status: "ok", url: "/api/session/s1/screenshot/file?ts=5", page_url: "https://example.org/a" };
    expect(renderShot(root, body, COPY, "s1")).toBe("shot");
    expect(root.querySelector("img").getAttribute("src")).toBe("/api/session/s1/screenshot/file?ts=5");
    expect(root.querySelector("img").alt).toBe("The last screenshot");
    expect(root.textContent).not.toContain("example.org");
  });

  it("no screenshot is one line, a failed read says so", () => {
    const root = document.createElement("div");
    expect(renderShot(root, { status: "ok", is_placeholder: true, url: "/static/img/favicon.ico" }, COPY, "s1")).toBe("none");
    expect(root.textContent).toBe(COPY.none);
    expect(renderShot(root, null, COPY, "s1")).toBe("unreadable");
    expect(renderShot(root, { status: "ok", url: "https://elsewhere.example/x.png" }, COPY, "s1")).toBe("unreadable");
  });

  it("a 500 from the reader is unreadable, not none", async () => {
    const root = document.createElement("div");
    const fetcher = vi.fn(async () => ({ ok: false, status: 500, json: async () => ({ status: "error" }) }));
    expect(await loadShot(root, COPY, "s1", fetcher)).toBe("unreadable");
    expect(fetcher.mock.calls[0][0]).toBe("/api/session/s1/screenshot");
  });
});
