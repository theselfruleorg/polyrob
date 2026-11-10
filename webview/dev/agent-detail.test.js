// 113 W5 item 9 (C-14) — a skill's text on Abilities and the episodes on Memory:
// two readers that had no console caller now have one.
import { describe, it, expect } from "vitest";
import { fillSkill, renderEpisodes, skillDetail } from "../static/app/agent-detail.js";
import { capabilityRow } from "../static/app/agent.js";

const COPY = { cap_skill_read: "Read the skill", cap_skill_gone: "Gone.", cap_skill_unreadable: "Could not read.",
  mem_ep_title: "What the agent did", mem_ep_aside: "the last {count} runs",
  mem_ep_empty: "No runs yet.", mem_ep_unreadable: "Could not read the runs." };
const answer = (body, ok = true, status = 200) => async () => ({ ok, status, json: async () => body });

describe("a skill's text", () => {
  it("a skill row carries the disclosure, a tool row does not", () => {
    const skill = capabilityRow({ id: "deploy", kind: "skill", what: "Deploy", on: true }, COPY, 0);
    expect(skill.querySelector("details.skill-detail summary").textContent).toBe("Read the skill");
    const tool = capabilityRow({ id: "shell", kind: "tool", what: "Shell", on: true }, COPY, 0);
    expect(tool.querySelector("details")).toBeNull();
    expect(skillDetail("x", COPY).querySelector("pre")).toBeNull();      // nothing read before open
  });

  it("draws the text, a gone skill, and an unreadable one", async () => {
    const body = document.createElement("div");
    expect(await fillSkill(body, "deploy", COPY, answer({ body: "# Deploy\nsteps", error: null }))).toBe("text");
    expect(body.querySelector("pre").textContent).toContain("# Deploy");
    expect(await fillSkill(body, "x", COPY, answer({ body: "" }, false, 404))).toBe("gone");
    expect(body.textContent).toBe("Gone.");
    expect(await fillSkill(body, "x", COPY, answer({ body: null, error: "disk" }))).toBe("unreadable");
    expect(body.textContent).toContain("Could not read.");
    expect(body.textContent).toContain("disk");
  });
});

describe("What the agent did", () => {
  it("rows, empty and unreadable are three answers", () => {
    const root = document.createElement("div");
    expect(renderEpisodes(root, { items: [{ task: "Write the report", outcome: "done", day: "2026-11-10", summary: "Wrote it" }], error: null }, COPY)).toBe("episodes");
    expect(root.textContent).toContain("Write the report");
    expect(root.textContent).toContain("the last 1 runs");
    expect(renderEpisodes(root, { items: [], error: null }, COPY)).toBe("empty");
    expect(root.textContent).toContain("No runs yet.");
    expect(renderEpisodes(root, { items: null, error: "locked" }, COPY)).toBe("unreadable");
    expect(root.textContent).toContain("Could not read the runs.");
    expect(root.textContent).toContain("the last — runs");
  });
});
