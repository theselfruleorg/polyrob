// The 2026-09-21 interface audit, section A — the console half.
//
// One file per audit rather than per module, because what is being pinned is a
// FINDING: the thing the console said that was not true. Each block names its
// id and asserts the corrected reading, not the implementation that produces
// it.
import { describe, it, expect, vi } from "vitest";

import { applyState, pausedFrom, toggle } from "../static/app/pause.js";
import { keepFace, renderIdentity } from "../static/app/agent.js";
import {
  appEntry, artifactsRead, killApp, renderApps,
} from "../static/app/work-apps.js";
import { renderNowNext } from "../static/app/work-now.js";
import { load as loadLog, render as renderLog } from "../static/app/worklog.js";
import { sayReach } from "../static/app/palette.js";
import { buildFiles, fileHref, renderFiles } from "../static/app/workpane.js";
import { renderInvoices } from "../static/app/money.js";
import { answerFor, decide } from "../static/app/inbox.js";
import { uploadOne } from "../static/app/file-attach.js";

const root = () => {
  const node = document.createElement("div");
  document.body.appendChild(node);
  return node;
};

const answered = (body, ok = true, status = 200) => vi.fn(async () => ({
  ok, status, json: async () => body,
}));

// --- A2: the pause control the head line has always named ------------------- #

describe("A2 — the head's Pause/Resume control", () => {
  const COPY = { pause: "Pause", resume: "Resume", pause_label: "Pause or resume",
                 pause_unreachable: "The request did not reach the console." };

  const button = () => {
    const b = document.createElement("button");
    b.innerHTML = '<span class="glyph"></span><span class="kbd"></span>';
    return b;
  };

  it("offers Resume while paused and Pause while running", () => {
    const b = button();
    applyState(b, true, COPY);
    expect(b.querySelector(".kbd").textContent).toBe("Resume");
    expect(b.dataset.verb).toBe("resume");
    applyState(b, false, COPY);
    expect(b.querySelector(".kbd").textContent).toBe("Pause");
    expect(b.dataset.verb).toBe("pause");
  });

  it("an unknown state offers NO verb — never a cheerful 'running'", () => {
    const b = button();
    expect(applyState(b, null, COPY)).toBeNull();
    expect(b.dataset.paused).toBe("unknown");
    expect(b.classList.contains("is-uncertain")).toBe(true);
  });

  it("reads the state from the record, in any of the shapes it answers in", () => {
    expect(pausedFrom({ paused: true })).toBe(true);
    expect(pausedFrom({ halted: true })).toBe(true);
    expect(pausedFrom({ state: { paused: false } })).toBe(false);
    // Nothing said about it is UNKNOWN, not "running".
    expect(pausedFrom({})).toBeNull();
    expect(pausedFrom(null)).toBeNull();
  });

  it("reports the state the SERVER read back, not the one it was asked for", async () => {
    // The owner asked to resume; the runtime is still halted by something the
    // console cannot clear. The button must not claim it resumed.
    const fetcher = answered({ ok: true, paused: true, message: "still paused" });
    const res = await toggle("resume", COPY, { fetcher });
    expect(fetcher.mock.calls[0][0]).toBe("/api/webgate/resume");
    expect(res.paused).toBe(true);
    expect(res.message).toBe("still paused");
  });

  it("pauses everything, and says so when the click never arrived", async () => {
    const fetcher = answered({ ok: true, paused: true, message: "paused" });
    await toggle("pause", COPY, { fetcher });
    expect(fetcher.mock.calls[0][0]).toBe("/api/webgate/pause");
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ scopes: ["all"] });

    const offline = vi.fn(async () => { throw new Error("offline"); });
    const res = await toggle("pause", COPY, { fetcher: offline });
    expect(res.paused).toBeNull();
    expect(res.message).toBe(COPY.pause_unreachable);
  });
});

// --- A19: the keep ceremony ------------------------------------------------- #

describe("A19 — keeping the face is one-way, and the body decides", () => {
  const COPY = {
    id_face_title: "Rob's face", id_face_body: "generated once",
    id_reroll: "Make a new face", id_keep: "Keep this face",
    id_keep_hint: "Keeping is permanent.",
    id_kept_title: "Kept for good", id_kept_body: "permanent",
    id_persona_title: "How Rob should behave", id_learned_title: "What Rob learned",
    id_keep_done: "Kept.", id_keep_failed: "I could not keep the face.",
  };

  it("offers Keep beside the reroll while the record is a draft", () => {
    const r = root();
    renderIdentity(r, { soul: "s", self: "x" }, COPY,
      { pfp: { traits: { tier: 1 }, locked: false } });
    expect(r.querySelector("button[data-keep]")).toBeTruthy();
    expect(r.querySelector("button[data-reroll]")).toBeTruthy();
    expect(r.textContent).toContain("Keeping is permanent.");
  });

  it("offers NEITHER once it is kept — a control that can only refuse", () => {
    const r = root();
    renderIdentity(r, { soul: "s", self: "x" }, COPY,
      { pfp: { traits: { tier: 1 }, locked: true } });
    expect(r.querySelector("button[data-keep]")).toBeNull();
    expect(r.querySelector("button[data-reroll]")).toBeNull();
    expect(r.textContent).toContain("Kept for good");
  });

  it("offers no Keep when there is no face to keep", () => {
    const r = root();
    renderIdentity(r, { soul: "s" }, COPY, { pfp: { error: "404" } });
    expect(r.querySelector("button[data-keep]")).toBeNull();
  });

  it("derives ok from the BODY — a 200 refusal is a refusal", async () => {
    const fetcher = answered({ ok: false, message: "the identity is kept" });
    expect(await keepFace(COPY, { fetcher })).toEqual({
      ok: false, message: "the identity is kept",
    });
  });

  it("renders a 409 as the server's own refusal text", async () => {
    const fetcher = answered({ ok: false, error: "no avatar to keep" }, false, 409);
    const res = await keepFace(COPY, { fetcher });
    expect(res.ok).toBe(false);
    expect(res.message).toBe("no avatar to keep");
  });
});

// --- A3 / A22: Work › Apps -------------------------------------------------- #

describe("A3 — a session-less artifact answer is not 'nothing built'", () => {
  const COPY = {
    online_title: "Online", online_empty: "none", empty_title: "Nothing built",
    published_title: "Published", published_unreadable: "I could not read the ledger.",
    made_title: "Made", made_unreadable: "I could not read the ledger.",
  };

  it("null artifacts is NOT READ, with or without an error string", () => {
    expect(artifactsRead({ artifacts: null, error: "session-scoped" })).toBe(false);
    expect(artifactsRead({ artifacts: null, error: null })).toBe(false);
    expect(artifactsRead({ artifacts: [] })).toBe(true);
  });

  it("never fires the empty state over an unread ledger", () => {
    const r = root();
    expect(renderApps(r, { apps: [] },
      { artifacts: null, error: "session-scoped" }, COPY)).toBe("apps");
    expect(r.textContent).not.toContain("Nothing built");
    expect(r.querySelectorAll(".entry.is-unknown").length).toBe(2); // published + made
  });

  it("still says 'nothing built' when every store ANSWERED and was empty", () => {
    const r = root();
    expect(renderApps(r, { apps: [] }, { artifacts: [] }, COPY)).toBe("empty");
    expect(r.textContent).toContain("Nothing built");
  });
});

describe("A22 — a live app can be stopped and its log read", () => {
  const COPY = { status_live: "live", status_unhealthy: "unhealthy",
                 status_pending: "waiting", kill: "Stop it", logs: "Show the log",
                 unreachable: "did not reach the console" };

  it("draws Stop and the log on a live or unhealthy app only", () => {
    for (const status of ["live", "unhealthy"]) {
      const e = appEntry({ slug: "shop", status }, COPY, {});
      expect(e.querySelector('button[data-app-verb="kill"]')).toBeTruthy();
      expect(e.querySelector('button[data-app-verb="logs"]')).toBeTruthy();
    }
    const pending = appEntry({ slug: "shop", status: "pending" }, COPY, {});
    expect(pending.querySelector("button[data-app-verb]")).toBeNull();
  });

  it("draws no Stop under read-only, but keeps the log — a read", () => {
    const e = appEntry({ slug: "shop", status: "live" }, COPY, { readOnly: true });
    expect(e.querySelector('button[data-app-verb="kill"]')).toBeNull();
    expect(e.querySelector('button[data-app-verb="logs"]')).toBeTruthy();
  });

  it("says back exactly what the supervisor answered", async () => {
    const refused = answered({ detail: "the app is not running" }, false, 409);
    expect(await killApp("shop", COPY, { fetcher: refused })).toEqual({
      ok: false, message: "the app is not running",
    });
    const done = answered({ ok: true, message: "stopped" });
    expect(await killApp("shop", COPY, { fetcher: done })).toEqual({
      ok: true, message: "stopped",
    });
  });
});

// --- A6 / A7 / A37: Work › Now ---------------------------------------------- #

describe("A6/A7/A37 — Now renders the live reader, deduped and honest", () => {
  const COPY = {
    empty_now: "not running anything", empty_next: "nothing waiting",
    running_count: "{count} running", running_count_partial: "at least {count} running",
    next_queued: "{count} queued", elapsed_started: "started {elapsed}",
    secs: "{count}s", mins: "{count}m", hours: "{count}h", days: "{count}d",
    session_title: "A chat", open_chat: "Open", cron_title: "A scheduled run",
    live_unreadable: "I could not read every source", live_unreadable_why: "Why",
    unreadable: "I could not read the goal board.", unreadable_why: "Why",
  };
  const ctx = () => ({
    running: root(), next: root(),
    runningAside: root(), nextAside: root(),
  });

  it("draws a running goal the board's WINDOW no longer carries", () => {
    const c = ctx();
    renderNowNext(c, { goals: [], counts: {} }, null, COPY, {
      live: { goals: [{ id: "g1", title: "Old but running", since: 1, session_id: "s1" }],
              cron: [], sessions: [] },
    });
    expect(c.running.textContent).toContain("Old but running");
    expect(c.running.textContent).not.toContain("not running anything");
  });

  it("does not draw the goal's own session a second time", () => {
    const c = ctx();
    renderNowNext(c, { goals: [], counts: {} }, null, COPY, {
      live: {
        goals: [{ id: "g1", title: "Running", session_id: "s1" }],
        cron: [],
        sessions: [{ session_id: "s1", task: "the same run" },
                   { session_id: "s2", task: "a real chat" }],
      },
    });
    expect(c.running.textContent).not.toContain("the same run");
    expect(c.running.textContent).toContain("a real chat");
    expect(c.runningAside.textContent).toBe("2 running");
  });

  it("a partial live read is a FLOOR, and every unread source is named", () => {
    const c = ctx();
    renderNowNext(c, { goals: [], counts: {} }, null, COPY, {
      live: { goals: [{ id: "g1", title: "Running" }], cron: [], sessions: null,
              partial: true, unreadable: ["sessions"] },
    });
    expect(c.runningAside.textContent).toBe("at least 1 running");
    expect(c.running.textContent).toContain("I could not read every source");
  });

  it("falls back to the board's window when the live reader said nothing", () => {
    const c = ctx();
    renderNowNext(c, { goals: [{ id: "g", title: "From the board", status: "running" }],
                       counts: {} }, null, COPY, {});
    expect(c.running.textContent).toContain("From the board");
  });
});

// --- A13: Work › Log switches are REQUEST state ----------------------------- #

describe("A13 — the log switches change what is asked for", () => {
  it("asks for neither raw nor diagnostics by default", async () => {
    const fetcher = answered({ entries: [], unreadable: null });
    await loadLog({}, fetcher);
    expect(fetcher.mock.calls[0][0]).toBe("/api/webgate/log");
  });

  it("puts each switch on the request when it is on", async () => {
    const fetcher = answered({ entries: [], unreadable: null });
    await loadLog({ raw: true, diagnostics: true }, fetcher);
    expect(fetcher.mock.calls[0][0]).toBe("/api/webgate/log?raw=1&diagnostics=1");
  });

  it("names the rows the tenant filter removed instead of dropping them", () => {
    const r = root();
    renderLog(r, {
      entries: [{ cls: "goal", summary: "did a thing", ts: Date.now() / 1000 }],
      unreadable: null, filtered_out: 12,
    }, null, { header_what: "What", header_when: "When",
               filtered_out: "{count} rows belong to another tenant and are not shown." });
    expect(r.textContent).toContain("12 rows belong to another tenant");
  });
});

// --- A18: the palette off a chat screen ------------------------------------- #

describe("A18 — the palette says where a verb runs", () => {
  it("states the reach and does NOT close when there is no composer", () => {
    const dialog = document.createElement("dialog");
    dialog.innerHTML = '<div id="palette-results"></div>';
    document.body.appendChild(dialog);
    const note = sayReach(dialog, { reach: "Type this into a chat to run it." });
    expect(note.textContent).toBe("Type this into a chat to run it.");
    // Calling twice replaces the note rather than stacking one per click.
    sayReach(dialog, { reach: "Type this into a chat to run it." });
    expect(dialog.querySelectorAll("[data-palette-reach]").length).toBe(1);
  });
});

// --- A23: the Work pane's files are reachable ------------------------------- #

describe("A23 — a file the pane lists can be opened", () => {
  const COPY = { tier_ready: "Ready", tier_ready_why: "for you",
                 tier_working: "Working", tier_working_why: "its own",
                 tier_given: "Given", tier_given_why: "by you",
                 verdict_ok: "ok", files_empty: "empty" };

  it("builds the file read url, and nothing when either half is missing", () => {
    expect(fileHref("s1", "out/report.md"))
      .toBe("/api/session/s1/workspace/file?path=out%2Freport.md");
    expect(fileHref("", "a.md")).toBe("");
    expect(fileHref("s1", "")).toBe("");
  });

  it("keeps a deliverable's path so it can be linked, not only named", () => {
    const built = buildFiles(null, [{ path: "out/report.md", verdict: "ok" }]);
    expect(built.ready[0].path).toBe("out/report.md");
  });

  it("links every row on a bound session and never emits a dead anchor", () => {
    const r = root();
    renderFiles(r, null, {
      tree: { children: [{ name: "notes.txt", type: "file" },
                         { name: "inbound", type: "dir",
                           children: [{ name: "brief.pdf", type: "file" }] }] },
      artifacts: [{ path: "out/report.md", verdict: "ok" }],
      sessionId: "s1",
    }, COPY);
    const hrefs = Array.from(r.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(hrefs).toContain("/api/session/s1/workspace/file?path=out%2Freport.md");
    expect(hrefs).toContain("/api/session/s1/workspace/file?path=notes.txt");
    expect(hrefs).toContain("/api/session/s1/workspace/file?path=inbound%2Fbrief.pdf");
    expect(hrefs.every(Boolean)).toBe(true);

    const noSession = root();
    renderFiles(noSession, null, {
      tree: { children: [{ name: "notes.txt", type: "file" }] }, artifacts: [],
    }, COPY);
    expect(noSession.querySelector("a")).toBeNull(); // plain text, not a dead link
  });
});

// --- A35: the outstanding total is the server's ----------------------------- #

describe("A35 — the invoice total is not a sum of one page", () => {
  const COPY = {
    inv_title: "Invoices", inv_aside: "{amount} outstanding", inv_empty: "none",
    inv_col_who: "Who", inv_col_for: "For", inv_col_amount: "How much",
    inv_col_state: "State", inv_state_pending: "waiting", inv_note: "note",
    inv_truncated: "There are more invoices than the ones shown here.",
    inv_unpriced: "{count} outstanding invoices carry no amount I could read, so they are not in that total.",
    inv_machine_title: "Machine income", inv_machine_body: "may undercount",
  };

  it("renders the server total, not the page's own arithmetic", () => {
    const r = root();
    renderInvoices(r, {
      invoices: [{ id: "1", status: "pending", amount_usd: 10 }],
      outstanding_usd_total: 4210.5, outstanding_count: 51, truncated: true,
    }, COPY);
    expect(r.textContent).toContain("4,210.50");
    expect(r.textContent).not.toContain("10.00 outstanding");
    expect(r.textContent).toContain("There are more invoices than the ones shown here.");
  });

  it("names the outstanding invoices the total could not price", () => {
    const r = root();
    renderInvoices(r, {
      invoices: [{ id: "1", status: "pending", amount_usd: 10 }],
      outstanding_usd_total: 10, outstanding_count: 3, outstanding_unpriced: 2,
    }, COPY);
    expect(r.textContent).toContain("2 outstanding invoices carry no amount");
  });

  it("a total the server could not compute is a dash, never a sum", () => {
    const r = root();
    renderInvoices(r, {
      invoices: [{ id: "1", status: "pending", amount_usd: 10 }],
      outstanding_usd_total: null,
    }, COPY);
    expect(r.querySelector(".section-aside").textContent).toContain("—");
  });
});

// --- A27: an ask can be ANSWERED -------------------------------------------- #

describe("A27 — a decision can carry the owner's answer", () => {
  it("reads the answer off the card at click time", () => {
    const card = document.createElement("div");
    card.innerHTML = '<input data-answer="ask1" value="  use the staging key  ">';
    expect(answerFor(card)).toBe("use the staging key");
    expect(answerFor(document.createElement("div"))).toBe("");
  });

  it("sends {answer} with the decision, and no body without one", async () => {
    const fetcher = answered({ ok: true, message: "done" });
    await decide("ask", "a1", "decide", { fetcher, answer: "the staging key" });
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ answer: "the staging key" });

    const plain = answered({ ok: true, message: "done" });
    await decide("app", "shop", "decide", { fetcher: plain });
    expect(plain.mock.calls[0][1].body).toBeUndefined();
  });
});

// --- A24: the composer's file picker ---------------------------------------- #

describe("A24 — a file can be attached again", () => {
  const COPY = { attach_done: "{name} is in this chat's folder.",
                 attach_failed: "I could not take {name}.",
                 attach_unreachable: "The file did not reach the console." };

  it("posts multipart to the session's own upload route", async () => {
    const fetcher = answered({ path: "brief.pdf" }, true, 201);
    const file = new File(["x"], "brief.pdf");
    const res = await uploadOne("s1", file, COPY, { fetcher });
    expect(fetcher.mock.calls[0][0])
      .toBe("/api/task/sessions/s1/workspace/upload");
    const init = fetcher.mock.calls[0][1];
    expect(init.method).toBe("POST");
    expect(init.body instanceof FormData).toBe(true);
    // ⚠️ the browser must write the multipart boundary itself.
    expect(init.headers).toBeUndefined();
    expect(res).toEqual({ ok: true, message: "brief.pdf is in this chat's folder." });
  });

  it("renders the endpoint's own refusal, never a silent drop", async () => {
    const fetcher = answered({ detail: "File type .svg not allowed." }, false, 400);
    const res = await uploadOne("s1", new File(["x"], "a.svg"), COPY, { fetcher });
    expect(res).toEqual({ ok: false, message: "File type .svg not allowed." });
  });

  it("says so when the file never reached the console", async () => {
    const offline = vi.fn(async () => { throw new Error("offline"); });
    const res = await uploadOne("s1", new File(["x"], "a.txt"), COPY,
      { fetcher: offline });
    expect(res).toEqual({ ok: false, message: COPY.attach_unreachable });
  });
});

// --- revalidation, 2026-09-21: residue found re-reading the audit commit ---- #

describe("A9 residue — 'not configured' is not 'unreadable'", () => {
  const COPY = { mem_kb_title: "What Rob has read", mem_kb_aside: "{count} sources",
                 mem_kb_unreadable: "I could not read the sources.",
                 mem_kb_unreadable_why: "Why",
                 mem_kb_empty: "Rob has not read any sources yet." };

  it("draws the empty state with the server's reason when nothing is configured",
    async () => {
      const { renderKb } = await import("../static/app/agent.js");
      const node = root();
      // The shape the knowledge reader answers with when there is no memory
      // provider: a REAL empty list, no error, one sentence in `reason`.
      const drew = renderKb(node, {
        items: [], count: 0, error: null,
        reason: "There is no memory backend configured, so there is nothing to read here.",
      }, COPY);
      expect(drew).toBe("empty");
      expect(node.textContent).toContain("no memory backend configured");
      expect(node.textContent).not.toContain("I could not read the sources.");
    });

  it("still draws unreadable when the store actually refused", async () => {
    const { renderKb } = await import("../static/app/agent.js");
    const node = root();
    const drew = renderKb(node, { items: null, count: null,
                                  error: "OSError: memory.db is locked" }, COPY);
    expect(drew).toBe("unreadable");
    expect(node.textContent).toContain("I could not read the sources.");
  });
});

describe("instance-wide controls are drawn where they can act", () => {
  const COPY = { kill: "Stop", logs: "Show the log", online_open: "Open" };

  it("draws Stop for the owner console and not for a tenant seat", () => {
    const owner = appEntry({ slug: "shop", status: "live" }, COPY,
      { readOnly: false, canDecide: true });
    expect([...owner.querySelectorAll("button")].map((b) => b.dataset.appVerb))
      .toEqual(["logs", "kill"]);
    // ⚠️ `apps_routes._decide` refuses every multitenant caller, so a Stop on
    // that seat is a button whose only answer is 403.
    const tenant = appEntry({ slug: "shop", status: "live" }, COPY,
      { readOnly: false, canDecide: false });
    expect([...tenant.querySelectorAll("button")].map((b) => b.dataset.appVerb))
      .toEqual(["logs"]);
  });

  it("keeps the log on every seat — a read is not a decision", () => {
    const tenant = appEntry({ slug: "shop", status: "unhealthy" }, COPY,
      { readOnly: false, canDecide: false });
    expect(tenant.querySelector('button[data-app-verb="logs"]')).toBeTruthy();
  });

  it("a caller that says nothing about the seat keeps the old behaviour", () => {
    const e = appEntry({ slug: "shop", status: "live" }, COPY, {});
    expect(e.querySelector('button[data-app-verb="kill"]')).toBeTruthy();
  });
});

describe("A13 residue — the class chip asks the server, like its switches", () => {
  it("sends the class filter so `filtered_out` describes the list on screen",
    async () => {
      const seen = [];
      const fetcher = vi.fn(async (url) => {
        seen.push(url);
        return { ok: true, json: async () => ({ entries: [], filtered_out: 4 }) };
      });
      await loadLog({ cls: "money", diagnostics: true }, fetcher);
      expect(seen[0]).toContain("class=money");
      expect(seen[0]).toContain("diagnostics=1");
      // raw stays OFF unless asked — the half A13 already fixed.
      expect(seen[0]).not.toContain("raw=1");
    });

  it("asks for everything when no chip is on", async () => {
    const fetcher = vi.fn(async () => ({ ok: true, json: async () => ({ entries: [] }) }));
    await loadLog({}, fetcher);
    expect(fetcher.mock.calls[0][0]).toBe("/api/webgate/log");
  });
});

describe("Money › Cash — money taken and not delivered on", () => {
  const CASH = { cash_income_what: "Income", cash_spent_what: "Spent",
                 cash_whose: "last {days} days", cash_no_sum: "no single number",
                 cash_net: "Net", cash_paid_invoices: "Paid invoices",
                 cash_waiting: "Waiting", cash_balance_now: "In the treasury",
                 cash_runtime_title: "Runtime", cash_runtime_aside: "your money",
                 cash_runtime_window: "Last {days} days",
                 cash_runtime_total: "Since the start",
                 cash_runtime_calls: "{count} calls",
                 cash_provider_left: "Provider balance", cash_provider_why: "why",
                 cash_refund_due: "{amount} is owed back across {count} payment(s)." };
  const ledger = (treasury) => ({
    window_days: 7, settled_payments: 1, note: null,
    treasury: { income_usd: 10, spend_usd: 1, pending_usd: 0, pending_count: 0,
                balance_usd: 9, net_usd: 9, available: true, ...treasury },
    runtime: { spend_window_usd: 1, spend_total_usd: 2, calls_window: 1,
               calls_total: 2, provider_balance_usd: null, available: true },
    caps: {},
  });

  it("names a refund the owner owes back", async () => {
    const { renderCash } = await import("../static/app/money.js");
    const node = root();
    renderCash(node, ledger({ refund_due_usd: 12.5, refund_due_count: 2 }), CASH);
    const line = node.querySelector("[data-refund-due]");
    expect(line).toBeTruthy();
    expect(line.textContent).toContain("$12.50");
    expect(line.textContent).toContain("2 payment(s)");
  });

  it("says nothing when nothing is owed — a zero is not a debt", async () => {
    const { renderCash } = await import("../static/app/money.js");
    const node = root();
    renderCash(node, ledger({ refund_due_usd: 0, refund_due_count: 0 }), CASH);
    expect(node.querySelector("[data-refund-due]")).toBeNull();
    // …and an older ledger with no such key behaves the same.
    const legacy = root();
    renderCash(legacy, ledger(), CASH);
    expect(legacy.querySelector("[data-refund-due]")).toBeNull();
  });

  it("dashes an amount it could not read rather than owing $0.00", async () => {
    const { renderCash } = await import("../static/app/money.js");
    const node = root();
    renderCash(node, ledger({ refund_due_usd: null, refund_due_count: 3 }), CASH);
    const line = node.querySelector("[data-refund-due]");
    expect(line.textContent).toContain("—");
    expect(line.textContent).not.toContain("$0.00");
  });
});
