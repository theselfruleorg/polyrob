// 113 W6 item 5 (G-4): the model for new chats on Agent › Settings.
import { describe, it, expect, vi } from "vitest";
import { renderModel, loadModel } from "../static/app/agent-model.js";

const COPY = { title: "Model for new chats", now: "New chats start on {model} ({provider}).",
  source: "Chosen by: {source}", label: "Model name or alias", save: "Use for new chats",
  reset: "Use the operator's default", unreadable: "I could not read your model choice.", why: "Why" };
const READ = { readable: true, provider: "deepseek", model: "deepseek-flash", source: "the provider default",
  mine: false, suggestions: ["deepseek-pro"], can_set: true };
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("the model for new chats", () => {
  it("says what new chats start on and who chose it", () => {
    const root = document.createElement("section");
    expect(renderModel(root, READ, COPY, () => {})).toBe("form");
    expect(root.textContent).toContain("New chats start on deepseek-flash (deepseek).");
    expect(root.textContent).toContain("Chosen by: the provider default");
    expect(root.querySelector("datalist option").value).toBe("deepseek-pro");
    expect([...root.querySelectorAll(".model-form:not(.model-tiers *) button")]
      .filter((b) => !b.closest(".model-tiers")).map((b) => b.textContent)).toEqual(["Use for new chats"]);
  });

  it("draws no control when the server says it cannot set it", () => {
    const root = document.createElement("section");
    expect(renderModel(root, { ...READ, can_set: false }, COPY, () => {})).toBe("read");
    expect(root.querySelector("form")).toBeNull();
  });

  it("an unreadable choice is named, never the operator default as yours", () => {
    const root = document.createElement("section");
    expect(renderModel(root, { readable: false, error: "TOMLDecodeError" }, COPY, () => {})).toBe("unreadable");
    expect(root.textContent).toContain("I could not read your model choice.");
    expect(root.textContent).not.toContain("New chats start on");
  });

  it("posts the name, shows the pipeline's answer and the new choice", async () => {
    document.body.innerHTML = '<section id="r"></section>';
    const root = document.getElementById("r");
    const fetcher = vi.fn(async (url, init) => {
      if (!init || init.method !== "POST") return { ok: true, json: async () => ({ ...READ, mine: true }) };
      return { ok: true, status: 200, json: async () => ({ ok: true, message: "✅ Model: deepseek-pro (deepseek)",
        now: { model: "deepseek-pro", provider: "deepseek", source: "your saved model (/model <name>)" } }) };
    });
    await loadModel(root, COPY, fetcher);
    root.querySelector("input").value = "deepseek-pro";
    root.querySelector("form").dispatchEvent(new Event("submit", { cancelable: true }));
    await flush(); await flush();
    const post = fetcher.mock.calls.find((c) => c[1] && c[1].method === "POST");
    expect(post[0]).toBe("/api/webgate/model");
    expect(JSON.parse(post[1].body)).toEqual({ name: "deepseek-pro" });
    expect(root.querySelector("[role=status]").textContent).toBe("✅ Model: deepseek-pro (deepseek)");
    expect(root.textContent).toContain("New chats start on deepseek-pro (deepseek).");
    root.querySelector("button.btn-quiet").click();
    await flush(); await flush();
    expect(JSON.parse(fetcher.mock.calls.filter((c) => c[1] && c[1].method === "POST")[1][1].body)).toEqual({ reset: true });
  });
});


// 121: the heavy and light models and what the agent may choose.
import { renderTiers } from "../static/app/agent-model.js";

describe("the heavy and light models", () => {
  const TCOPY = { tiers_title: "Heavy and light", tier_heavy: "Set as heavy", tier_light: "Set as light",
    tier_reset_heavy: "Heavy: operator", tier_reset_light: "Light: operator",
    choice_free: "Any model", choice_listed: "Only listed" };

  it("shows the /model lines, and no control where it cannot set", () => {
    const sec = renderTiers({ settings: ["Heavy model: glm", "Light model: flash"], can_set: false },
                            TCOPY, () => {});
    expect(sec.querySelector("pre").textContent).toBe("Heavy model: glm\nLight model: flash");
    expect(sec.querySelector("button")).toBe(null);
  });

  it("posts the /model words and shows the pipeline's answer", async () => {
    const calls = [];
    const save = async (p) => { calls.push(p); return { ok: true, message: "✅ heavy: x", settings: ["Heavy model: x"] }; };
    const sec = renderTiers({ settings: [], can_set: true }, TCOPY, save);
    sec.querySelector("input").value = "glm-4.6";
    const byText = (t) => [...sec.querySelectorAll("button")].find((b) => b.textContent === t);
    byText("Set as heavy").click();
    await flush();
    byText("Only listed").click();
    await flush();
    expect(calls).toEqual([{ tier: "heavy", name: "glm-4.6" }, { choice: "listed" }]);
    expect(sec.querySelector("pre").textContent).toBe("Heavy model: x");
    expect(sec.querySelector("[role=status]").textContent).toBe("✅ heavy: x");
  });

  it("a set with no name posts nothing", async () => {
    const calls = [];
    const sec = renderTiers({ settings: [], can_set: true }, TCOPY, async (p) => { calls.push(p); return {}; });
    [...sec.querySelectorAll("button")].find((b) => b.textContent === "Set as light").click();
    await flush();
    expect(calls).toEqual([]);
  });
});
