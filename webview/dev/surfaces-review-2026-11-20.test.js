// 2026-11-20 surfaces review: a refused memory read is "could not read", and
// "Load older" reads one page at a time.
import { describe, it, expect } from "vitest";
import { renderNotes, renderRecall } from "../static/app/agent.js";
import { Transcript } from "../static/app/transcript.js";

const COPY = { mem_notes_empty: "No notes yet.", mem_notes_error: "Could not read the notes.",
               mem_recall_empty: "Nothing recalled.", mem_recall_error: "Could not read recall.",
               load_older: "Load older" };

describe("a refused memory read", () => {
  it("is never drawn as an empty list", () => {
    const r = document.createElement("div");
    expect(renderNotes(r, { error: "403" }, COPY)).toBe("error");
    expect(r.textContent).not.toContain("No notes yet.");
    const q = document.createElement("div");
    expect(renderRecall(q, { error: "500" }, COPY)).toBe("error");
    expect(q.textContent).not.toContain("Nothing recalled.");
  });
});

describe("Load older", () => {
  it("runs one load at a time", async () => {
    const thread = document.createElement("div");
    const tx = new Transcript(thread, COPY);
    let calls = 0;
    let release;
    const load = () => { calls += 1; return new Promise((r) => { release = r; }); };
    const row = tx.olderControl(true, load);
    const b = row.querySelector("button");
    b.click(); b.click();
    expect(calls).toBe(1);
    expect(b.disabled).toBe(true);
    release();
    await new Promise((r) => setTimeout(r, 0));
    expect(b.disabled).toBe(false);
    b.click();
    expect(calls).toBe(2);
  });
});

import { renderIdentity } from "../static/app/agent.js";

describe("Agent › Identity", () => {
  it("shows the character the model speaks as, read only", () => {
    const r = document.createElement("div");
    renderIdentity(r, { character: "Persona: character 'x'", soul: "", self: "" },
      { id_character_title: "The character it speaks as", id_persona_empty: "None yet." });
    expect(r.textContent).toContain("The character it speaks as");
    expect(r.querySelector("pre").textContent).toBe("Persona: character 'x'");
  });
});

describe("a chat-box verb answer", () => {
  it("keeps its lines and draws bold and code, never HTML", () => {
    const tx = new Transcript(document.createElement("div"), {});
    const note = tx._commandNote({ command_reply: "**New chats:** glm\n`/model x`\n\n<b>no</b>" });
    expect(note.querySelector("strong").textContent).toBe("New chats:");
    expect(note.querySelector("code").textContent).toBe("/model x");
    expect(note.querySelector("b")).toBe(null);
    expect(note.classList.contains("command-note")).toBe(true);
  });
});
