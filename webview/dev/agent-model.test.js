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
    expect([...root.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["Use for new chats"]);
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
        now: { model: "deepseek-pro", provider: "deepseek", source: "your default (/model <name> default)" } }) };
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
