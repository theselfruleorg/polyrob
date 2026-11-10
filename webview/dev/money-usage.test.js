// 113 W6 item 4 (G-3): Money › Cash › AI cost.
import { describe, it, expect, vi } from "vitest";
import { renderUsage, loadUsage } from "../static/app/money-usage.js";

const COPY = { title: "AI cost", period: "Period", days_1: "24 hours", days_7: "7 days",
  days_30: "30 days", unreadable: "I could not read what the AI use cost. This is not a zero.", why: "Why" };

describe("AI cost", () => {
  it("prints the verb's lines and marks the period", () => {
    const root = document.createElement("section");
    const lines = ["AI cost, the last 7 days: $1.20 · 40 AI requests · 50% of input from cache", "by model:"];
    expect(renderUsage(root, { readable: true, lines }, COPY, 7, () => {})).toBe("usage");
    expect([...root.querySelectorAll(".usage-lines p")].map((p) => p.textContent)).toEqual(lines);
    const pressed = root.querySelector('[aria-pressed="true"]');
    expect(pressed.textContent).toBe("7 days");
  });

  it("an unreadable record is its own entry with the reason, never a zero", () => {
    const root = document.createElement("section");
    expect(renderUsage(root, { readable: false, error: "DatabaseError: locked" }, COPY, 7, () => {})).toBe("unreadable");
    expect(root.textContent).toContain("This is not a zero.");
    expect(root.querySelector("details").textContent).toContain("DatabaseError: locked");
    expect(root.textContent).not.toContain("$0");
  });

  it("a period chip re-reads with its days", async () => {
    const root = document.createElement("section");
    const fetcher = vi.fn(async () => ({ ok: true, json: async () => ({ readable: true, lines: ["x"] }) }));
    await loadUsage(root, COPY, 7, fetcher);
    [...root.querySelectorAll("button")].find((b) => b.textContent === "30 days").click();
    await new Promise((r) => setTimeout(r, 0));
    expect(fetcher.mock.calls.map((c) => c[0])).toEqual(["/api/webgate/usage?days=7", "/api/webgate/usage?days=30"]);
  });

  it("a refused or failed read is unreadable", async () => {
    const root = document.createElement("section");
    const refused = vi.fn(async () => ({ ok: false, status: 403, json: async () => ({ detail: "no identity" }) }));
    expect(await loadUsage(root, COPY, 7, refused)).toBe("unreadable");
    expect(root.querySelector("details").textContent).toContain("no identity");
    const down = vi.fn(async () => { throw new Error("offline"); });
    expect(await loadUsage(root, COPY, 7, down)).toBe("unreadable");
  });
});
