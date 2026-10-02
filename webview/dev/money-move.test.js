// Money › Moves › Make a move (static/app/money-move.js): pickers fill the
// form; a quote answers with an action card; its buttons post the tap.
import { describe, it, expect } from "vitest";
import { mount, quoteBody, tokenOptions } from "../static/app/money-move.js";

const PICKERS = {
  chains: ["base", "ethereum"], swap_chains: ["base"],
  tokens: [{ chain: "base", address: "0xUSDC", symbol: "USDC" },
           { chain: "ethereum", address: "0xDAI", symbol: "DAI" }],
  recipients: ["0xFRIEND"], unreadable: [],
};

function section() {
  document.body.innerHTML = `
    <section id="money-move" data-unreachable="down" data-unreadable="partial" data-native="native">
      <form id="money-move-form">
        <select name="verb"><option value="send">Send</option><option value="swap">Swap</option></select>
        <select name="chain"></select>
        <input name="amount"><input name="token">
        <label data-for="send"><input name="to"></label>
        <label data-for="swap" hidden><input name="token_out"></label>
        <label data-for="swap" hidden><input name="slippage"></label>
        <datalist id="money-move-tokens"></datalist><datalist id="money-move-recipients"></datalist>
        <button type="submit">Get a quote</button>
      </form>
      <div id="money-move-result"></div>
    </section>`;
  return document.getElementById("money-move");
}

const jsonOk = (body) => async () => ({ ok: true, status: 200, json: async () => body });
const tick = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0)); };

describe("make a move", () => {
  it("builds a send body, and a swap buys token_out", () => {
    expect(quoteBody({ verb: "send", amount: "1", token: "native", chain: "base", to: "0xA" }))
      .toEqual({ verb: "send", amount: "1", token: "native", chain: "base", to: "0xA" });
    expect(quoteBody({ verb: "swap", amount: "1", token: "native", chain: "base",
                       token_out: "0xUSDC", slippage: "50" }).to).toBe("0xUSDC");
  });

  it("offers native plus the trusted tokens of the chosen chain only", () => {
    expect(tokenOptions(PICKERS, "base").map((o) => o.value)).toEqual(["native", "0xUSDC"]);
  });

  it("fills pickers, and swap narrows chains and shows the swap fields", async () => {
    const s = section();
    await mount(s, { fetcher: jsonOk(PICKERS) });
    const form = s.querySelector("form");
    expect([...form.elements.chain.options].map((o) => o.value)).toEqual(["base", "ethereum"]);
    expect(s.querySelector("#money-move-recipients option").value).toBe("0xFRIEND");
    form.elements.verb.value = "swap";
    form.elements.verb.dispatchEvent(new Event("change"));
    expect([...form.elements.chain.options].map((o) => o.value)).toEqual(["base"]);
    expect(s.querySelector('[data-for="swap"]').hidden).toBe(false);
    expect(s.querySelector('[data-for="send"]').hidden).toBe(true);
  });

  it("a quote draws the card; Confirm posts the tap and redraws", async () => {
    const s = section();
    const posts = [];
    const card = { id: "0123456789", title: "/send — quote", text: "quote", state: "open",
                   buttons: [{ label: "Confirm", act: "ok", primary: true }] };
    const postFetcher = async (url, init) => {
      posts.push(url);
      const body = url.endsWith("/quote") ? { ok: true, message: "q", card }
        : { ok: true, message: "SENT", card: { ...card, state: "done", buttons: [], text: "✅ Done." } };
      return { ok: true, status: 200, json: async () => body };
    };
    await mount(s, { fetcher: jsonOk(PICKERS), postFetcher });
    const form = s.querySelector("form");
    form.elements.amount.value = "1"; form.elements.token.value = "native";
    form.elements.to.value = "0xFRIEND";
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    await tick();
    expect(posts[0]).toBe("/api/webgate/cards/quote");
    s.querySelector("#money-move-result button").click();
    await tick();
    expect(posts[1]).toBe("/api/webgate/cards/0123456789/ok");
    expect(s.querySelector("#money-move-result button")).toBe(null);
    expect(s.querySelector("#money-move-result").textContent).toContain("SENT");
  });
});
