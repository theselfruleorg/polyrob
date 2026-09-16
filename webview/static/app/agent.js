/**
 * agent.js — the Agent destination: six tabs (043 WS-AF1 / phase 4).
 *
 * Who Rob is, what it can do, what it remembers, and the rules it runs under —
 * one screen, six panes:
 *
 *   - Overview — the ranked health block leads (the same build_status_snapshot
 *     every seat renders), then Rob's face and name, then the four posture axes
 *     as four plain sentences (never printed twice in two vocabularies, which is
 *     what doctor and /autonomy do today), then what it is connected to.
 *   - Identity — the name and face, the persona you wrote (read-only, frozen),
 *     and what Rob has learned about itself as a reviewable change. An edit is
 *     ALWAYS a proposal: it lands in a review queue, never live, and this says so.
 *   - Capabilities — skills, tools and connected services as ONE list, because
 *     to a person they are one idea: what it does, whether it is on, where it
 *     came from and when it was last used. Plus the read-only helpers.
 *   - Memory — what Rob remembers about you (its notes, add and forget here),
 *     what it works out on its own (recall), and what it has read.
 *   - Settings — the preferences people actually change, grouped by intent, and
 *     the money limits it cannot go past. Everything else is in Advanced.
 *   - Advanced — every setting, search-first (nothing renders until you ask),
 *     and the full diagnostic report behind one disclosure.
 *
 * Four rules it keeps, the same ones money.js / work-now.js keep:
 *
 * 1. **Every word comes from the copy layer.** No 043 template carries an inline
 *    script (tests/unit/webview/test_no_new_inline_script.py), so the words ride
 *    on `#agent-copy`'s data attributes and this reads them off `dataset`.
 * 2. **An unreadable section is a dashed entry with its reason, never a silent
 *    drop and never a confident empty list.** A reader answers 200 with an
 *    `error` / `unavailable` field on a failed read; that becomes the dash.
 * 3. **It draws no verb it cannot act on.** Under `WEBVIEW_READ_ONLY` the shell
 *    states the posture once and this renders no Edit / Remember / Forget — an
 *    absent control, not a disabled-looking one that 403s.
 * 4. **It decides nothing itself.** A write is a POST/DELETE to an existing
 *    route (`http.js`), and the row says back exactly what the server answered.
 */
import { postJson } from "./http.js";
import { settingControl } from "./settings-control.js";

const DASH = "—"; // — : an unknown value, never a fabricated zero or a blank.

/** The copy the server handed over, as a plain object. */
export function copyFrom(node) {
  return node ? { ...node.dataset } : {};
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** Fill every `{name}` from a vars object. Copy owns the words; this owns only
 *  the values. */
export function format(template, vars) {
  return String(template || "").replace(/\{(\w+)\}/g, (m, key) =>
    vars && Object.prototype.hasOwnProperty.call(vars, key)
      ? String(vars[key]) : m);
}

/** A timestamp in plain words, from an epoch-seconds value. The words are copy;
 *  only the number is computed here. Empty for a missing timestamp, never a
 *  guess. */
export function relTime(ts, copy, nowMs) {
  const now = Number.isFinite(nowMs) ? nowMs : Date.now();
  // ⚠️ A missing timestamp is empty, never "just now": `Number(null)` is 0, a
  // finite value, so a null/absent last-used must be caught BEFORE the coercion
  // or an untracked capability would render as used a moment ago.
  if (ts === null || ts === undefined || ts === "") return "";
  const t = Number(ts);
  if (!Number.isFinite(t)) return "";
  const seconds = Math.max(0, now / 1000 - t);
  if (seconds < 60) return (copy && copy.when_now) || "";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return format(copy && copy.when_min, { count: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return format(copy && copy.when_hour, { count: hours });
  return format(copy && copy.when_day, { count: Math.floor(hours / 24) });
}

/** A plain informational entry (empty state, loading, feature-off). */
export function noticeEntry(text) {
  const entry = el("div", "entry");
  entry.appendChild(el("p", "entry-body", text || ""));
  return entry;
}

/** The dashed entry for a store that could not be read — the reason behind a
 *  disclosure, never a confident empty list and never a fabricated value. */
export function dashedEntry(reason, copy, titleKey, whyKey) {
  const entry = el("div", "entry is-unknown");
  entry.appendChild(el("h3", "entry-title", (copy && copy[titleKey]) || ""));
  if (reason) {
    const details = el("details");
    details.appendChild(el("summary", null, (copy && copy[whyKey]) || ""));
    details.appendChild(el("p", "entry-meta", String(reason)));
    entry.appendChild(details);
  }
  return entry;
}

/** A section shell: a head with a title and an optional aside. */
function section(titleText, asideText) {
  const sec = el("div", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", titleText || ""));
  if (asideText) head.appendChild(el("span", "section-aside", asideText));
  sec.appendChild(head);
  return sec;
}

// --- Overview --------------------------------------------------------------- #
// The ranked health block leads (this IS A8's health card), then the face and
// name, then the four posture axes, then what Rob is connected to.

//: The health severity → entry class. Health items are only crit or warn today
//: (core.status_snapshot); an unknown severity is drawn as unknown, never green.
const _SEVERITY_CLASS = { crit: "entry is-stopped", warn: "entry is-needs-you" };

/** One ranked health item: what is wrong, and the remedy behind it. */
export function healthItem(item, copy) {
  const cls = _SEVERITY_CLASS[String(item && item.severity)] || "entry is-unknown";
  const entry = el("div", cls);
  entry.dataset.severity = String((item && item.severity) || "");
  entry.appendChild(el("h3", "entry-title", (item && item.text) || ""));
  if (item && item.remedy) entry.appendChild(el("p", "entry-meta", item.remedy));
  return entry;
}

/**
 * Draw the Overview health card from the doctor reader's `health` block. A read
 * that failed is a dash with its reason; an `unavailable` overall is named; no
 * items is a plain "everything checked out". Returns which it drew, for tests.
 */
export function renderHealth(root, doctor, copy) {
  root.replaceChildren();
  const sec = section(copy && copy.ov_health_title, copy && copy.ov_health_aside);
  root.appendChild(sec);
  if (!doctor || doctor.error) {
    sec.appendChild(dashedEntry((doctor && doctor.error) || "", copy,
      "ov_health_unreadable", "ov_health_unreadable_why"));
    return "unreadable";
  }
  const health = doctor.health || {};
  const items = health.items || [];
  const ledger = el("div", "ledger");
  if (health.overall === "unavailable" && !items.length) {
    ledger.appendChild(dashedEntry((health.lines || []).join(" "), copy,
      "ov_health_unreadable", "ov_health_unreadable_why"));
    sec.appendChild(ledger);
    return "unavailable";
  }
  items.forEach((it) => ledger.appendChild(healthItem(it, copy)));
  // The "and everything else answered" entry, with the link to the full report.
  const ok = el("div", "entry is-running");
  ok.appendChild(el("h3", "entry-title",
    items.length ? (copy && copy.ov_health_ok_title) : (copy && copy.ov_health_none)));
  ok.appendChild(el("p", "entry-body", (copy && copy.ov_health_ok_body) || ""));
  const actions = el("div", "entry-actions");
  const link = el("a", "btn btn-quiet", (copy && copy.ov_report_link) || "");
  link.href = "#advanced";
  actions.appendChild(link);
  ok.appendChild(actions);
  ledger.appendChild(ok);
  sec.appendChild(ledger);
  // What could not be checked is named, never dropped.
  const unver = health.unverified || [];
  if (unver.length) {
    sec.appendChild(el("p", "entry-meta",
      format(copy && copy.ov_unverified, { sources: unver.join(", ") })));
  }
  return "health";
}

/** Draw Rob's face and name. `pfp` is /pfp.json (or `{error}` when absent — a
 *  fresh instance has no avatar, which is a state, not a failure). */
export function renderFace(root, pfp, copy) {
  root.replaceChildren();
  const sec = el("div", "section");
  const row = el("div", "section-head");
  const face = el("div", "face");
  face.setAttribute("aria-hidden", "true");
  if (pfp && !pfp.error) {
    const img = document.createElement("img");
    img.src = "/pfp.png";
    img.alt = "";
    img.width = 64; img.height = 64;
    face.appendChild(img);
  }
  row.appendChild(face);
  const body = el("div");
  body.appendChild(el("h2", "section-title", (copy && copy.name) || ""));
  const line = (pfp && !pfp.error && (pfp.description || pfp.tagline))
    || (copy && copy.ov_identity_no_avatar) || "";
  body.appendChild(el("p", "entry-body", line));
  row.appendChild(body);
  sec.appendChild(row);
  root.appendChild(sec);
  return pfp && !pfp.error ? "face" : "no_face";
}

//: The four posture axes, in order, and the copy that names each. The effective
//: value rides on the copy node as data (server-computed from build_posture_card),
//: so a person reads ONE sentence per axis and the state it is in — never the
//: same axis twice in two vocabularies, which is the defect this replaces.
const _POSTURE_AXES = [
  { key: "local", value: "posture_local" },
  { key: "mode", value: "posture_mode" },
  { key: "loop", value: "posture_loop" },
  { key: "compute", value: "posture_compute" },
];

/** The four posture axes as four entries. Each is a plain title, its consequence,
 *  and the state it is in (a value, shown once). Compute is frozen at import, so
 *  its entry says so rather than offering a control that would not work. */
export function postureEntries(copy) {
  return _POSTURE_AXES.map((axis) => {
    const entry = el("div", "entry");
    entry.dataset.axis = axis.key;
    entry.appendChild(el("h3", "entry-title", (copy && copy[`ov_axis_${axis.key}_title`]) || ""));
    entry.appendChild(el("p", "entry-body", (copy && copy[`ov_axis_${axis.key}_why`]) || ""));
    const state = (copy && copy[axis.value]) || DASH;
    const meta = el("p", "entry-meta");
    const pill = el("span", "pill");
    pill.appendChild(el("span", "dot"));
    pill.appendChild(document.createTextNode(state));
    meta.appendChild(pill);
    if (axis.key === "compute" && (copy && copy.posture_compute_locked) === "1") {
      meta.appendChild(document.createTextNode(" "));
      meta.appendChild(el("span", "unknown-why", (copy && copy.ov_axis_compute_locked) || ""));
    }
    entry.appendChild(meta);
    return entry;
  });
}

/** Draw the four posture axes into *root*. */
export function renderPosture(root, copy) {
  root.replaceChildren();
  const sec = section(copy && copy.ov_posture_title, copy && copy.ov_posture_aside);
  const ledger = el("div", "ledger");
  postureEntries(copy).forEach((e) => ledger.appendChild(e));
  sec.appendChild(ledger);
  root.appendChild(sec);
  return "posture";
}

/** Draw "connected to" from the doctor reader: the models that answer and where
 *  memory is kept. Values are the machine's own; a link goes to Settings. */
export function renderConnected(root, doctor, copy) {
  root.replaceChildren();
  const sec = section(copy && copy.ov_connected_title, "");
  if (!doctor || doctor.error) {
    sec.appendChild(dashedEntry((doctor && doctor.error) || "", copy,
      "ov_health_unreadable", "ov_health_unreadable_why"));
    root.appendChild(sec);
    return "unreadable";
  }
  const table = el("table", "table");
  const tbody = el("tbody");
  const rowOf = (label, value) => {
    const tr = el("tr");
    const what = el("td", "what");
    what.dataset.label = "";
    what.textContent = label || "";
    tr.appendChild(what);
    const val = el("td");
    val.dataset.label = "";
    if (value) val.appendChild(el("span", "val", value));
    else val.appendChild(el("span", "unknown", DASH));
    tr.appendChild(val);
    tbody.appendChild(tr);
  };
  const model = [doctor.provider, doctor.model].filter(Boolean).join(" ");
  rowOf(copy && copy.ov_models_label, model);
  rowOf(copy && copy.ov_memory_label, doctor.memory_backend);
  table.appendChild(tbody);
  sec.appendChild(table);
  const actions = el("div", "entry-actions");
  const link = el("a", "btn btn-quiet", (copy && copy.ov_change) || "");
  link.href = "#settings";
  actions.appendChild(link);
  sec.appendChild(actions);
  root.appendChild(sec);
  return "connected";
}

// --- Identity --------------------------------------------------------------- #
// The persona you wrote (read-only, frozen) and what Rob has learned about
// itself (a reviewable change, edited into a review queue and never live).

/** Draw the identity tab from /api/webgate/identity. `readOnly` hides the write
 *  controls. Returns the answer it drew, for tests. */
export function renderIdentity(root, data, copy, opts = {}) {
  const readOnly = Boolean(opts.readOnly);
  root.replaceChildren();
  if (!data || data.error) {
    root.appendChild(dashedEntry((data && data.error) || "", copy,
      "id_unreadable", "id_unreadable_why"));
    return "unreadable";
  }

  // The face — reroll is kept, but it is a one-time ceremony, so it is offered
  // only on a writable console and it says back exactly what the server answered.
  const faceSec = section(copy && copy.id_face_title, copy && copy.id_face_body);
  if (!readOnly) {
    const actions = el("div", "entry-actions");
    const btn = el("button", "btn btn-quiet", (copy && copy.id_reroll) || "");
    btn.type = "button";
    btn.dataset.reroll = "1";
    actions.appendChild(btn);
    faceSec.appendChild(actions);
  }
  root.appendChild(faceSec);

  // The persona you wrote — READ-ONLY. It is the one document Rob never edits.
  const soulSec = section(copy && copy.id_persona_title, copy && copy.id_persona_aside);
  const soul = el("div", "entry");
  soul.appendChild(el("p", "entry-body",
    (data.soul && String(data.soul)) || (copy && copy.id_persona_empty) || ""));
  soulSec.appendChild(soul);
  root.appendChild(soulSec);

  // What Rob has learned about itself — a reviewable change. Editing it proposes
  // a new version into the review queue; it is never live until you keep it.
  const selfSec = section(copy && copy.id_learned_title, copy && copy.id_learned_aside);
  const self = el("div", "entry");
  self.appendChild(el("p", "entry-body",
    (data.self && String(data.self)) || (copy && copy.id_learned_empty) || ""));
  if (!readOnly) {
    const actions = el("div", "entry-actions");
    const edit = el("button", "btn", (copy && copy.id_edit) || "");
    edit.type = "button";
    edit.dataset.editSelf = "1";
    actions.appendChild(edit);
    self.appendChild(actions);
    self.appendChild(el("p", "entry-meta", (copy && copy.id_edit_hint) || ""));
  }
  selfSec.appendChild(self);
  root.appendChild(selfSec);
  return "identity";
}

/** The inline editor for the SELF doc: a textarea pre-filled with the current
 *  text and a Save that posts a PROPOSAL. Replaces the row it is opened from. */
export function selfEditor(current, copy) {
  const wrap = el("div", "entry");
  wrap.dataset.selfEditor = "1";
  const ta = document.createElement("textarea");
  ta.setAttribute("aria-label", (copy && copy.id_learned_title) || "");
  ta.value = current || "";
  ta.style.width = "100%";
  ta.style.minHeight = "150px";
  wrap.appendChild(ta);
  wrap.appendChild(el("p", "entry-meta", (copy && copy.id_edit_hint) || ""));
  const actions = el("div", "entry-actions");
  const save = el("button", "btn btn-primary", (copy && copy.id_edit_save) || "");
  save.type = "button";
  save.dataset.saveSelf = "1";
  const cancel = el("button", "btn btn-quiet", (copy && copy.id_edit_cancel) || "");
  cancel.type = "button";
  cancel.dataset.cancelSelf = "1";
  actions.appendChild(save);
  actions.appendChild(cancel);
  wrap.appendChild(actions);
  return wrap;
}

// --- Capabilities ----------------------------------------------------------- #
// Skills + tools + connected services as ONE list, plus the read-only helpers.

//: source id → the plain sentence for where a capability came from.
const _SOURCE_KEY = {
  default: "cap_source_default", builtin: "cap_source_builtin",
  optional: "cap_source_optional", profile: "cap_source_profile",
  mcp: "cap_source_mcp", skill: "cap_source_skill",
};

/** One capability row: what it does, where it came from, when last used, and
 *  whether it is on. Money capabilities carry a "spends money" note. */
export function capabilityRow(item, copy, nowMs) {
  const tr = el("tr");
  tr.dataset.kind = String((item && item.kind) || "");
  tr.dataset.on = item && item.on ? "1" : "0";
  const caps = (item && item.capabilities) || [];
  tr.dataset.money = caps.includes("money") ? "1" : "0";
  tr.dataset.helperBlocked = caps.includes("delegate_blocked") ? "1" : "0";

  const what = el("td", "what");
  what.dataset.label = (copy && copy.cap_col_what) || "";
  what.appendChild(document.createTextNode((item && (item.what || item.id)) || ""));
  if (caps.includes("money")) what.appendChild(el("span", "why", (copy && copy.cap_money_note) || ""));
  tr.appendChild(what);

  const from = el("td");
  from.dataset.label = (copy && copy.cap_col_from) || "";
  const created = item && item.created_by;
  const src = (created === "user" || created === "agent")
    ? (copy && copy.cap_source_kept)
    : (copy && copy[_SOURCE_KEY[String(item && item.source)]]);
  from.textContent = src || (item && item.source) || DASH;
  tr.appendChild(from);

  const last = el("td");
  last.dataset.label = (copy && copy.cap_col_last) || "";
  const when = relTime(item && item.last_used, copy, nowMs);
  last.textContent = when || DASH;
  tr.appendChild(last);

  const state = el("td");
  const pill = el("span", item && item.on ? "pill is-running" : "pill");
  pill.appendChild(el("span", "dot"));
  pill.appendChild(document.createTextNode(
    item && item.on ? (copy && copy.cap_state_on) || "" : (copy && copy.cap_state_off) || ""));
  state.appendChild(pill);
  tr.appendChild(state);
  return tr;
}

/** The rows to show given the active chip: "" everything, "on"/"off"/"money"/
 *  "helpers". */
export function filterCapabilities(items, chip) {
  return (items || []).filter((it) => {
    const caps = it.capabilities || [];
    if (chip === "on") return Boolean(it.on);
    if (chip === "off") return !it.on;
    if (chip === "money") return caps.includes("money");
    if (chip === "helpers") return caps.includes("delegate_blocked");
    return true;
  });
}

/** Merge the capability sections into one list, each row carrying its kind. */
export function capabilityItems(data) {
  const out = [];
  ["tools", "skills", "mcp"].forEach((name) => {
    const items = (data && data[name] && data[name].items) || [];
    items.forEach((it) => out.push(it));
  });
  return out;
}

/**
 * Draw Capabilities. `data` is /api/webgate/capabilities. One list from three
 * stores; a section that failed to read is a dashed entry naming itself, never a
 * silent drop. Returns the answer it drew, for tests.
 */
export function renderCapabilities(root, data, copy, opts = {}) {
  const nowMs = Number.isFinite(opts.nowMs) ? opts.nowMs : Date.now();
  const chip = opts.chip || "";
  root.replaceChildren();
  if (!data || data.error) {
    root.appendChild(dashedEntry((data && data.error) || "", copy,
      "cap_unreadable", "cap_unreadable_why"));
    return "unreadable";
  }

  const items = filterCapabilities(capabilityItems(data), chip);
  const sec = section(copy && copy.cap_list_title,
    format(copy && copy.cap_list_aside, { count: items.length }));
  // Any section that failed reading is named before the table.
  ["tools", "skills", "mcp"].forEach((name) => {
    const err = data[name] && data[name].error;
    if (err) sec.appendChild(dashedEntry(err, copy, "cap_part_unreadable",
      "cap_part_unreadable_why"));
  });
  if (!items.length) {
    sec.appendChild(noticeEntry(copy && copy.cap_empty));
  } else {
    const table = el("table", "table");
    const thead = el("thead");
    const hrow = el("tr");
    [copy && copy.cap_col_what, copy && copy.cap_col_from,
     copy && copy.cap_col_last, ""].forEach((h) => hrow.appendChild(el("th", null, h || "")));
    thead.appendChild(hrow);
    table.appendChild(thead);
    const tbody = el("tbody");
    items.forEach((it) => tbody.appendChild(capabilityRow(it, copy, nowMs)));
    table.appendChild(tbody);
    sec.appendChild(table);
  }
  root.appendChild(sec);

  // Helpers — read-only. An unreadable store is dashed with its reason.
  const helpers = (data.helpers && data.helpers.items) || [];
  const hsec = section(copy && copy.cap_helpers_title, copy && copy.cap_helpers_aside);
  if (data.helpers && data.helpers.error) {
    hsec.appendChild(dashedEntry(data.helpers.error, copy, "cap_helpers_unreadable",
      "cap_helpers_unreadable_why"));
  } else if (!helpers.length) {
    hsec.appendChild(noticeEntry(copy && copy.cap_helpers_empty));
  } else {
    const ledger = el("div", "ledger");
    helpers.forEach((h) => {
      const entry = el("div", "entry");
      entry.appendChild(el("h3", "entry-title", (h && h.id) || ""));
      if (h && h.what) entry.appendChild(el("p", "entry-body", h.what));
      ledger.appendChild(entry);
    });
    hsec.appendChild(ledger);
  }
  root.appendChild(hsec);
  return "capabilities";
}

// --- Memory ----------------------------------------------------------------- #
// What Rob remembers about you (its notes; add and forget here), what it works
// out on its own (recall), and what it has read (the knowledge base).

/** One curated note row: what, when, and Forget (unless read-only). */
export function noteRow(note, copy, nowMs, readOnly) {
  const tr = el("tr");
  tr.dataset.noteId = String((note && note.id) || "");
  const what = el("td", "what");
  what.dataset.label = "";
  what.appendChild(document.createTextNode((note && (note.content || note.title)) || ""));
  if (note && note.title && note.content) what.appendChild(el("span", "why", note.title));
  tr.appendChild(what);
  const when = el("td");
  when.dataset.label = "";
  when.textContent = relTime(note && note.updated_ts, copy, nowMs) || DASH;
  tr.appendChild(when);
  const act = el("td");
  if (!readOnly) {
    const btn = el("button", "btn btn-quiet", (copy && copy.mem_forget) || "");
    btn.type = "button";
    btn.dataset.forget = String((note && note.id) || "");
    act.appendChild(btn);
  }
  tr.appendChild(act);
  return tr;
}

/** One recall snippet — what Rob worked out on its own. Recall is a string. */
function recallRow(text, copy) {
  const tr = el("tr");
  const what = el("td", "what");
  what.dataset.label = "";
  what.textContent = String(text || "");
  tr.appendChild(what);
  return tr;
}

/** One knowledge-base source — what Rob has read. */
function kbRow(item, copy) {
  const tr = el("tr");
  const what = el("td", "what");
  what.dataset.label = "";
  what.textContent = (item && (item.title || item.source || item.name || item.collection)) || DASH;
  tr.appendChild(what);
  const count = el("td");
  count.dataset.label = "";
  const passages = item && (item.passages != null ? item.passages
    : item.count != null ? item.count : item.chunks);
  count.textContent = Number.isFinite(Number(passages))
    ? format(copy && copy.mem_kb_passages, { count: Number(passages) }) : "";
  tr.appendChild(count);
  return tr;
}

/** Draw the notes section (what Rob remembers about you). Add opens the composer;
 *  Forget archives one. An unavailable provider is named, never an empty list. */
export function renderNotes(root, data, copy, opts = {}) {
  const nowMs = Number.isFinite(opts.nowMs) ? opts.nowMs : Date.now();
  const readOnly = Boolean(opts.readOnly);
  root.replaceChildren();
  const sec = section(copy && copy.mem_notes_title, copy && copy.mem_notes_aside);
  root.appendChild(sec);
  if (data && data.unavailable) {
    sec.appendChild(dashedEntry("", copy, "mem_notes_unavailable",
      "mem_notes_unavailable_why"));
    return "unavailable";
  }
  if (data && data.notes_error) {
    sec.appendChild(dashedEntry(data.notes_error, copy, "mem_notes_error",
      "mem_notes_error_why"));
    return "error";
  }
  const notes = (data && data.notes) || [];
  if (!notes.length) {
    sec.appendChild(noticeEntry(copy && copy.mem_notes_empty));
  } else {
    const table = el("table", "table");
    const tbody = el("tbody");
    notes.forEach((n) => tbody.appendChild(noteRow(n, copy, nowMs, readOnly)));
    table.appendChild(tbody);
    sec.appendChild(table);
  }
  if (!readOnly) {
    const actions = el("div", "entry-actions");
    const add = el("button", "btn", (copy && copy.mem_add_open) || "");
    add.type = "button";
    add.dataset.addNote = "1";
    actions.appendChild(add);
    sec.appendChild(actions);
  }
  return "notes";
}

/** The add-a-note composer: a textarea kept word for word, and a Save that
 *  posts it. */
export function noteComposer(copy) {
  const sec = section(copy && copy.mem_add_title, copy && copy.mem_add_aside);
  sec.dataset.noteComposer = "1";
  const ta = document.createElement("textarea");
  ta.setAttribute("aria-label", (copy && copy.mem_add_title) || "");
  ta.dataset.noteInput = "1";
  ta.style.width = "100%";
  ta.style.minHeight = "88px";
  sec.appendChild(ta);
  const actions = el("div", "entry-actions");
  const save = el("button", "btn btn-primary", (copy && copy.mem_add_save) || "");
  save.type = "button";
  save.dataset.saveNote = "1";
  const cancel = el("button", "btn btn-quiet", (copy && copy.mem_add_cancel) || "");
  cancel.type = "button";
  cancel.dataset.cancelNote = "1";
  actions.appendChild(save);
  actions.appendChild(cancel);
  sec.appendChild(actions);
  sec.appendChild(el("p", "entry-meta", (copy && copy.mem_add_hint) || ""));
  return sec;
}

/** Draw recall (what Rob works out on its own). */
export function renderRecall(root, data, copy) {
  root.replaceChildren();
  const sec = section(copy && copy.mem_recall_title, copy && copy.mem_recall_aside);
  root.appendChild(sec);
  if (data && data.unavailable) {
    sec.appendChild(dashedEntry("", copy, "mem_notes_unavailable",
      "mem_notes_unavailable_why"));
    return "unavailable";
  }
  if (data && data.recall_error) {
    sec.appendChild(dashedEntry(data.recall_error, copy, "mem_recall_error",
      "mem_recall_error_why"));
    return "error";
  }
  const recall = (data && data.recall) || [];
  if (!recall.length) {
    sec.appendChild(noticeEntry(copy && copy.mem_recall_empty));
    return "empty";
  }
  const table = el("table", "table");
  const tbody = el("tbody");
  recall.forEach((r) => tbody.appendChild(recallRow(r, copy)));
  table.appendChild(tbody);
  sec.appendChild(table);
  return "recall";
}

/** Draw the knowledge base (what Rob has read). `data` is /knowledge/kb. */
export function renderKb(root, data, copy) {
  root.replaceChildren();
  const items = (data && data.items) || [];
  const sec = section(copy && copy.mem_kb_title,
    format(copy && copy.mem_kb_aside, { count: !data || data.error ? DASH : items.length }));
  root.appendChild(sec);
  if (!data || data.error) {
    sec.appendChild(dashedEntry(data?.error || "", copy, "mem_kb_unreadable",
      "mem_kb_unreadable_why"));
    return "unreadable";
  }
  if (!items.length) {
    sec.appendChild(noticeEntry(copy && copy.mem_kb_empty));
    return "empty";
  }
  const table = el("table", "table");
  const tbody = el("tbody");
  items.forEach((it) => tbody.appendChild(kbRow(it, copy)));
  table.appendChild(tbody);
  sec.appendChild(table);
  return "kb";
}

// --- Settings --------------------------------------------------------------- #
// The preferences people actually change, grouped by intent, plus the money
// limits Rob cannot go past. Writes use the existing guarded preference service.

/** Group preference rows by their `applies` field (the intent). */
export function groupPreferences(items) {
  const groups = new Map();
  (items || []).forEach((p) => {
    const key = String(p.applies || "");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(p);
  });
  return groups;
}

/** Draw Settings. `prefs` is /api/webgate/preferences, `ledger` is
 *  /api/webgate/ledger (its caps). Returns the answer it drew. */
export function renderSettings(root, prefs, ledger, copy) {
  root.replaceChildren();

  // Money limits — gathered here from the ledger's caps, because a cap is
  // configuration, not a result.
  const lim = section(copy && copy.set_limits_title, copy && copy.set_limits_aside);
  if (!ledger || ledger.error) {
    lim.appendChild(dashedEntry((ledger && ledger.error) || "", copy,
      "set_limits_unreadable", "set_limits_unreadable_why"));
  } else {
    const caps = ledger.caps || {};
    const cap = caps.daily_cap_usd != null ? caps.daily_cap_usd : caps.wallet_daily_cap_usd;
    const entry = el("div", "entry");
    if (caps.wallet_daily_cap_state === "disabled") {
      entry.appendChild(el("p", "entry-body", (copy && copy.set_limit_no_cap) || ""));
    } else if (cap == null) {
      entry.appendChild(el("p", "entry-body", (copy && copy.set_limit_unknown) || ""));
    } else {
      entry.appendChild(el("p", "entry-body",
        format(copy && copy.set_limit_daily, { cap: fmtUsd(cap) })));
      if (caps.daily_used_usd != null) {
        entry.appendChild(el("p", "entry-meta",
          format(copy && copy.set_limit_used,
            { used: fmtUsd(caps.daily_used_usd), cap: fmtUsd(cap) })));
      }
    }
    lim.appendChild(entry);
  }
  root.appendChild(lim);

  // The preferences, grouped by intent.
  if (!prefs || prefs.error) {
    root.appendChild(dashedEntry((prefs && prefs.error) || "", copy,
      "set_prefs_unreadable", "set_prefs_unreadable_why"));
    return "unreadable";
  }
  const items = (prefs && prefs.preferences) || [];
  if (!items.length) {
    root.appendChild(noticeEntry(copy && copy.set_prefs_empty));
    return "empty";
  }
  const groups = groupPreferences(items);
  groups.forEach((rows, applies) => {
    const sec = section(applies || (copy && copy.set_prefs_other), "");
    const table = el("table", "table");
    const tbody = el("tbody");
    rows.forEach((p) => {
      const tr = el("tr");
      const what = el("td", "what");
      what.dataset.label = (copy && copy.set_col_what) || "";
      what.appendChild(document.createTextNode(p.description || p.key));
      tr.appendChild(what);
      const val = el("td");
      val.dataset.label = (copy && copy.set_col_value) || "";
      if (p.value != null && p.value !== "") val.appendChild(el("span", "val", String(p.value)));
      else val.appendChild(el("span", "unknown", DASH));
      const effective = val.querySelector("span");
      if (effective) effective.dataset.effective = "1";
      val.appendChild(settingControl(p, copy || {}));
      tr.appendChild(val);
      const src = el("td");
      src.dataset.source = "1";
      if (p.source) src.appendChild(el("span", "why", String(p.source)));
      tr.appendChild(src);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    sec.appendChild(table);
    root.appendChild(sec);
  });

  const more = el("p", "entry-meta", (copy && copy.set_more) || "");
  const link = el("a", "btn btn-quiet", (copy && copy.set_more_link) || "");
  link.href = "#advanced";
  more.appendChild(document.createTextNode(" "));
  more.appendChild(link);
  root.appendChild(more);
  return "settings";
}

/** A USD figure a person reads; a sub-cent value is written out in full, never
 *  rounded to a confident zero. */
export function fmtUsd(n) {
  if (n === null || n === undefined || !Number.isFinite(Number(n))) return DASH;
  const v = Number(n);
  const neg = v < 0;
  const a = Math.abs(v);
  let body;
  if (a !== 0 && a < 0.01) body = a.toFixed(12).replace(/0+$/, "").replace(/\.$/, "");
  else body = a.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (neg ? "-$" : "$") + body;
}

// --- Advanced --------------------------------------------------------------- #
// Every setting, search-first (nothing renders until you ask), plus the full
// diagnostic report behind one disclosure.

/**
 * Draw the flag catalog. Search-first: `queried` false renders the idle state
 * and nothing else — the ~689-row catalog is not a first screen. A guarded or
 * secret flag is MARKED so the owner knows before trying which the console can
 * never write. Returns the answer it drew, for tests.
 */
export function renderFlags(root, data, copy) {
  root.replaceChildren();
  if (!data || !data.queried) {
    const state = el("div", "state");
    state.appendChild(el("h2", "state-title", (copy && copy.adv_idle_title) || ""));
    state.appendChild(el("p", "state-body", (copy && copy.adv_idle_body) || ""));
    root.appendChild(state);
    return "idle";
  }
  if (data.error) {
    root.appendChild(dashedEntry(data.error, copy, "adv_unreadable",
      "adv_unreadable_why"));
    return "unreadable";
  }
  const flags = data.flags || [];
  if (!flags.length) {
    root.appendChild(noticeEntry(copy && copy.adv_no_match));
    return "empty";
  }
  const table = el("table", "table");
  const thead = el("thead");
  const hrow = el("tr");
  [copy && copy.adv_col_name, copy && copy.adv_col_what, copy && copy.adv_col_default]
    .forEach((h) => hrow.appendChild(el("th", null, h || "")));
  thead.appendChild(hrow);
  table.appendChild(thead);
  const tbody = el("tbody");
  flags.forEach((f) => {
    const tr = el("tr");
    tr.dataset.guarded = f.guarded ? "1" : "0";
    const name = el("td", "what");
    name.dataset.label = (copy && copy.adv_col_name) || "";
    name.appendChild(el("span", "val", f.name || ""));
    if (f.guarded) name.appendChild(el("span", "why", (copy && copy.adv_guarded) || ""));
    else if (f.secret) name.appendChild(el("span", "why", (copy && copy.adv_secret) || ""));
    tr.appendChild(name);
    const what = el("td");
    what.dataset.label = (copy && copy.adv_col_what) || "";
    what.textContent = f.description || "";
    tr.appendChild(what);
    const def = el("td");
    def.dataset.label = (copy && copy.adv_col_default) || "";
    if (f.default != null && f.default !== "") def.appendChild(el("span", "val", String(f.default)));
    else def.appendChild(el("span", "unknown", DASH));
    if (Object.hasOwn(f, "value")) {
      def.appendChild(el("p", "why", (copy && copy.control_effective) || ""));
      def.appendChild(el("span", "val", f.value == null ? DASH : String(f.value)));
      if (f.source) def.appendChild(el("p", "why", f.source));
      if (f.applies) def.appendChild(el("p", "why", f.applies));
    }
    def.appendChild(settingControl(f, copy || {}, { flag: true }));
    tr.appendChild(def);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  root.appendChild(table);
  return "rows";
}

/** Draw the group chips from the catalog's own group list. */
export function flagGroupChips(groups, copy, active, onSelect) {
  return (groups || []).map((g) => {
    const chip = el("button", active === g ? "chip is-on" : "chip", g);
    chip.type = "button";
    chip.dataset.group = g;
    chip.setAttribute("aria-pressed", String(active === g));
    chip.addEventListener("click", () => onSelect(g));
    return chip;
  });
}

/** Draw the diagnostics report (the doctor transcript) behind one disclosure. */
export function renderDiagnostics(root, doctor, copy) {
  root.replaceChildren();
  const sec = section(copy && copy.adv_diag_title, copy && copy.adv_diag_aside);
  const pre = document.createElement("pre");
  pre.hidden = true;
  const toggle = el("button", "btn btn-quiet", (copy && copy.adv_diag_show) || "");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  if (!doctor || doctor.error) {
    sec.appendChild(dashedEntry((doctor && doctor.error) || "", copy,
      "adv_diag_unreadable", "adv_diag_unreadable_why"));
    root.appendChild(sec);
    return "unreadable";
  }
  const lines = [].concat(doctor.checks || [], doctor.status_lines || []);
  pre.textContent = lines.length ? lines.join("\n") : (copy && copy.adv_diag_empty) || "";
  toggle.addEventListener("click", () => {
    const open = pre.hidden;
    pre.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.textContent = open ? (copy && copy.adv_diag_hide) || ""
      : (copy && copy.adv_diag_show) || "";
  });
  const actions = el("div", "entry-actions");
  actions.appendChild(toggle);
  sec.appendChild(actions);
  sec.appendChild(pre);
  root.appendChild(sec);
  return "diagnostics";
}

// --- reads ------------------------------------------------------------------ #
// Each tab has its own reader, so one failing store never blanks another. A
// non-200 is shaped `{error}` here; a reader's own honest body (unavailable /
// error) is respected by the renderer above.

async function getJson(url, fetcher) {
  const call = fetcher || fetch;
  const resp = await call(url, { credentials: "include" });
  if (!resp.ok) return { error: String(resp.status) };
  return await resp.json();
}

export const loadDoctor = (f) => getJson("/api/webgate/doctor", f);
export const loadPfp = (f) => getJson("/pfp.json", f);
export const loadIdentity = (f) => getJson("/api/webgate/identity", f);
export const loadCapabilities = (f) => getJson("/api/webgate/capabilities", f);
export const loadMemory = (q, f) =>
  getJson(`/api/webgate/memory/search?query=${encodeURIComponent(q || "")}&limit=20`, f);
export const loadKb = (f) => getJson("/api/webgate/knowledge/kb", f);
export const loadPreferences = (f) => getJson("/api/webgate/preferences", f);
export const loadLedger = (f) => getJson("/api/webgate/ledger", f);
export const loadFlags = (q, group, f) =>
  getJson(`/api/webgate/flags?q=${encodeURIComponent(q || "")}`
    + `&group=${encodeURIComponent(group || "")}`, f);

// --- mutations -------------------------------------------------------------- #

/** Propose an edit to the SELF doc. Returns `{ok, message}` — the row SAYS it,
 *  and the edit lands in the review queue, never live. */
export async function saveSelf(content, copy, opts = {}) {
  try {
    const { ok, body } = await postJson("/api/webgate/self-context", { content },
      { fetcher: opts.fetcher });
    const message = (body && body.message)
      || (ok ? (copy && copy.id_edit_saved) : (copy && copy.id_edit_failed)) || "";
    return { ok: Boolean(ok && body && body.ok), message };
  } catch (err) {
    console.error("[agent] the edit did not reach the console", err);
    return { ok: false, message: (copy && copy.unreachable) || "" };
  }
}

/** Save one curated note. Returns `{ok, message}`. */
export async function addNote(content, copy, opts = {}) {
  try {
    const { ok, body } = await postJson("/api/webgate/memory", { content },
      { fetcher: opts.fetcher });
    const message = (body && body.message)
      || (ok ? (copy && copy.mem_add_done) : (copy && copy.mem_add_failed)) || "";
    return { ok: Boolean(ok && body && body.ok), message };
  } catch (err) {
    console.error("[agent] the note did not reach the console", err);
    return { ok: false, message: (copy && copy.unreachable) || "" };
  }
}

/** Forget one curated note (a soft archive). Returns `{ok, message}`. */
export async function forgetNote(noteId, copy, opts = {}) {
  try {
    const call = opts.fetcher || fetch;
    const resp = await call("/api/webgate/memory", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ note_id: noteId }),
    });
    let body = null;
    try { body = await resp.json(); } catch (err) { body = null; }
    const message = (body && body.message)
      || (resp.ok ? (copy && copy.mem_forgotten) : (copy && copy.mem_forget_failed)) || "";
    return { ok: Boolean(resp.ok && body && body.ok), message };
  } catch (err) {
    console.error("[agent] the forget did not reach the console", err);
    return { ok: false, message: (copy && copy.unreachable) || "" };
  }
}

/** Re-roll the face (a draft ceremony). Returns `{ok, message}`. */
export async function rerollFace(copy, opts = {}) {
  try {
    const { ok, body } = await postJson("/api/pfp/randomize", {},
      { fetcher: opts.fetcher });
    const message = (body && (body.message || body.detail))
      || (ok ? (copy && copy.id_reroll_done) : (copy && copy.id_reroll_failed)) || "";
    return { ok: Boolean(ok), message };
  } catch (err) {
    console.error("[agent] the reroll did not reach the console", err);
    return { ok: false, message: (copy && copy.unreachable) || "" };
  }
}

// --- tabs ------------------------------------------------------------------- #

/** The active tab id, from the URL hash, defaulting to the first tab. */
export function activeTab(hash, tabs) {
  const want = String(hash || "").replace(/^#/, "");
  return (tabs || []).includes(want) ? want : (tabs || [])[0];
}

/** Reveal the pane whose `data-pane` matches `name`, hide the rest, and move
 *  `aria-current` onto its subnav link. */
export function showTab(name, links, panes) {
  (links || []).forEach((a) => {
    if (a.dataset.tab === name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  (panes || []).forEach((p) => { p.hidden = p.dataset.pane !== name; });
}

function byId(id) { return document.getElementById(id); }

function bind() {
  const copyNode = byId("agent-copy");
  if (!copyNode) return;
  const copy = copyFrom(copyNode);
  const readOnly = copy.read_only === "1";

  // --- tabs ---
  const links = Array.from(document.querySelectorAll(".subnav a[data-tab]"));
  const panes = Array.from(document.querySelectorAll("[data-pane]"));
  const tabs = links.map((a) => a.dataset.tab);
  const fired = {};
  const lazy = {};
  function ensure(name) {
    const fn = lazy[name];
    if (!fn || fired[name]) return;
    fired[name] = true;
    fn();
  }
  function apply() {
    const name = activeTab(location.hash, tabs);
    showTab(name, links, panes);
    ensure(name);
  }
  links.forEach((a) => a.addEventListener("click", (ev) => {
    ev.preventDefault();
    const name = a.dataset.tab;
    try { history.replaceState(null, "", `#${name}`); } catch (err) { /* ignore */ }
    showTab(name, links, panes);
    ensure(name);
  }));
  window.addEventListener("hashchange", apply);

  // --- Overview (eager) ---
  const ovHealth = byId("agent-ov-health");
  const ovFace = byId("agent-ov-face");
  const ovPosture = byId("agent-ov-posture");
  const ovConnected = byId("agent-ov-connected");
  if (ovPosture) renderPosture(ovPosture, copy);
  Promise.all([loadDoctor(), loadPfp()])
    .then(([doctor, pfp]) => {
      if (ovHealth) renderHealth(ovHealth, doctor, copy);
      if (ovFace) renderFace(ovFace, pfp, copy);
      if (ovConnected) renderConnected(ovConnected, doctor, copy);
    })
    .catch((err) => {
      console.error("[agent] could not read the overview", err);
      const e = { error: (err && String(err.message)) || "error" };
      if (ovHealth) renderHealth(ovHealth, e, copy);
      if (ovConnected) renderConnected(ovConnected, e, copy);
    });

  // --- Identity (lazy) ---
  const idRoot = byId("agent-identity");
  lazy.identity = () => {
    if (!idRoot) return;
    idRoot.replaceChildren(noticeEntry(copy.id_loading));
    loadIdentity()
      .then((data) => renderIdentity(idRoot, data, copy, { readOnly }))
      .catch((err) => renderIdentity(idRoot,
        { error: (err && String(err.message)) || "error" }, copy, { readOnly }));
  };
  if (idRoot && !readOnly) {
    idRoot.addEventListener("click", async (ev) => {
      const editBtn = ev.target.closest("button[data-edit-self]");
      if (editBtn) {
        const entry = editBtn.closest(".entry");
        const body = entry && entry.querySelector(".entry-body");
        const current = body ? body.textContent : "";
        if (entry) entry.replaceWith(selfEditor(current, copy));
        return;
      }
      const cancel = ev.target.closest("button[data-cancel-self]");
      if (cancel) { lazy.identity(); return; }
      const save = ev.target.closest("button[data-save-self]");
      if (save) {
        const wrap = save.closest("[data-self-editor]");
        const ta = wrap && wrap.querySelector("textarea");
        save.disabled = true;
        const { ok, message } = await saveSelf(ta ? ta.value : "", copy);
        let line = wrap && wrap.querySelector(".entry-answer");
        if (wrap && !line) {
          line = el("p", "entry-meta entry-answer");
          wrap.appendChild(line);
        }
        if (line) line.textContent = message;
        if (!ok) save.disabled = false;
        return;
      }
      const reroll = ev.target.closest("button[data-reroll]");
      if (reroll) {
        reroll.disabled = true;
        const { message } = await rerollFace(copy);
        const answer = el("p", "entry-meta", message);
        reroll.closest(".section").appendChild(answer);
        reroll.disabled = false;
      }
    });
  }

  // --- Capabilities (lazy) ---
  const capRoot = byId("agent-capabilities");
  const capChips = byId("agent-cap-chips");
  const capSearch = byId("agent-cap-search");
  let capData = null;
  let capChip = "";
  function drawCaps() {
    if (capRoot) renderCapabilities(capRoot, capData, copy, { chip: capChip });
    if (capSearch) capSearch.dispatchEvent(new Event("input"));
  }
  lazy.capabilities = () => {
    if (!capRoot) return;
    capRoot.replaceChildren(noticeEntry(copy.cap_loading));
    loadCapabilities()
      .then((data) => { capData = data; drawCaps(); })
      .catch((err) => {
        capData = { error: (err && String(err.message)) || "error" };
        drawCaps();
      });
  };
  if (capChips) {
    const chipDefs = [
      ["", copy.cap_chip_all], ["on", copy.cap_chip_on], ["off", copy.cap_chip_off],
      ["money", copy.cap_chip_money], ["helpers", copy.cap_chip_helpers],
    ];
    chipDefs.forEach(([id, label]) => {
      const chip = el("button", id === capChip ? "chip is-on" : "chip", label || "");
      chip.type = "button";
      chip.dataset.chip = id;
      chip.addEventListener("click", () => {
        capChip = id;
        capChips.querySelectorAll("button").forEach((b) =>
          b.classList.toggle("is-on", b.dataset.chip === id));
        drawCaps();
      });
      capChips.appendChild(chip);
    });
  }
  if (capSearch) {
    capSearch.addEventListener("input", () => {
      const q = capSearch.value.trim().toLowerCase();
      if (!capData || capData.error) return;
      capRoot.querySelectorAll("tbody tr").forEach((tr) => {
        tr.hidden = q ? !tr.textContent.toLowerCase().includes(q) : false;
      });
    });
  }

  // --- Memory (lazy) ---
  const memNotes = byId("agent-mem-notes");
  const memRecall = byId("agent-mem-recall");
  const memKb = byId("agent-mem-kb");
  const memSearch = byId("agent-mem-search");
  let memoryRequest = 0;
  function drawMemory(q) {
    const request = ++memoryRequest;
    if (memNotes) memNotes.replaceChildren(noticeEntry(copy.mem_loading));
    Promise.all([loadMemory(q).catch(err => ({ notes_error: String(err.message), recall_error: String(err.message) })), loadKb().catch(err => ({ error: String(err.message) }))])
      .then(([mem, kb]) => {
        if (request !== memoryRequest) return;
        if (memNotes) renderNotes(memNotes, mem, copy, { readOnly });
        if (memRecall) renderRecall(memRecall, mem, copy);
        if (memKb) renderKb(memKb, kb, copy);
      })
      .catch((err) => {
        if (request !== memoryRequest) return;
        const e = { recall_error: (err && String(err.message)) || "error",
                    notes_error: (err && String(err.message)) || "error" };
        if (memNotes) renderNotes(memNotes, e, copy, { readOnly });
        if (memRecall) renderRecall(memRecall, e, copy);
      });
  }
  lazy.memory = () => drawMemory("");
  if (memSearch) {
    let memTimer = null;
    memSearch.addEventListener("input", () => {
      if (memTimer) clearTimeout(memTimer);
      memTimer = setTimeout(() => drawMemory(memSearch.value.trim()), 250);
    });
  }
  if (memNotes && !readOnly) {
    memNotes.addEventListener("click", async (ev) => {
      const add = ev.target.closest("button[data-add-note]");
      if (add) {
        const sec = add.closest(".section");
        if (sec) sec.after(noteComposer(copy));
        add.disabled = true;
        return;
      }
      const cancel = ev.target.closest("button[data-cancel-note]");
      if (cancel) {
        const comp = cancel.closest("[data-note-composer]");
        if (comp) comp.remove();
        memNotes.querySelectorAll("button[data-add-note]").forEach((b) => { b.disabled = false; });
        return;
      }
      const forget = ev.target.closest("button[data-forget]");
      if (forget) {
        forget.disabled = true;
        const tr = forget.closest("tr");
        const { ok, message } = await forgetNote(Number(forget.dataset.forget), copy);
        if (ok && tr) {
          tr.dataset.forgotten = "1";
          const cell = tr.querySelector(".what");
          if (cell) cell.appendChild(el("span", "why", message));
          forget.remove();
        } else {
          if (tr) tr.querySelector(".what")?.appendChild(el("span", "why", message));
          forget.disabled = false;
        }
        return;
      }
    });
    // The composer's Save lives on the same delegated root as the list.
    memNotes.parentElement && memNotes.parentElement.addEventListener("click", async (ev) => {
      const save = ev.target.closest("button[data-save-note]");
      if (!save) return;
      const comp = save.closest("[data-note-composer]");
      const ta = comp && comp.querySelector("textarea");
      save.disabled = true;
      const { ok, message } = await addNote(ta ? ta.value : "", copy);
      let line = comp && comp.querySelector(".entry-answer");
      if (comp && !line) { line = el("p", "entry-meta entry-answer"); comp.appendChild(line); }
      if (line) line.textContent = message;
      if (ok) drawMemory(memSearch ? memSearch.value.trim() : "");
      else save.disabled = false;
    });
  }

  // --- Settings (lazy) ---
  const setRoot = byId("agent-settings");
  lazy.settings = () => {
    if (!setRoot) return;
    setRoot.replaceChildren(noticeEntry(copy.set_loading));
    Promise.all([loadPreferences().catch(err => ({ error: String(err.message) })), loadLedger().catch(err => ({ error: String(err.message) }))])
      .then(([prefs, ledger]) => renderSettings(setRoot, prefs, ledger, copy))
      .catch((err) => renderSettings(setRoot,
        { error: (err && String(err.message)) || "error" }, null, copy));
  };

  // --- Advanced (lazy) ---
  const advFlags = byId("agent-adv-flags");
  const advChips = byId("agent-adv-chips");
  const advSearch = byId("agent-adv-search");
  const advDiag = byId("agent-adv-diag");
  let advGroup = "";
  let flagRequest = 0;
  function drawFlags() {
    const request = ++flagRequest;
    const q = advSearch ? advSearch.value.trim() : "";
    if (!q && !advGroup) { if (advFlags) renderFlags(advFlags, { queried: false }, copy); return; }
    loadFlags(q, advGroup)
      .then((data) => { if (request === flagRequest && advFlags) renderFlags(advFlags, data, copy); })
      .catch((err) => { if (request === flagRequest && advFlags) renderFlags(advFlags,
        { queried: true, error: (err && String(err.message)) || "error" }, copy); });
  }
  lazy.advanced = () => {
    if (advFlags) renderFlags(advFlags, { queried: false }, copy);
    if (advDiag) loadDoctor()
      .then((doctor) => renderDiagnostics(advDiag, doctor, copy))
      .catch((err) => renderDiagnostics(advDiag,
        { error: (err && String(err.message)) || "error" }, copy));
    // The group chips come from the catalog itself, fetched once.
    if (advChips) loadFlags("", "").then((data) => {
      advChips.replaceChildren();
      flagGroupChips((data && data.groups) || [], copy, advGroup, (g) => {
        advGroup = advGroup === g ? "" : g;
        advChips.querySelectorAll("button").forEach((b) =>
          b.classList.toggle("is-on", b.dataset.group === advGroup));
        drawFlags();
      }).forEach((c) => advChips.appendChild(c));
    }).catch(() => { /* the search still works without the chips */ });
  };
  if (advSearch) {
    let advTimer = null;
    advSearch.addEventListener("input", () => {
      if (advTimer) clearTimeout(advTimer);
      advTimer = setTimeout(drawFlags, 250);
    });
  }

  apply();
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
}
