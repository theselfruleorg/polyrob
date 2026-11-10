// 113 W5 item 3 (IA-2) — the palette has a "Commands" button beside "Chats"
// that shows its key, and `/` in an empty composer still opens it.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const flush = () => new Promise((r) => setTimeout(r, 0));

function page() {
  document.body.innerHTML = `
    <button id="palette-open" type="button">Commands</button>
    <dialog id="palette-dialog"><input id="palette-input"><div id="palette-results"></div>
      <button id="palette-close"></button></dialog>
    <div id="palette-copy" hidden></div>
    <form id="chat-composer"><textarea id="chat-input"></textarea></form>`;
  const dialog = document.getElementById("palette-dialog");
  dialog.showModal = vi.fn(() => dialog.setAttribute("open", ""));
  return dialog;
}

describe("the Commands button", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ group_order: [], verbs: [] }) })));
  });

  it("opens the palette with an empty query", async () => {
    const dialog = page();
    const { bind } = await import("../static/app/palette.js");
    bind();
    document.getElementById("palette-open").click();
    await flush();
    expect(dialog.showModal).toHaveBeenCalled();
    expect(document.getElementById("palette-input").value).toBe("");
  });

  it("/ in an empty composer still opens it seeded with /", async () => {
    const dialog = page();
    const { bind } = await import("../static/app/palette.js");
    bind();
    const box = document.getElementById("chat-input");
    box.dispatchEvent(new KeyboardEvent("keydown", { key: "/", bubbles: true }));
    await flush();
    expect(dialog.showModal).toHaveBeenCalled();
    expect(document.getElementById("palette-input").value).toBe("/");
  });

  it("sits beside Chats in the shell and shows its key", () => {
    const shell = readFileSync(resolve(process.cwd(), "..", "templates", "shell.html"), "utf8");
    const chats = shell.indexOf('id="chats-open"');
    const cmds = shell.indexOf('id="palette-open"');
    expect(chats).toBeGreaterThan(0);
    expect(cmds).toBeGreaterThan(chats);
    expect(shell.slice(cmds, cmds + 400)).toContain('<span class="kbd">&#8984;&#8679;P</span>');
  });
});
