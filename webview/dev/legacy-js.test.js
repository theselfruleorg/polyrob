// FE14 / FE15 / FE16 (2026-10-03 audit) — the legacy `static/js` pages.
//
// The console CSP drops `'unsafe-inline'`, so an inline `onclick=` is dead
// markup: the toast × and the "Reload" button did nothing, and the critical
// overlay stacked. Server text (`str(exc)`, admin fields) must never be parsed
// as HTML.
import { describe, it, expect, beforeEach } from "vitest";
import { ErrorHandler } from "../static/js/error-handler.js";
import { setAction } from "../static/js/error-page.js";

describe("FE14 — the error toast and overlay need no inline handler", () => {
  beforeEach(() => { document.body.replaceChildren(); });

  it("the toast × closes it, and carries no onclick attribute", () => {
    const h = new ErrorHandler();
    h.showErrorNotification({ message: "x" });
    const toast = document.querySelector(".error-notification");
    expect(toast.querySelector("[onclick]")).toBeNull();
    toast.querySelector(".error-close").click();
    expect(document.querySelector(".error-notification")).toBeNull();
  });

  it("the critical overlay is drawn once, with a wired Reload", () => {
    const h = new ErrorHandler();
    h.showCriticalError();
    h.showCriticalError();
    expect(document.querySelectorAll(".error-critical-overlay").length).toBe(1);
    const reload = document.querySelector(".btn-reload");
    expect(reload.getAttribute("onclick")).toBeNull();
  });
});

describe("FE15 — the repair answer is text, never HTML", () => {
  it("setAction writes server text as a text node", () => {
    const btn = document.createElement("button");
    setAction(btn, 'Error: <img src=x onerror="alert(1)">');
    expect(btn.querySelector("img")).toBeNull();
    expect(btn.textContent).toBe('> Error: <img src=x onerror="alert(1)">');
    expect(btn.querySelector(".action-prefix").textContent).toBe(">");
  });
});

describe("FE16 — admin pages write server fields as text", () => {
  it("AdminUtils.h never parses a server field as HTML", async () => {
    await import("../static/js/admin/utils.js");
    const { h, messageRow } = window.AdminUtils;
    const evil = '<img src=x onerror="alert(1)">';
    const row = h("tr", null, h("td", { class: "action", title: evil, text: evil }), "tail");
    expect(row.querySelector("img")).toBeNull();
    expect(row.querySelector("td").textContent).toBe(evil);
    expect(row.querySelector("td").title).toBe(evil);
    expect(row.textContent).toBe(`${evil}tail`);
    const tbody = document.createElement("tbody");
    messageRow(tbody, 7, "empty-state", `Error: ${evil}`);
    expect(tbody.querySelector("img")).toBeNull();
    expect(tbody.querySelector("td").getAttribute("colspan")).toBe("7");
  });

  it("no admin script assigns an interpolated string to innerHTML", async () => {
    const { readFileSync, readdirSync } = await import("node:fs");
    const { resolve } = await import("node:path");
    const dir = resolve(process.cwd(), "../static/js/admin");
    for (const name of readdirSync(dir).filter((n) => n.endsWith(".js"))) {
      const src = readFileSync(resolve(dir, name), "utf8");
      // a template literal or a built `html` string handed to innerHTML
      expect(src, name).not.toMatch(/innerHTML\s*\+?=\s*`[^`]*\$\{/);
      expect(src, name).not.toMatch(/innerHTML\s*\+?=\s*html\b/);
    }
  });
});
