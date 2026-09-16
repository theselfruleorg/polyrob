/**
 * worklog.js — Work › Log: the classified activity stream (043 A16 / §5).
 *
 * The honest version of the /activity terminal the console ships today: the
 * same events, read into the plain classes core/activity_class gives them, with
 * the machine's own bookkeeping behind one switch rather than mixed into the
 * reading.
 *
 * Four rules it keeps:
 *
 * 1. **It names every class it read, from the server's list.** The filter chips
 *    come from `core.activity_class.CLIENT_CLASSES`, handed over on
 *    `#worklog-copy`'s `data-classes` — never a set this file invents, so the
 *    chips can never drift from the classifier.
 * 2. **Diagnostics are off by default.** The endpoint flags each low-signal
 *    engine line (`diagnostic: true`); the switch reveals them. It never guesses
 *    which are noise — that reading is `core.activity_class.is_diagnostic`.
 * 3. **The raw record is one disclosure away, per row.** Each line shows its
 *    one-sentence summary; a toggle reveals the event exactly as it was written,
 *    never a prettied copy of it.
 * 4. **An unreadable source is a dashed entry with its reason, never a silent
 *    drop and never a confident empty list.** `unreadable` set is a failure to
 *    read; an empty list with `unreadable` null is a genuine empty day.
 *
 * Every word a person reads comes from the copy layer on `#worklog-copy`. There
 * is no inline script on any 043 template
 * (tests/unit/webview/test_no_new_inline_script.py), so this is how a string
 * crosses from Python into JS.
 */

/** The copy the server handed over, as a plain object. */
export function copyFrom(node) {
  return node ? { ...node.dataset } : {};
}

/** The ordered class ids the server classified into, from `data-classes`. */
export function classesFrom(node) {
  const raw = (node && node.dataset && node.dataset.classes) || "";
  return raw.split(",").map((c) => c.trim()).filter(Boolean);
}

/** Fill a `{count}` template. Copy owns the words; this owns only the number. */
function fill(template, count) {
  return String(template || "").replace("{count}", String(count));
}

/** A timestamp in plain words, drawn from the event's own `ts` (epoch seconds). */
export function relTime(ts, copy, nowMs) {
  const now = Number.isFinite(nowMs) ? nowMs : Date.now();
  if (!Number.isFinite(ts)) return "";
  const seconds = Math.max(0, now / 1000 - ts);
  if (seconds < 60) return (copy && copy.when_now) || "";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return fill(copy && copy.when_min, minutes);
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return fill(copy && copy.when_hour, hours);
  return fill(copy && copy.when_day, Math.floor(hours / 24));
}

/** The plain word for a class, from copy. An unknown class shows its own id
 *  rather than a guess — the classifier never returns one, but a row will not
 *  invent a word it was not given. */
export function classLabel(cls, copy) {
  const key = `class_${String(cls || "").trim()}`;
  return (copy && copy[key]) || String(cls || "");
}

/**
 * The rows to show, given the current filter. `cls === ""` is every class;
 * diagnostics are dropped unless `showDiagnostics`.
 */
export function filterEntries(entries, { cls = "", showDiagnostics = false } = {}) {
  return (entries || []).filter((e) => {
    if (e.diagnostic && !showDiagnostics) return false;
    if (cls && e.cls !== cls) return false;
    return true;
  });
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** One log line as a `<tr>`, with its per-row raw toggle. Returns a DOM node —
 *  nothing here builds HTML from a string. */
export function entryRow(entry, copy, nowMs) {
  const row = el("tr");
  row.dataset.cls = String(entry.cls || "");
  row.dataset.kind = String(entry.kind || "");
  if (entry.diagnostic) row.dataset.diagnostic = "1";

  const what = el("td", "what");
  what.dataset.label = (copy && copy.header_what) || "";
  what.appendChild(document.createTextNode(entry.summary || ""));
  what.appendChild(el("span", "why", classLabel(entry.cls, copy)));

  // The raw record: present only when the endpoint sent the payload (raw=1).
  if (Object.prototype.hasOwnProperty.call(entry, "payload")) {
    const pre = el("pre");
    pre.hidden = true;
    try {
      pre.textContent = JSON.stringify(entry.payload, null, 2);
    } catch (err) {
      pre.textContent = String(entry.payload);
    }
    const toggle = el("button", "btn btn-quiet", (copy && copy.raw) || "");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", () => {
      const open = pre.hidden;
      pre.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      toggle.textContent = open
        ? ((copy && copy.raw_hide) || "")
        : ((copy && copy.raw) || "");
    });
    what.appendChild(document.createTextNode(" "));
    what.appendChild(toggle);
    what.appendChild(pre);
  }
  row.appendChild(what);

  const when = el("td", "num", relTime(entry.ts, copy, nowMs));
  when.dataset.label = (copy && copy.header_when) || "";
  when.dataset.ts = String(entry.ts != null ? entry.ts : "");
  row.appendChild(when);
  return row;
}

/** The dashed entry for a source that could not be read. `reason` is the
 *  machine detail, kept behind a disclosure — the sentence a person reads is
 *  copy. */
export function unreadableEntry(reason, copy) {
  const entry = el("div", "entry is-unknown");
  entry.appendChild(el("h2", "entry-title", (copy && copy.unreadable) || ""));
  if (reason) {
    const details = el("details");
    details.appendChild(el("summary", null, (copy && copy.unreadable_why) || ""));
    details.appendChild(el("p", "entry-meta", reason));
    entry.appendChild(details);
  }
  return entry;
}

/** The chips: "Everything" plus one per class, in the server's order. Clicking
 *  one calls `onSelect(clsId)` (`""` for Everything). */
export function chipNodes(classes, copy, active, onSelect) {
  const nodes = [];
  const make = (id, label) => {
    const chip = el("button", "chip", label);
    chip.type = "button";
    chip.dataset.cls = id;
    const on = (active || "") === id;
    chip.setAttribute("aria-pressed", String(on));
    if (on) chip.classList.add("is-on");
    chip.addEventListener("click", () => onSelect(id));
    return chip;
  };
  nodes.push(make("", (copy && copy.everything) || ""));
  (classes || []).forEach((id) => nodes.push(make(id, classLabel(id, copy))));
  return nodes;
}

/**
 * Render the stream into *root*, distinguishing three answers:
 *   - `unreadable` set  → a dashed entry with its reason (a read that failed);
 *   - no rows           → the empty state (a genuine empty day);
 *   - rows              → the table, newest first.
 * Returns which of the three it drew, for the test to assert on.
 */
export function render(root, data, state, copy, opts = {}) {
  root.replaceChildren();
  if (data && data.unreadable) {
    root.appendChild(unreadableEntry(data.unreadable, copy));
    if (state) state.hidden = true;
    return "unreadable";
  }
  const rows = filterEntries((data && data.entries) || [], opts);
  if (!rows.length) {
    if (state) {
      state.textContent = (copy && copy.empty) || "";
      state.hidden = false;
    }
    return "empty";
  }
  if (state) state.hidden = true;
  const table = el("table", "table");
  const thead = el("thead");
  const hrow = el("tr");
  hrow.appendChild(el("th", null, (copy && copy.header_what) || ""));
  hrow.appendChild(el("th", null, (copy && copy.header_when) || ""));
  thead.appendChild(hrow);
  table.appendChild(thead);
  const tbody = el("tbody");
  rows.forEach((e) => tbody.appendChild(entryRow(e, copy, opts.nowMs)));
  table.appendChild(tbody);
  root.appendChild(table);
  return "rows";
}

async function load(fetcher) {
  const call = fetcher || fetch;
  const resp = await call("/api/webgate/log?raw=1&diagnostics=1", {
    credentials: "include",
  });
  if (!resp.ok) throw new Error(String(resp.status));
  const data = await resp.json();
  return {
    entries: Array.isArray(data.entries) ? data.entries : [],
    unreadable: data.unreadable || null,
  };
}

function bind() {
  const stream = document.getElementById("worklog-stream");
  const chipsRoot = document.getElementById("worklog-chips");
  const stateNode = document.getElementById("worklog-state");
  const switchNode = document.getElementById("worklog-diagnostics");
  const copyNode = document.getElementById("worklog-copy");
  if (!stream || !copyNode) return;

  const copy = copyFrom(copyNode);
  const classes = classesFrom(copyNode);
  const state = { data: { entries: [], unreadable: null }, cls: "", diagnostics: false };

  function draw() {
    if (chipsRoot) {
      chipsRoot.replaceChildren();
      chipNodes(classes, copy, state.cls, (id) => {
        state.cls = id;
        draw();
      }).forEach((c) => chipsRoot.appendChild(c));
    }
    if (switchNode) {
      switchNode.classList.toggle("is-on", state.diagnostics);
      switchNode.setAttribute("aria-checked", String(state.diagnostics));
    }
    render(stream, state.data, stateNode, copy, {
      cls: state.cls,
      showDiagnostics: state.diagnostics,
    });
  }

  if (switchNode) {
    switchNode.addEventListener("click", () => {
      state.diagnostics = !state.diagnostics;
      draw();
    });
  }

  if (stateNode) {
    stateNode.textContent = copy.loading || "";
    stateNode.hidden = false;
  }
  draw();
  const reload = () => load()
    .then((data) => {
      state.data = data;
      draw();
    })
    .catch((err) => {
      console.error("[worklog] could not read the activity log", err);
      // The fetch never reached the server, so the honest answer is the dashed
      // entry: entries empty AND unreadable set (the machine reason behind the
      // disclosure), never a confident empty list.
      state.data = { entries: [], unreadable: (err && String(err.message)) || "error" };
      draw();
    });
  reload();

  // Live: the stream re-reads itself when live.js relays an activity event
  // (and on the heartbeat), debounced so a burst is one read. The server
  // classifies and tenant-scopes every row; nothing is drawn from the raw
  // socket payload here.
  let pending = null;
  const soon = () => {
    if (pending) return;
    pending = setTimeout(() => { pending = null; reload(); }, 1500);
  };
  document.addEventListener("polyrob:activity", soon);
  document.addEventListener("polyrob:tick", soon);
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
}
