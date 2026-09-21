/**
 * palette.js — the command palette (043 A23).
 *
 * One list of everything a person can type, grouped exactly the way the
 * terminal's `/help` groups it, reachable from every screen. It is a discovery
 * tool and an autocomplete, not a new authority: selecting a verb only fills
 * the composer with it — the same text you could have typed — and the existing
 * send path carries it. Nothing here runs a verb.
 *
 * Where the words come from:
 *
 *   * the VERB LIST — name, group, one-line help — is `core/verbs.py`, handed
 *     over as a generated `static/app/verbs.en.json` (see scripts/gen_verbs_json.py).
 *     The browser imports no Python, so the one verb table crosses to it as a
 *     committed artifact, pinned against the source by a contract test. The
 *     palette lists EXACTLY that table, which is `dispatcher._COMMANDS` minus
 *     `/task` and `/new` (the two the console has its own UI for —
 *     `webview/console_commands.py`).
 *   * the palette's own CHROME — its title, placeholder, the "not a command"
 *     line, the plain-words note — is the copy layer, handed over on
 *     `#palette-copy`'s data attributes, the same way chats.js reads
 *     `#chats-copy`. No 043 template carries an inline script.
 *
 * Two ways in, and neither is ⌘K — that is already the Chats overlay and stays
 * it (shipped, muscle-memory). The palette opens on **⌘⇧P** from anywhere, and
 * on **`/`** typed into an empty composer (the `/`-triggered list the mockup
 * `chat-palette.html` draws). A verb you type that is not one of mine is not an
 * error: the palette says so, and pressing enter sends it to Rob as a message,
 * which is exactly what the console does with an unknown slash.
 */

/** The copy the server handed over, as a plain object. Empty, not broken, when
 *  the element is missing. */
export function copyFrom(node) {
  return node ? { ...node.dataset } : {};
}

/** A query reduced to what it filters on: a single leading slash dropped,
 *  lower-cased, trimmed. `"/INV"` and `"inv"` and `" /inv "` all become
 *  `"inv"`; a bare `"/"` becomes `""` (which matches everything, as the mockup's
 *  seeded input does). */
export function normalizeQuery(query) {
  let q = String(query || "").trim().toLowerCase();
  if (q.startsWith("/")) q = q.slice(1);
  return q;
}

/**
 * Does *verb* match the normalized *q*? Empty *q* matches everything. Matching
 * is on the NAME and its aliases only — never the help line — so `/inv` narrows
 * to `/invoices` and does NOT also pull in `/settle` because its help says
 * "invoice". A palette that matched prose would make its own suggestions
 * unpredictable.
 */
export function matchVerb(verb, q) {
  if (!q) return true;
  const names = [verb.name, ...(verb.aliases || [])];
  return names.some((n) => String(n || "").slice(1).toLowerCase().includes(q));
}

/**
 * The verbs that match *query*, as `{group, verbs}` pairs in `group_order`.
 * A group with no matches is omitted, and within a group the source order is
 * kept — so the render is the same shape, in the same order, as the REPL's
 * grouped help.
 */
export function filterVerbs(data, query) {
  const q = normalizeQuery(query);
  const order = (data && data.group_order) || [];
  const verbs = (data && data.verbs) || [];
  const out = [];
  order.forEach((group) => {
    const rows = verbs.filter((v) => v.group === group && matchVerb(v, q));
    if (rows.length) out.push({ group, verbs: rows });
  });
  return out;
}

/**
 * Is *query* a slash-command that names nothing? True only when the text is an
 * explicit slash token (`/xyz`) that matches no verb — that is the case the
 * console sends to the LLM as prose, and the case the palette must own up to
 * rather than showing a blank list.
 */
export function isUnknownSlash(data, query) {
  const raw = String(query || "").trim();
  if (!raw.startsWith("/") || raw.length < 2) return false;
  return filterVerbs(data, query).length === 0;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** One selectable verb. A `<button>`, so it is focusable and clickable with no
 *  CSS of its own — `.entry` is defined for exactly this (border:0, full width,
 *  left-aligned). Builds nodes, never markup. */
export function verbRow(verb, copy) {
  const row = el("button", "entry");
  row.type = "button";
  row.dataset.verb = verb.name;
  const title = el("h4", "entry-title");
  title.appendChild(el("span", "val", verb.name));
  row.appendChild(title);
  row.appendChild(el("p", "entry-body", verb.help || ""));
  return row;
}

/**
 * Fill *container* for *query*. Returns what it rendered:
 *   "rows"    — one or more matching groups;
 *   "unknown" — an explicit `/token` that names nothing (the "sends to Rob as a
 *               message" line);
 *   "empty"   — no matches and not a slash token (an ordinary word with no
 *               command; the palette still says so rather than going blank).
 */
export function render(container, data, query, copy) {
  container.replaceChildren();
  const groups = filterVerbs(data, query);
  if (!groups.length) {
    const note = el("p", "state-body");
    if (isUnknownSlash(data, query)) {
      note.textContent = (copy && copy.unknown) || "";
      container.appendChild(note);
      return "unknown";
    }
    note.textContent = (copy && copy.empty) || (copy && copy.unknown) || "";
    container.appendChild(note);
    return "empty";
  }
  groups.forEach(({ group, verbs }) => {
    const head = el("div", "section-head");
    head.appendChild(el("h3", "section-title", group));
    container.appendChild(head);
    const ledger = el("div", "ledger");
    verbs.forEach((verb) => ledger.appendChild(verbRow(verb, copy)));
    container.appendChild(ledger);
  });
  return "rows";
}

// --------------------------------------------------------------------- wiring
// Below here is the live wiring — the dialog, the two openers, the composer
// hand-off. It is exercised by a running console; the unit test covers the pure
// functions above (filter, unknown-slash, render).

let _data = null;

async function loadVerbs() {
  if (_data) return _data;
  try {
    const resp = await fetch("/static/app/verbs.en.json", { credentials: "same-origin" });
    if (!resp.ok) throw new Error(String(resp.status));
    _data = await resp.json();
  } catch (err) {
    console.error("[palette] could not read the verb table", err);
    _data = { group_order: [], verbs: [] };
  }
  return _data;
}

/** The bound composer, if this screen has one. The palette works everywhere;
 *  the hand-off only happens where there is a composer to hand off to. */
function composer() {
  return document.getElementById("chat-input");
}

/**
 * Put *name* into the composer WITHOUT clobbering half-typed text. The palette
 * opens over whatever the person was writing (⌘⇧P does not clear it), so a
 * whole-field overwrite would eat a draft. Three cases:
 *
 *   * the composer's tail is a slash-token being typed (`… /go`) — replace just
 *     that token, keeping the words before it;
 *   * the composer is empty — it becomes the verb;
 *   * otherwise — append the verb after what is there, with one separating
 *     space.
 *
 * Always leaves a trailing space so the next keystroke is an argument. A slash
 * mid-word (`text/path`) is not a token and is left untouched. Returns the new
 * value.
 */
export function insertVerb(input, name) {
  const cur = String(input.value || "");
  const token = cur.match(/(^|\s)\/\S*$/);
  if (token) {
    const before = cur.slice(0, cur.length - token[0].length);
    input.value = `${before}${token[1]}${name} `;
  } else if (cur.trim() === "") {
    input.value = `${name} `;
  } else {
    const sep = /\s$/.test(cur) ? "" : " ";
    input.value = `${cur}${sep}${name} `;
  }
  return input.value;
}

/**
 * Say, inside the still-open palette, that this screen has nowhere to run a
 * verb — and where one IS run. A18.
 *
 * ⚠️ The defect: with no `#chat-input` on the screen (Inbox, Work, Money,
 * Agent — four of the five destinations) selecting a verb simply CLOSED the
 * dialog. Nothing was filled, nothing ran, nothing was said: the palette's
 * only visible behaviour off a chat screen was to disappear, which reads as a
 * verb that ran and did nothing.
 */
export function sayReach(dialog, copy) {
  if (!dialog) return null;
  let note = dialog.querySelector("[data-palette-reach]");
  if (!note) {
    note = document.createElement("p");
    note.className = "state-body";
    note.dataset.paletteReach = "1";
    note.setAttribute("role", "status");
    const results = dialog.querySelector("#palette-results");
    if (results && results.parentNode) results.parentNode.insertBefore(note, results);
    else dialog.appendChild(note);
  }
  note.textContent = (copy && copy.reach) || "";
  return note;
}

function selectVerb(dialog, name, copy) {
  const input = composer();
  if (input) {
    insertVerb(input, name);
    dialog.close();
    input.focus();
    return;
  }
  // No composer on this screen. The palette stays open and states the reach —
  // it does not pretend to have done something and it does not vanish.
  sayReach(dialog, copy);
}

function bind() {
  const dialog = document.getElementById("palette-dialog");
  const input = document.getElementById("palette-input");
  const results = document.getElementById("palette-results");
  if (!dialog || !input || !results) return;
  const copy = copyFrom(document.getElementById("palette-copy"));

  const paint = () => render(results, _data || { group_order: [], verbs: [] },
                            input.value, copy);

  async function open(seed) {
    await loadVerbs();
    // A second ⌘⇧P while the palette is already open must not re-seed (that
    // would wipe an in-progress filter) nor call showModal again (which throws
    // InvalidStateError on an open dialog); it just refocuses the input.
    if (!dialog.open) {
      input.value = seed || "";
      paint();
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    }
    input.focus();
  }

  input.addEventListener("input", paint);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      const first = results.querySelector("button.entry");
      if (first) {
        selectVerb(dialog, first.dataset.verb, copy);
      } else {
        // An unknown slash: hand the raw text back to the composer so pressing
        // enter there sends it to Rob as a message — the console's own rule.
        // Merge it the same way a verb selection does, so a half-typed draft
        // survives here too.
        const text = input.value.trim();
        const box = composer();
        if (box && text) {
          insertVerb(box, text);
          dialog.close();
          box.focus();
        } else {
          sayReach(dialog, copy);
        }
      }
    }
  });

  results.addEventListener("click", (event) => {
    const row = event.target.closest("button.entry");
    if (row && row.dataset.verb) selectVerb(dialog, row.dataset.verb, copy);
  });

  const close = document.getElementById("palette-close");
  if (close) close.addEventListener("click", () => dialog.close());

  // ⌘⇧P / Ctrl-⇧-P opens the palette from anywhere. ⌘K is NOT rebound — it is
  // the Chats overlay and stays it.
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.shiftKey
        && String(event.key).toLowerCase() === "p") {
      event.preventDefault();
      open("");
    }
  });

  // `/` in an EMPTY composer opens the palette seeded with "/", the triggered
  // list the mockup draws. A slash mid-message (a path, a URL, a fraction) is
  // left alone — the palette only intercepts a command's first keystroke.
  const box = composer();
  if (box) {
    box.addEventListener("keydown", (event) => {
      if (event.key === "/" && box.value === "") {
        event.preventDefault();
        open("/");
      }
    });
  }
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
}
