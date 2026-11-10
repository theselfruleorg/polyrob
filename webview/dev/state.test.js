// 113 W5 item 2 (ST-2) — `stateBlock` is THE empty state a module draws: the
// same markup as templates/_state.html, with no empty heading and no empty row.
import { describe, it, expect } from "vitest";
import { stateBlock } from "../static/app/state.js";
import { emptyState as moneyEmpty } from "../static/app/money.js";
import { emptyState as appsEmpty } from "../static/app/work-apps.js";

describe("stateBlock", () => {
  it("draws glyph, title, body lines and the one action", () => {
    const node = stateBlock({ glyph: "G", title: "T", body: ["a", "b"],
      actions: [{ href: "/w", label: "Go", primary: true }] });
    expect(node.className).toBe("state");
    expect(node.querySelector(".state-glyph").getAttribute("aria-hidden")).toBe("true");
    expect(node.querySelector("h2.state-title").textContent).toBe("T");
    expect([...node.querySelectorAll("p.state-body")].map((p) => p.textContent)).toEqual(["a", "b"]);
    const link = node.querySelector(".state-actions a.btn.btn-primary");
    expect(link.getAttribute("href")).toBe("/w");
  });

  it("leaves out what is empty", () => {
    const node = stateBlock({ body: "only" });
    expect(node.querySelector("h2")).toBeNull();
    expect(node.querySelector(".state-actions")).toBeNull();
    expect(node.querySelector("details")).toBeNull();
  });

  it("puts a named reason behind a disclosure", () => {
    const node = stateBlock({ title: "T", why: "Why", reason: "boom" });
    expect(node.querySelector("details summary").textContent).toBe("Why");
    expect(node.querySelector("details .entry-meta").textContent).toBe("boom");
  });

  it("is the shape Money and Work › Apps draw", () => {
    const copy = { empty_title: "T", empty_body: "B", empty_action: "Go" };
    expect(moneyEmpty(null, copy).querySelector("h2.state-title").textContent).toBe("T");
    expect(appsEmpty(copy).querySelector(".state-actions a").textContent).toBe("Go");
  });
});
