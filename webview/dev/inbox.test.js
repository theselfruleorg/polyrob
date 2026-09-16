// 043 C5 fix round 1 — the Inbox's decision buttons actually decide.
//
// ⚠️ The regression: `inbox.html` emitted `data-verb` buttons and NOTHING bound
// them, on every posture. A decision surface whose buttons do nothing is worse
// than one with no buttons, because it looks like the owner acted.
//
// What is tested here is the part only the client can get wrong: the route it
// builds from an id that is not URL-safe, the CSRF header the console's one
// mutation guard reads, and — the whole point — that a refusal REACHES THE CARD
// instead of a console error nobody sees.
import { describe, it, expect } from "vitest";
import { applyResult, decide, decisionUrl } from "../static/app/inbox.js";
import { jsonHeaders, postJson } from "../static/app/http.js";

function card(html) {
  document.body.innerHTML =
    `<div class="ledger"><div class="entry">${html}</div></div>`;
  return document.querySelector(".entry");
}

function reply(body, { ok = true, status = 200 } = {}) {
  const calls = [];
  const fetcher = async (url, init) => {
    calls.push({ url, init });
    return { ok, status, json: async () => body };
  };
  return { fetcher, calls };
}

describe("the route", () => {
  it("encodes an id that is not URL-safe", () => {
    // A correspondent's id IS `surface:address`; an app slug is a path segment.
    expect(decisionUrl("correspondent", "telegram:12345", "decide"))
      .toBe("/api/webgate/inbox/correspondent/telegram%3A12345/decide");
    expect(decisionUrl("app", "a/b", "reject"))
      .toBe("/api/webgate/inbox/app/a%2Fb/reject");
  });

  it("posts to the verb it was given", async () => {
    const { fetcher, calls } = reply({ ok: true, message: "done" });
    await decide("ask", "g7", "fulfill", { fetcher });
    expect(calls[0].url).toBe("/api/webgate/inbox/ask/g7/fulfill");
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].init.credentials).toBe("include");
  });
});

describe("what a mutation actually sends", () => {
  // ⚠️ There is no CSRF TOKEN in this console. `webgate.csrf_guard` is a
  // SAME-ORIGIN check: on a mutating method it compares the request's Origin
  // (else Referer) host against its own Host, and Host is browser-forbidden, so
  // a cross-origin page cannot make them agree. Nothing mints a `polyrob_csrf`
  // cookie and nothing reads an `X-CSRF-Token` header — code claiming otherwise
  // reads as a defence that is there.
  it("sends no token header, because nothing on the server reads one", () => {
    expect(jsonHeaders()).toEqual({ "Content-Type": "application/json" });
  });

  it("posts same-origin with credentials, which is what the guard needs", async () => {
    const calls = [];
    const fetcher = async (url, init) => {
      calls.push({ url, init });
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    };
    await postJson("/api/webgate/inbox/ask/g7/decide", undefined, { fetcher });
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].init.credentials).toBe("include");
    expect(calls[0].init.headers["X-CSRF-Token"]).toBe(undefined);
  });

  it("carries what came back, refusal included, without throwing", async () => {
    const fetcher = async () => ({
      ok: false, status: 403, json: async () => ({ detail: "read-only" }),
    });
    expect(await postJson("/x", undefined, { fetcher }))
      .toEqual({ ok: false, status: 403, body: { detail: "read-only" } });
  });

  it("answers a null body rather than throwing on a non-JSON reply", async () => {
    const fetcher = async () => ({
      ok: false, status: 500, json: async () => { throw new Error("html"); },
    });
    expect((await postJson("/x", undefined, { fetcher })).body).toBe(null);
  });
});

describe("what comes back", () => {
  it("passes the endpoint's own words through, success or refusal", async () => {
    const yes = reply({ ok: true, message: "Done. One goal can run again." });
    expect(await decide("ask", "g7", "fulfill", { fetcher: yes.fetcher }))
      .toEqual({ ok: true, message: "Done. One goal can run again." });

    const no = reply({ ok: false, message: "That one is no longer open." });
    expect(await decide("ask", "g7", "fulfill", { fetcher: no.fetcher }))
      .toEqual({ ok: false, message: "That one is no longer open." });
  });

  it("reads a FastAPI refusal detail rather than inventing one", async () => {
    const { fetcher } = reply({ detail: "Console is read-only" },
                               { ok: false, status: 403 });
    const out = await decide("ask", "g7", "fulfill", { fetcher });
    expect(out).toEqual({ ok: false, message: "Console is read-only" });
  });

  it("falls back to the SERVER's sentence when the click never arrived", async () => {
    const fetcher = async () => { throw new Error("offline"); };
    const out = await decide("ask", "g7", "fulfill",
                             { fetcher, fallback: "That did not reach me." });
    expect(out).toEqual({ ok: false, message: "That did not reach me." });
  });

  it("never throws, whatever the transport did", async () => {
    const fetcher = async () => ({ ok: false, status: 500,
                                   json: async () => { throw new Error("html"); } });
    const out = await decide("ask", "g7", "fulfill", { fetcher });
    expect(out.ok).toBe(false);
    expect(out.message).toBe("500");
  });
});

describe("the card", () => {
  it("shows the answer where the decision was made", () => {
    const node = card('<h2 class="entry-title">x</h2>'
      + '<div class="entry-actions"><button data-verb="fulfill"></button></div>');
    applyResult(node, { ok: true, message: "Done." });
    expect(node.querySelector(".entry-answer").textContent).toBe("Done.");
  });

  it("retires the buttons a decision consumed", () => {
    const node = card('<div class="entry-actions"><button data-verb="x"></button></div>');
    applyResult(node, { ok: true, message: "Done." });
    expect(node.querySelector(".entry-actions")).toBe(null);
    expect(node.dataset.decided).toBe("true");
  });

  it("keeps the buttons when the decision was REFUSED", () => {
    const node = card('<div class="entry-actions"><button data-verb="x"></button></div>');
    applyResult(node, { ok: false, message: "no such item" });
    expect(node.querySelector(".entry-actions")).not.toBe(null);
    expect(node.querySelector(".entry-answer").dataset.ok).toBe("false");
  });

  it("replaces the previous answer instead of stacking them", () => {
    const node = card('<div class="entry-actions"><button data-verb="x"></button></div>');
    applyResult(node, { ok: false, message: "first" });
    applyResult(node, { ok: false, message: "second" });
    expect(node.querySelectorAll(".entry-answer").length).toBe(1);
    expect(node.querySelector(".entry-answer").textContent).toBe("second");
  });

  it("writes text, never markup, so a refusal can never be HTML", () => {
    const node = card("<div></div>");
    applyResult(node, { ok: false, message: '<img src=x onerror="alert(1)">' });
    expect(node.querySelector("img")).toBe(null);
  });
});
