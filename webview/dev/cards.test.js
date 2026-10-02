// Action cards in the Inbox (webview/static/app/cards.js): the server draws the
// words; a click posts the tap; the card is redrawn from the answer.
import { describe, it, expect } from "vitest";
import { drawCard, load, press, pressUrl } from "../static/app/cards.js";

const CARD = {
  id: "0123456789", kind: "quote", origin: "system", state: "open",
  title: "/send — quote", text: "transfer … Confirm: /card_0123456789_ok",
  buttons: [{ label: "Confirm", act: "ok", primary: true },
            { label: "Cancel", act: "no", danger: true }],
};

function section(readOnly = false) {
  const s = document.createElement("div");
  s.id = "inbox-cards";
  s.hidden = true;
  s.dataset.readOnly = readOnly ? "true" : "false";
  s.dataset.unreachable = "That did not reach me.";
  s.dataset.unreadable = "The card store could not be read.";
  const list = document.createElement("div");
  list.setAttribute("data-cards-list", "");
  s.appendChild(list);
  return s;
}

const json = (body, status = 200) => async () =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("inbox cards", () => {
  it("draws the server's text and buttons", () => {
    const e = drawCard(CARD);
    expect(e.querySelector("pre").textContent).toContain("/card_0123456789_ok");
    expect([...e.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["Confirm", "Cancel"]);
  });

  it("draws no buttons read-only", () => {
    expect(drawCard(CARD, { readOnly: true }).querySelector("button")).toBe(null);
  });

  it("encodes the press url", () => {
    expect(pressUrl("ab/c", "ok")).toBe("/api/webgate/cards/ab%2Fc/ok");
  });

  it("an unreadable store is said, never an empty list", async () => {
    const s = section();
    await load(s, { fetcher: json({ readable: false, cards: [] }) });
    expect(s.hidden).toBe(false);
    expect(s.textContent).toContain("could not be read");
  });

  it("hides the section when nothing is open", async () => {
    const s = section();
    expect(await load(s, { fetcher: json({ readable: true, cards: [] }) })).toBe(0);
    expect(s.hidden).toBe(true);
  });

  it("a tap redraws the card from the answer (no buttons once decided)", async () => {
    const s = section();
    const decided = { ...CARD, state: "done", buttons: [], text: "✅ Done." };
    await load(s, { fetcher: json({ readable: true, cards: [CARD] }),
                    postFetcher: async () => ({ ok: true, status: 200,
                      json: async () => ({ ok: true, message: "Sent.", card: decided }) }) });
    s.querySelector("button").click();
    for (let i = 0; i < 4; i++) await new Promise((r) => setTimeout(r, 0));
    expect(s.querySelector("button")).toBe(null);
    expect(s.querySelector(".entry-answer").textContent).toBe("Sent.");
    expect(s.querySelector("pre").textContent).toBe("✅ Done.");
  });

  it("press never throws", async () => {
    const r = await press("x", "ok", { fetcher: async () => { throw new Error("down"); },
                                       fallback: "unreachable" });
    expect(r).toEqual({ ok: false, message: "unreachable", card: null });
  });
});
