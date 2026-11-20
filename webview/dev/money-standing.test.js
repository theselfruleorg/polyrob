// Money › Limits › Standing rules (static/app/money-standing.js): the status
// snapshot's income_rule and mandates sections; unreadable is never "none".
import { describe, it, expect } from "vitest";
import { renderStanding, loadStanding } from "../static/app/money-standing.js";

const COPY = { title: "Standing rules", aside: "aside", income_rule: "Income rule",
  mandates: "Mandates", unreadable: "I could not read this.", why: "Why it failed" };

const BODY = { sections: [
  { name: "income_rule", state: "ok", lines: ["Rule: none set", "Owed to the rule: $0.00"],
    health: [] },
  { name: "mandates", state: "unavailable", reason: "JSONDecodeError: bad", lines: [],
    health: [] },
] };

describe("standing rules", () => {
  it("draws each section's own lines, and an unreadable one as unreadable", () => {
    const root = document.createElement("div");
    expect(renderStanding(root, BODY, COPY)).toBe("standing");
    const income = root.querySelector('[data-section="income_rule"]');
    expect(income.textContent).toContain("Income rule");
    expect(income.textContent).toContain("Owed to the rule: $0.00");
    const mandates = root.querySelector('[data-section="mandates"]');
    expect(mandates.dataset.state).toBe("unavailable");
    expect(mandates.textContent).toContain("I could not read this.");
    expect(mandates.textContent).toContain("JSONDecodeError");
  });

  it("a health item is drawn as needing you", () => {
    const root = document.createElement("div");
    renderStanding(root, { sections: [{ name: "mandates", state: "degraded",
      lines: ["m_1 [live] …"], health: [{ text: "does not verify", remedy: "/nft mandate" }] }] },
    COPY);
    expect(root.querySelector(".is-needs-you").textContent).toContain("does not verify");
  });

  it("a failed read is unreadable, never an empty list", async () => {
    const root = document.createElement("div");
    const fetcher = async () => ({ ok: false, status: 403, json: async () => ({ detail: "owner only" }) });
    expect(await loadStanding(root, COPY, fetcher)).toBe("unreadable");
    expect(root.textContent).toContain("owner only");
  });
});
