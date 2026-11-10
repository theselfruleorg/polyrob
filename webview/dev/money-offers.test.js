// 113 W6 item 6 (G-2): offers and orders on Money › Invoices.
import { describe, it, expect, vi } from "vitest";
import { renderOffers, loadOffers } from "../static/app/money-offers.js";

const COPY = { title: "What the agent sells", unreadable: "I could not read the offers. This is not the same as no offers.",
  why: "Why", empty: "The agent sells nothing yet.", sold: "{count} sold", pause: "Pause", resume: "Resume",
  orders_title: "Orders", orders_empty: "No orders yet.", state_live: "On sale", state_paused: "Paused",
  state_draft: "Draft", order_paid: "Paid", can_write: "1" };
const BODY = { readable: true, offers: [
  { id: "custom-art", state: "live", title: "Custom art NFT", sold: 3 },
  { id: "logo", state: "paused", title: "Logo", sold: 0 },
  { id: "draft-1", state: "draft", title: "Draft one", sold: 0 }],
  orders: [{ id: "ord_1", offer_id: "custom-art", status: "paid", amount_usd: "5.00", channel: "telegram", error: null }] };
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("offers and orders", () => {
  it("lists each offer with its state and what it sold; only live and paused have a button", () => {
    const root = document.createElement("section");
    expect(renderOffers(root, BODY, COPY, () => {})).toBe("offers");
    const entries = root.querySelectorAll("[data-offer]");
    expect(entries[0].textContent).toContain("On sale");
    expect(entries[0].textContent).toContain("3 sold");
    expect(entries[0].querySelector("button").textContent).toBe("Pause");
    expect(entries[1].querySelector("button").textContent).toBe("Resume");
    expect(entries[2].querySelector("button")).toBeNull();
    expect(root.querySelector('[data-order="ord_1"]').textContent).toContain("Paid");
    expect(root.querySelector('[data-order="ord_1"]').textContent).toContain("$5.00");
  });

  it("draws no button on a read-only console", () => {
    const root = document.createElement("section");
    renderOffers(root, BODY, { ...COPY, can_write: undefined }, () => {});
    expect(root.querySelector("button")).toBeNull();
  });

  it("an unreadable store is not no offers", () => {
    const root = document.createElement("section");
    expect(renderOffers(root, { readable: false, error: "disk I/O error" }, COPY)).toBe("unreadable");
    expect(root.textContent).toContain("This is not the same as no offers.");
    expect(root.textContent).not.toContain("sells nothing");
  });

  it("no offers yet is said", () => {
    const root = document.createElement("section");
    renderOffers(root, { readable: true, offers: [], orders: [] }, COPY);
    expect(root.textContent).toContain("The agent sells nothing yet.");
    expect(root.textContent).toContain("No orders yet.");
  });

  it("Pause posts the verb and shows its answer", async () => {
    const root = document.createElement("section");
    const fetcher = vi.fn(async (url, init) => {
      if (init && init.method === "POST") return { ok: true, status: 200, json: async () => ({ ok: true, message: "✅ Offer custom-art is PAUSED." }) };
      return { ok: true, json: async () => BODY };
    });
    await loadOffers(root, COPY, fetcher);
    root.querySelector('[data-offer="custom-art"] button').click();
    await flush(); await flush(); await flush();
    const post = fetcher.mock.calls.find((c) => c[1] && c[1].method === "POST");
    expect(post[0]).toBe("/api/webgate/offers/custom-art/pause");
    expect(root.querySelector("[role=status]").textContent).toBe("✅ Offer custom-art is PAUSED.");
  });
});
