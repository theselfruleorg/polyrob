// 043 residue W2 — `postJson` is the ONE console mutation, so it must be able
// to carry the ONE thing a multitenant seat needs and a single-tenant one does
// not: an `Authorization` header.
//
// ⚠️ Read the header for what it is. It is the multitenant caller's SEAT (a
// bearer token the auth layer issued), NOT a CSRF defence — this console has no
// CSRF token at all, and `webgate.csrf_guard` is a same-origin check. A reader
// who mistakes one for the other concludes a protection exists that does not.
//
// The cookie path is unchanged: `credentials: 'include'` still rides on every
// call, so the single-tenant console keeps working with no header at all.
import { describe, it, expect } from "vitest";
import { getJson, jsonHeaders, postJson, readJson } from "../static/app/http.js";

function recorder(body, { ok = true, status = 200, json } = {}) {
  const calls = [];
  const fetcher = async (url, init) => {
    calls.push({ url, init });
    return { ok, status, json: json || (async () => body) };
  };
  return { fetcher, calls };
}

describe("the headers", () => {
  it("is JSON with no caller headers at all", async () => {
    const { fetcher, calls } = recorder({ ok: true });
    await postJson("/api/webgate/pause", { scope: "all" }, { fetcher });
    expect(calls[0].init.headers["Content-Type"]).toBe("application/json");
    expect(calls[0].init.headers.Authorization).toBeUndefined();
  });

  it("carries an Authorization header when the caller passes one", async () => {
    const { fetcher, calls } = recorder({ ok: true });
    await postJson("/api/webgate/pause", { scope: "all" }, {
      fetcher, headers: { Authorization: "Bearer x" },
    });
    expect(calls[0].init.headers.Authorization).toBe("Bearer x");
    // …and the body is still described correctly.
    expect(calls[0].init.headers["Content-Type"]).toBe("application/json");
  });

  it("does not leak a caller's headers into the next call", async () => {
    // `jsonHeaders()` returns a fresh object per call; a merge that mutated it
    // would put one seat's token on the next seat's request.
    const { fetcher, calls } = recorder({ ok: true });
    await postJson("/a", {}, { fetcher, headers: { Authorization: "Bearer x" } });
    await postJson("/b", {}, { fetcher });
    expect(calls[1].init.headers.Authorization).toBeUndefined();
    expect(jsonHeaders().Authorization).toBeUndefined();
  });

  it("lets the caller win a collision", async () => {
    const { fetcher, calls } = recorder({ ok: true });
    await postJson("/a", {}, { fetcher, headers: { "Content-Type": "application/json; charset=utf-8" } });
    expect(calls[0].init.headers["Content-Type"])
      .toBe("application/json; charset=utf-8");
  });
});

describe("the call", () => {
  it("still sends the cookie", async () => {
    const { fetcher, calls } = recorder({ ok: true });
    await postJson("/a", {}, { fetcher, headers: { Authorization: "Bearer x" } });
    expect(calls[0].init.credentials).toBe("include");
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].init.body).toBe("{}");
  });

  it("sends no body when there is none", async () => {
    const { fetcher, calls } = recorder({ ok: true });
    await postJson("/a", undefined, { fetcher });
    expect(calls[0].init.body).toBeUndefined();
  });
});

describe("the answer", () => {
  it("carries what came back", async () => {
    const { fetcher } = recorder({ ok: true, message: "done" });
    expect(await postJson("/a", {}, { fetcher }))
      .toEqual({ ok: true, status: 200, body: { ok: true, message: "done" } });
  });

  it("does not throw on a refusal", async () => {
    const { fetcher } = recorder({ detail: "no" }, { ok: false, status: 403 });
    const res = await postJson("/a", {}, { fetcher });
    expect(res.ok).toBe(false);
    expect(res.status).toBe(403);
    expect(res.body).toEqual({ detail: "no" });
  });

  it("answers body:null when the endpoint answered no JSON", async () => {
    const { fetcher } = recorder(null, {
      ok: false, status: 502,
      json: async () => { throw new Error("not JSON"); },
    });
    const res = await postJson("/a", {}, { fetcher });
    expect(res.ok).toBe(false);
    expect(res.status).toBe(502);
    expect(res.body).toBe(null);
  });
});

describe("070 E.31 — a busy 429 has console words", () => {
  it("a busy 429 maps to the busy line", async () => {
    const { serverAnswer, codeAnswer } = await import("../static/app/http.js");
    document.body.innerHTML = '<div id="shell-copy" data-busy="The agent is busy. Try again in a minute."></div>';
    const body = { success: false, code: "busy", error: "Rate limit exceeded", retry_after: 60 };
    expect(codeAnswer(body)).toBe("The agent is busy. Try again in a minute.");
    expect(serverAnswer(body, "fallback")).toBe("The agent is busy. Try again in a minute.");
    expect(serverAnswer({ error: "other" }, "fallback")).toBe("other");
  });
});

describe("FE13 — a FastAPI 422 detail reads as words", () => {
  const V422 = { detail: [
    { loc: ["body", "amount"], msg: "Input should be a valid number", type: "float_parsing" },
    { loc: ["body", "to"], msg: "Field required", type: "missing" },
  ] };

  it("detailText keeps the msg sentences of an array detail", async () => {
    const { detailText } = await import("../static/app/http.js");
    expect(detailText(V422.detail)).toBe("Input should be a valid number Field required");
    expect(detailText({ message: "nope" })).toBe("nope");
    expect(detailText("plain")).toBe("plain");
    expect(detailText(undefined)).toBe("");
  });

  it("serverAnswer never shows JSON or [object Object] for a 422", async () => {
    const { serverAnswer } = await import("../static/app/http.js");
    const out = serverAnswer(V422, "fallback");
    expect(out).toBe("Input should be a valid number Field required");
    expect(out).not.toContain("{");
    expect(serverAnswer({ error: "bad", detail: [{ msg: "why" }] })).toBe("bad why");
  });

  it("an upload refused with a 422 array names the reason", async () => {
    const { uploadOne } = await import("../static/app/file-attach.js");
    const fetcher = async () => ({ ok: false, status: 422, json: async () => V422 });
    const res = await uploadOne("s1", new File(["x"], "a.txt"), { attach_failed: "failed {name}" }, { fetcher });
    expect(res.ok).toBe(false);
    expect(res.message).toBe("Input should be a valid number Field required");
    expect(res.message).not.toContain("[object Object]");
  });
});

describe("FE10 — only the latest read draws", () => {
  it("an overtaken answer that lands last is stale", async () => {
    const { latestOnly } = await import("../static/app/http.js");
    const gates = [];
    const read = latestOnly((cls) => new Promise((resolve) => gates.push(() => resolve(cls))));
    const first = read("money");
    const second = read("work");
    await new Promise((r) => setTimeout(r, 0));
    gates[1](); // the newer answer arrives first
    gates[0](); // the older answer arrives last
    expect(await second).toEqual({ stale: false, value: "work" });
    expect(await first).toEqual({ stale: true });
  });

  it("an overtaken error is dropped; the latest error rejects", async () => {
    const { latestOnly } = await import("../static/app/http.js");
    let n = 0;
    const read = latestOnly(async () => { n += 1; throw new Error(`e${n}`); });
    const a = read();
    const b = read();
    expect(await a).toEqual({ stale: true });
    await expect(b).rejects.toThrow("e2");
  });
});

// 113 W5 item 6 (C-09) — `getJson` is the ONE read helper; the six local copies
// in agent, money, work-apps, work-now, work-goals and workpane are gone.
describe("getJson / readJson", () => {
  const answer = (body, ok = true, status = 200) => {
    const calls = [];
    const fetcher = async (url, init) => {
      calls.push({ url, init });
      return { ok, status, json: async () => { if (body === undefined) throw new Error("no json"); return body; } };
    };
    return { calls, fetcher };
  };

  it("returns {ok, status, body} with the cookie and no Content-Type", async () => {
    const { calls, fetcher } = answer({ a: 1 });
    expect(await getJson("/api/x", { fetcher })).toEqual({ ok: true, status: 200, body: { a: 1 } });
    expect(calls[0].init.credentials).toBe("include");
    expect(calls[0].init.headers).toBeUndefined();
  });

  it("carries a refusal's body and a non-JSON answer as null", async () => {
    expect(await getJson("/x", answer({ detail: "no" }, false, 403))).toEqual(
      { ok: false, status: 403, body: { detail: "no" } });
    expect((await getJson("/x", answer(undefined, false, 502))).body).toBeNull();
  });

  it("rejects on a network failure, as postJson does", async () => {
    await expect(getJson("/x", { fetcher: async () => { throw new Error("offline"); } }))
      .rejects.toThrow("offline");
  });

  it("readJson: the body, or {error: status}; allow404 keeps a 404 body", async () => {
    expect(await readJson("/x", answer({ a: 1 }).fetcher)).toEqual({ a: 1 });
    expect(await readJson("/x", answer({ a: 1 }, false, 500).fetcher)).toEqual({ error: "500" });
    expect(await readJson("/x", answer({ gone: true }, false, 404).fetcher, { allow404: true }))
      .toEqual({ gone: true });
    await expect(readJson("/x", answer(undefined).fetcher)).rejects.toThrow("not JSON");
  });

  it("no destination module keeps its own copy", async () => {
    const { readFileSync } = await import("node:fs");
    const { resolve } = await import("node:path");
    for (const name of ["agent", "money", "work-apps", "work-now", "work-goals", "workpane"]) {
      const src = readFileSync(resolve(process.cwd(), "..", "static", "app", `${name}.js`), "utf8");
      expect(src, name).not.toMatch(/function getJson\(/);
    }
  });
});
