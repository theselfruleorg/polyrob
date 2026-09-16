// 043 WS-AF1 / §7 — the Agent destination, six tabs a person can read and trust.
//
// What it must get right is small and specific, and it is the same bar money.js
// and work-now.js are held to:
//
//   * each tab renders from its OWN reader, so one failing store never blanks
//     another;
//   * the four posture axes render ONCE, as four plain sentences — never the
//     same axis twice in two vocabularies (the defect doctor and /autonomy have);
//   * Advanced renders nothing until you ask (search-first over ~689 flags);
//   * an unreadable section is a dashed entry with its reason, never a silent
//     drop and never a confident empty list.
//
// It lives beside money.test.js because the vitest rig's root IS webview/dev/.
import { describe, it, expect } from "vitest";
import {
  activeTab,
  capabilityItems,
  capabilityRow,
  copyFrom,
  dashedEntry,
  filterCapabilities,
  fmtUsd,
  format,
  groupPreferences,
  healthItem,
  noteRow,
  postureEntries,
  relTime,
  renderCapabilities,
  renderConnected,
  renderDiagnostics,
  renderFace,
  renderFlags,
  renderHealth,
  renderIdentity,
  renderKb,
  renderNotes,
  renderPosture,
  renderRecall,
  renderSettings,
  showTab,
} from "../static/app/agent.js";

const COPY = {
  name: "rob",
  read_only: "0",
  unreachable: "That did not reach the console.",
  when_now: "just now",
  when_min: "{count} min ago",
  when_hour: "{count} h ago",
  when_day: "{count} d ago",
  // Overview
  ov_health_title: "Health",
  ov_health_aside: "what Rob checks",
  ov_health_unreadable: "I could not run the health check.",
  ov_health_unreadable_why: "Why",
  ov_health_ok_title: "Everything else checked out",
  ov_health_none: "Everything checked out",
  ov_health_ok_body: "The rest answered.",
  ov_report_link: "See the full report",
  ov_unverified: "I could not check these: {sources}",
  ov_identity_no_avatar: "Rob has no face yet.",
  ov_posture_title: "What Rob is allowed to do",
  ov_posture_aside: "four rules",
  ov_axis_local_title: "Treat this machine as yours alone",
  ov_axis_local_why: "A single owner's tools.",
  ov_axis_mode_title: "Act, and tell you after",
  ov_axis_mode_why: "Money still asks.",
  ov_axis_loop_title: "Run background work on its own",
  ov_axis_loop_why: "It starts its own goals.",
  ov_axis_compute_title: "Reach the computer it runs on",
  ov_axis_compute_why: "Run a shell.",
  ov_axis_compute_locked: "Set at start, cannot change while it runs.",
  ov_connected_title: "Connected to",
  ov_models_label: "Models",
  ov_memory_label: "Where memory is kept",
  ov_change: "Change",
  posture_local: "on",
  posture_mode: "supervised",
  posture_loop: "off",
  posture_compute: "0",
  posture_compute_locked: "1",
  // Identity
  id_unreadable: "I could not read the identity.",
  id_unreadable_why: "Why",
  id_face_title: "Rob's face",
  id_face_body: "Generated once and kept.",
  id_reroll: "Make a new face",
  id_persona_title: "How Rob should behave",
  id_persona_aside: "written by you",
  id_persona_empty: "You have not written a persona yet.",
  id_learned_title: "What Rob has learned about itself",
  id_learned_aside: "approved by you",
  id_learned_empty: "Nothing yet.",
  id_edit: "Edit",
  id_edit_hint: "This is a proposal. It waits in review.",
  // Capabilities
  cap_unreadable: "I could not read what Rob can do.",
  cap_unreadable_why: "Why",
  cap_list_title: "Everything Rob can do",
  cap_list_aside: "{count} shown",
  cap_part_unreadable: "One list could not be read.",
  cap_part_unreadable_why: "Why",
  cap_empty: "Nothing matches.",
  cap_col_what: "What it can do",
  cap_col_from: "Where it came from",
  cap_col_last: "Last used",
  cap_money_note: "Spends real money",
  cap_source_default: "Built in",
  cap_source_builtin: "Built in",
  cap_source_optional: "Available to turn on",
  cap_source_profile: "A helper you shaped",
  cap_source_mcp: "A connected service",
  cap_source_skill: "A procedure",
  cap_source_kept: "Rob wrote it, you kept it",
  cap_state_on: "on",
  cap_state_off: "off",
  cap_helpers_title: "Helpers",
  cap_helpers_aside: "read-only",
  cap_helpers_unreadable: "I could not read the helpers.",
  cap_helpers_unreadable_why: "Why",
  cap_helpers_empty: "No helpers yet.",
  // Memory
  mem_forget: "Forget",
  mem_notes_title: "What Rob remembers about you",
  mem_notes_aside: "your notes",
  mem_notes_unavailable: "My memory is not available right now.",
  mem_notes_unavailable_why: "Why",
  mem_notes_error: "I could not read your notes.",
  mem_notes_error_why: "Why",
  mem_notes_empty: "No notes yet.",
  mem_add_open: "Tell Rob something to remember",
  mem_recall_title: "What Rob works out on its own",
  mem_recall_aside: "separate",
  mem_recall_error: "I could not read what Rob worked out.",
  mem_recall_error_why: "Why",
  mem_recall_empty: "Nothing worked out yet.",
  mem_kb_title: "What Rob has read",
  mem_kb_aside: "{count} sources",
  mem_kb_unreadable: "I could not read the sources.",
  mem_kb_unreadable_why: "Why",
  mem_kb_empty: "Nothing read yet.",
  mem_kb_passages: "{count} passages",
  // Settings
  set_limits_title: "Money limits",
  set_limits_aside: "Rob cannot go past these",
  set_limits_unreadable: "I could not read the limits.",
  set_limits_unreadable_why: "Why",
  set_limit_no_cap: "No daily cap is set.",
  set_limit_unknown: "The daily cap could not be read.",
  set_limit_daily: "Most Rob can spend in a day is {cap}.",
  set_limit_used: "{used} of {cap} used today.",
  set_prefs_unreadable: "I could not read the settings.",
  set_prefs_unreadable_why: "Why",
  set_prefs_empty: "No settings.",
  set_prefs_other: "Other",
  set_col_what: "Setting",
  set_col_value: "Now",
  set_more: "There are more settings.",
  set_more_link: "Advanced",
  // Advanced
  adv_idle_title: "Search, or pick a group.",
  adv_idle_body: "It waits for you to say what you are looking for.",
  adv_unreadable: "I could not read the settings.",
  adv_unreadable_why: "Why",
  adv_no_match: "Nothing matches.",
  adv_col_name: "Setting",
  adv_col_what: "What it does",
  adv_col_default: "Default",
  adv_guarded: "the console cannot change this",
  adv_secret: "a secret",
  adv_diag_title: "Diagnostics",
  adv_diag_aside: "for when something is wrong",
  adv_diag_show: "Show the raw report",
  adv_diag_hide: "Hide the raw report",
  adv_diag_empty: "The report is empty.",
  adv_diag_unreadable: "I could not run the report.",
  adv_diag_unreadable_why: "Why",
};

function root() {
  return document.createElement("div");
}

describe("the copy crosses on data attributes", () => {
  it("copyFrom is a plain object of the dataset", () => {
    const node = document.createElement("div");
    node.dataset.name = "rob";
    expect(copyFrom(node).name).toBe("rob");
  });
});

describe("the four posture axes render once, as four plain sentences", () => {
  it("is exactly four entries, in order, each with a title and its consequence", () => {
    const entries = postureEntries(COPY);
    expect(entries).toHaveLength(4);
    expect(entries.map((e) => e.dataset.axis)).toEqual(
      ["local", "mode", "loop", "compute"]);
    entries.forEach((e) => {
      expect(e.querySelector(".entry-title").textContent).toBeTruthy();
      expect(e.querySelector(".entry-body").textContent).toBeTruthy();
    });
  });

  it("shows each axis's effective state once, from the server-computed value", () => {
    const r = root();
    renderPosture(r, COPY);
    const axes = r.querySelectorAll("[data-axis]");
    expect(axes).toHaveLength(4);
    // The state rides in the pill — one axis, one sentence, one state.
    const local = r.querySelector('[data-axis="local"] .pill');
    expect(local.textContent).toContain("on");
    const mode = r.querySelector('[data-axis="mode"] .pill');
    expect(mode.textContent).toContain("supervised");
  });

  it("says the compute axis is frozen rather than offering a dead control", () => {
    const r = root();
    renderPosture(r, COPY);
    const compute = r.querySelector('[data-axis="compute"]');
    expect(compute.textContent).toContain("cannot change while it runs");
  });
});

describe("Overview health leads and is honest", () => {
  it("draws a ranked item with its severity class and its remedy", () => {
    const entry = healthItem(
      { key: "provider", severity: "warn", text: "A key expired", remedy: "Reconnect it" },
      COPY);
    expect(entry.className).toContain("is-needs-you");
    expect(entry.querySelector(".entry-title").textContent).toBe("A key expired");
    expect(entry.querySelector(".entry-meta").textContent).toBe("Reconnect it");
  });

  it("a failed read is a dashed entry with its reason, not an empty green card", () => {
    const r = root();
    const answer = renderHealth(r, { error: "500" }, COPY);
    expect(answer).toBe("unreadable");
    expect(r.querySelector(".entry.is-unknown")).toBeTruthy();
    expect(r.querySelector("details .entry-meta").textContent).toBe("500");
  });

  it("names what it could not verify rather than dropping it", () => {
    const r = root();
    renderHealth(r, { health: { overall: "ok", items: [], unverified: ["wallet"] } }, COPY);
    expect(r.textContent).toContain("wallet");
  });

  it("no items reads as everything checked out, with the link to the report", () => {
    const r = root();
    renderHealth(r, { health: { overall: "ok", items: [] } }, COPY);
    expect(r.textContent).toContain("Everything checked out");
    expect(r.querySelector('a[href="#advanced"]')).toBeTruthy();
  });
});

describe("Overview face and connected read from the doctor + pfp", () => {
  it("shows a face image when the avatar exists, a plain line when it does not", () => {
    const yes = root();
    renderFace(yes, { name: "rob", description: "a trader" }, COPY);
    expect(yes.querySelector("img")).toBeTruthy();
    const no = root();
    renderFace(no, { error: "404" }, COPY);
    expect(no.querySelector("img")).toBeNull();
    expect(no.textContent).toContain("Rob has no face yet.");
  });

  it("connected shows the models and where memory is kept, or a dash", () => {
    const r = root();
    renderConnected(r, { provider: "gemini", model: "flash", memory_backend: "sqlite" }, COPY);
    expect(r.textContent).toContain("gemini");
    expect(r.textContent).toContain("sqlite");
    const bad = root();
    expect(renderConnected(bad, { error: "500" }, COPY)).toBe("unreadable");
  });
});

describe("Identity — persona read-only, learned reviewable", () => {
  it("draws the persona and the learned doc; the edit is on the learned one", () => {
    const r = root();
    renderIdentity(r, { soul: "Be direct.", self: "Say which chain failed." }, COPY, {});
    expect(r.textContent).toContain("Be direct.");
    expect(r.textContent).toContain("Say which chain failed.");
    expect(r.querySelector("button[data-edit-self]")).toBeTruthy();
    // The edit hint states it lands in review, never live.
    expect(r.textContent).toContain("waits in review");
  });

  it("offers no write control on a read-only console", () => {
    const r = root();
    renderIdentity(r, { soul: "x", self: "y" }, COPY, { readOnly: true });
    expect(r.querySelector("button[data-edit-self]")).toBeNull();
    expect(r.querySelector("button[data-reroll]")).toBeNull();
  });

  it("an unreadable identity is a dashed entry with its reason", () => {
    const r = root();
    expect(renderIdentity(r, { error: "500" }, COPY, {})).toBe("unreadable");
    expect(r.querySelector(".entry.is-unknown")).toBeTruthy();
  });
});

describe("Capabilities — one list from three stores", () => {
  const DATA = {
    tools: { items: [
      { id: "filesystem", kind: "tool", what: "Read and write files", on: true,
        source: "default", capabilities: ["high_impact"], last_used: 1000 },
      { id: "defi_trade", kind: "tool", what: "Trade on-chain", on: true,
        source: "builtin", capabilities: ["money", "delegate_blocked"], last_used: null },
    ], error: null },
    skills: { items: [
      { id: "funding", kind: "skill", what: "Check funding rates", on: true,
        source: "user", created_by: "agent", last_used: 2000 },
    ], error: null },
    mcp: { items: [
      { id: "anysite", kind: "mcp", what: "Read any website", on: false, source: "mcp" },
    ], error: null },
    helpers: { items: [{ id: "researcher", kind: "helper", what: "A researcher" }], error: null },
  };

  it("merges tools, skills and mcp into one list", () => {
    expect(capabilityItems(DATA).map((i) => i.id))
      .toEqual(["filesystem", "defi_trade", "funding", "anysite"]);
  });

  it("filters by the real capability dimensions", () => {
    const items = capabilityItems(DATA);
    expect(filterCapabilities(items, "money").map((i) => i.id)).toEqual(["defi_trade"]);
    expect(filterCapabilities(items, "off").map((i) => i.id)).toEqual(["anysite"]);
    expect(filterCapabilities(items, "on").length).toBe(3);
  });

  it("a money row is marked as spending real money", () => {
    const tr = capabilityRow(
      { id: "defi_trade", what: "Trade", on: true, source: "builtin",
        capabilities: ["money"] }, COPY, 1000);
    expect(tr.dataset.money).toBe("1");
    expect(tr.textContent).toContain("Spends real money");
  });

  it("last used is a dash when it is not tracked, never a faked time", () => {
    const tr = capabilityRow(
      { id: "x", on: true, source: "mcp", capabilities: [], last_used: null }, COPY, 1000);
    expect(tr.querySelector("td:nth-child(3)").textContent).toBe("—");
  });

  it("renders the list plus the read-only helpers", () => {
    const r = root();
    expect(renderCapabilities(r, DATA, COPY, {})).toBe("capabilities");
    expect(r.textContent).toContain("Everything Rob can do");
    expect(r.textContent).toContain("Helpers");
    expect(r.textContent).toContain("researcher");
  });

  it("an unreadable capabilities read is a dashed entry", () => {
    const r = root();
    expect(renderCapabilities(r, { error: "500" }, COPY, {})).toBe("unreadable");
    expect(r.querySelector(".entry.is-unknown")).toBeTruthy();
  });

  it("one failing section is named, and never blanks the others", () => {
    const r = root();
    renderCapabilities(r, { ...DATA, skills: { items: [], error: "disk" } }, COPY, {});
    expect(r.textContent).toContain("One list could not be read.");
    expect(r.textContent).toContain("Read and write files"); // the others still render
  });
});

describe("Memory — notes, recall and the knowledge base, each its own state", () => {
  it("draws notes with a Forget and an Add on a writable console", () => {
    const r = root();
    renderNotes(r, { notes: [{ id: 1, content: "You want answers first", updated_ts: 1000 }] },
      COPY, {});
    expect(r.textContent).toContain("You want answers first");
    expect(r.querySelector("button[data-forget]")).toBeTruthy();
    expect(r.querySelector("button[data-add-note]")).toBeTruthy();
  });

  it("hides Forget and Add on a read-only console", () => {
    const r = root();
    renderNotes(r, { notes: [{ id: 1, content: "x", updated_ts: 1 }] }, COPY, { readOnly: true });
    expect(r.querySelector("button[data-forget]")).toBeNull();
    expect(r.querySelector("button[data-add-note]")).toBeNull();
  });

  it("an unavailable provider is named, never an empty list read as nothing", () => {
    const r = root();
    expect(renderNotes(r, { unavailable: true }, COPY, {})).toBe("unavailable");
    expect(r.querySelector(".entry.is-unknown")).toBeTruthy();
  });

  it("recall names its own error, distinct from an empty result", () => {
    const bad = root();
    expect(renderRecall(bad, { recall_error: "boom" }, COPY)).toBe("error");
    const empty = root();
    expect(renderRecall(empty, { recall: [] }, COPY)).toBe("empty");
  });

  it("the knowledge base shows a passage count and dashes an unreadable read", () => {
    const r = root();
    renderKb(r, { items: [{ title: "The guide", passages: 312 }] }, COPY);
    expect(r.textContent).toContain("312 passages");
    const bad = root();
    expect(renderKb(bad, { error: "500" }, COPY)).toBe("unreadable");
  });

  it("noteRow forget carries the note id for the delete", () => {
    const tr = noteRow({ id: 7, content: "x", updated_ts: 1 }, COPY, 1000, false);
    expect(tr.dataset.noteId).toBe("7");
    expect(tr.querySelector("button[data-forget]").dataset.forget).toBe("7");
  });
});

describe("Settings — prefs grouped by intent, and the money limits", () => {
  const PREFS = { preferences: [
    { key: "notify.max_per_day", description: "Messages a day", value: 30, source: "user", applies: "notify" },
    { key: "notify.quiet", description: "Quiet hours", value: "", source: "default", applies: "notify" },
    { key: "model.name", description: "Rob answers with", value: "flash", source: "user", applies: "model" },
  ] };
  const LEDGER = { caps: { daily_cap_usd: 100, daily_used_usd: 12 } };

  it("groups by the intent field", () => {
    const groups = groupPreferences(PREFS.preferences);
    expect([...groups.keys()]).toEqual(["notify", "model"]);
    expect(groups.get("notify")).toHaveLength(2);
  });

  it("draws the money limits from the ledger caps and the grouped prefs", () => {
    const r = root();
    expect(renderSettings(r, PREFS, LEDGER, COPY)).toBe("settings");
    expect(r.textContent).toContain("Most Rob can spend in a day is $100.00");
    expect(r.textContent).toContain("Messages a day");
    expect(r.textContent).toContain("Rob answers with");
  });

  it("an unreadable ledger dashes the limits but still shows the prefs", () => {
    const r = root();
    renderSettings(r, PREFS, { error: "500" }, COPY);
    expect(r.textContent).toContain("I could not read the limits.");
    expect(r.textContent).toContain("Messages a day");
  });

  it("an unreadable prefs read is dashed", () => {
    const r = root();
    expect(renderSettings(r, { error: "500" }, LEDGER, COPY)).toBe("unreadable");
  });

  it("a spend figure is never a confident zero", () => {
    expect(fmtUsd(0.0009)).toBe("$0.0009");
    expect(fmtUsd(100)).toBe("$100.00");
    expect(fmtUsd(null)).toBe("—");
  });
});

describe("Advanced — search-first, and the diagnostic report", () => {
  it("renders nothing but the idle state until a query or a group", () => {
    const r = root();
    expect(renderFlags(r, { queried: false }, COPY)).toBe("idle");
    expect(r.querySelector("table")).toBeNull();
    expect(r.textContent).toContain("Search, or pick a group.");
  });

  it("a queried result is a table, and a guarded flag is marked", () => {
    const r = root();
    const data = { queried: true, flags: [
      { name: "some_flag", description: "does a thing", default: "off", guarded: true },
    ] };
    expect(renderFlags(r, data, COPY)).toBe("rows");
    expect(r.querySelector("table")).toBeTruthy();
    expect(r.querySelector('tr[data-guarded="1"]')).toBeTruthy();
    expect(r.textContent).toContain("the console cannot change this");
  });

  it("no match is a plain notice, an error is dashed with its reason", () => {
    const empty = root();
    expect(renderFlags(empty, { queried: true, flags: [] }, COPY)).toBe("empty");
    const bad = root();
    expect(renderFlags(bad, { queried: true, error: "500" }, COPY)).toBe("unreadable");
    expect(bad.querySelector("details .entry-meta").textContent).toBe("500");
  });

  it("the diagnostics report is a transcript behind one disclosure", () => {
    const r = root();
    expect(renderDiagnostics(r, { checks: ["ok: memory"], status_lines: ["Health: ok"] }, COPY))
      .toBe("diagnostics");
    const pre = r.querySelector("pre");
    expect(pre.hidden).toBe(true);
    expect(pre.textContent).toContain("ok: memory");
    r.querySelector("button").click();
    expect(pre.hidden).toBe(false);
  });

  it("an unreadable report is dashed, not an empty transcript", () => {
    const r = root();
    expect(renderDiagnostics(r, { error: "500" }, COPY)).toBe("unreadable");
  });
});

describe("shared helpers", () => {
  it("relTime draws from the event's own timestamp, empty when missing", () => {
    const now = 1_000_000;
    expect(relTime(now / 1000 - 30, COPY, now)).toBe("just now");
    expect(relTime(now / 1000 - 120, COPY, now)).toBe("2 min ago");
    expect(relTime(undefined, COPY, now)).toBe("");
  });

  it("format fills named fields and leaves an unknown one alone", () => {
    expect(format("{count} shown", { count: 3 })).toBe("3 shown");
    expect(format("{nope}", {})).toBe("{nope}");
  });

  it("dashedEntry keeps the machine reason behind a disclosure", () => {
    const entry = dashedEntry("KeyError: x", COPY, "cap_unreadable", "cap_unreadable_why");
    expect(entry.className).toContain("is-unknown");
    expect(entry.querySelector("summary").textContent).toBe("Why");
    expect(entry.querySelector(".entry-meta").textContent).toBe("KeyError: x");
  });

  it("activeTab defaults to the first tab for an unknown hash", () => {
    const tabs = ["overview", "identity", "advanced"];
    expect(activeTab("#identity", tabs)).toBe("identity");
    expect(activeTab("#nope", tabs)).toBe("overview");
    expect(activeTab("", tabs)).toBe("overview");
  });

  it("showTab reveals one pane and marks its link", () => {
    const a1 = document.createElement("a"); a1.dataset.tab = "overview";
    const a2 = document.createElement("a"); a2.dataset.tab = "memory";
    const p1 = document.createElement("div"); p1.dataset.pane = "overview";
    const p2 = document.createElement("div"); p2.dataset.pane = "memory";
    showTab("memory", [a1, a2], [p1, p2]);
    expect(p1.hidden).toBe(true);
    expect(p2.hidden).toBe(false);
    expect(a2.getAttribute("aria-current")).toBe("page");
    expect(a1.getAttribute("aria-current")).toBeNull();
  });
});
