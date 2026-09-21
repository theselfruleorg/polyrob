/**
 * money.js — Money › Book: the ledger beside the chain (043 A34/A35, §3.4).
 *
 * The book leads, because on 2026-08-25 the agent published "book flat" while
 * holding three positions and no seat could have shown the owner otherwise. So
 * this tab renders, in order:
 *
 *   1. the VERDICT, first. It is a typed value from the reader
 *      (`core.book.BookVerdict`: clean / disagreement / unverified / no_ledger),
 *      never a regex over the verb's prose. A `disagreement` or an `unverified`
 *      verdict NEVER renders as green agreement — that collapse is the exact
 *      failure the typed verdict exists to stop.
 *   2. the POSITIONS the ledger claims, each beside the chain's reading. A
 *      value no store carries reads `—` WITH its reason, never `$0.00` and
 *      never a blank — a confident zero on the page where a wrong number costs
 *      real money is the worst failure the screen has.
 *   3. the CHAINS Rob checked, with the cache age. An unreadable chain is a
 *      dashed group with its reason; the positions total then names what it
 *      left out. A read that failed is `unverified`, never zero, never green.
 *
 * With no wallet there is nothing true to say, so the tab shows the empty
 * state rather than an empty table and three zeroes.
 *
 * Every word a person reads comes from the copy layer on `#money-copy`. There
 * is no inline script on any 043 template
 * (tests/unit/webview/test_no_new_inline_script.py), so this is how a string
 * crosses from Python into JS. Book leads; Moves, Cash, Invoices and Limits are
 * the other four panes of this one page, each drawn from its own reader.
 */
import { postJson } from "./http.js";

const DASH = "—"; // — : an unknown value, never $0.00 or a blank.

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

/** Fill a single `{name}` field. Copy owns the words; this owns only the value. */
function fill(template, name, value) {
  return String(template || "").replace(`{${name}}`, String(value));
}

/** A timestamp in plain words, from the reader's own `checked_at` (epoch
 *  seconds). The words are copy; only the number is computed here. */
export function relTime(ts, copy, nowMs) {
  const now = Number.isFinite(nowMs) ? nowMs : Date.now();
  if (!Number.isFinite(ts)) return "";
  const seconds = Math.max(0, now / 1000 - ts);
  if (seconds < 60) return (copy && copy.when_now) || "";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return fill(copy && copy.when_min, "count", minutes);
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return fill(copy && copy.when_hour, "count", hours);
  return fill(copy && copy.when_day, "count", Math.floor(hours / 24));
}

/** A USD figure a person reads. A sub-cent value is written out in full, never
 *  rounded to `$0.00` — the two-decimal format renders every worthless token as
 *  the one number that means worthless. `null` is the caller's to dash. */
export function fmtUsd(n) {
  if (n === null || n === undefined || !Number.isFinite(n)) return null;
  const neg = n < 0;
  const a = Math.abs(n);
  let body;
  if (a !== 0 && a < 0.01) {
    body = a.toFixed(12).replace(/0+$/, "").replace(/\.$/, "");
  } else {
    body = a.toLocaleString("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  }
  return (neg ? "-$" : "$") + body;
}

/** A token amount a person reads, with thousands separators. `null` when the
 *  ledger carries no quantity — the caller dashes it. */
export function fmtAmount(n) {
  if (n === null || n === undefined || !Number.isFinite(n)) return null;
  return n.toLocaleString("en-US", { maximumFractionDigits: 6 });
}

/** A chain reads `unverified` when its read failed — never zero, never green. */
export function chainUnreadable(chain) {
  return Boolean(chain) && chain.verdict === "unverified";
}

/** The chains whose read failed, in the reader's order — what the positions
 *  total must name as excluded, and what the chains section dashes. */
export function excludedChains(data) {
  const chains = (data && data.chains) || {};
  return Object.keys(chains).filter((name) => chainUnreadable(chains[name]));
}

/** Nothing true to say: no positions and no chain produced a reading (no
 *  wallet, or on-chain sight is off). The tab then shows the empty state
 *  rather than an empty table and three confident zeroes. */
export function isEmpty(data) {
  if (!data) return true;
  const rows = data.rows || [];
  const chains = data.chains || {};
  return rows.length === 0 && Object.keys(chains).length === 0;
}

/** A cell that carries a value, or `—` WITH the reason it is unknown. The
 *  reason is the machine's own (from the row), kept behind a disclosure so the
 *  sentence a person reads stays short. */
function valueCell(value, reason, label, copy) {
  const td = el("td", "num");
  if (label) td.dataset.label = label;
  if (value === null || value === undefined) {
    td.appendChild(el("span", "unknown", DASH));
    if (reason) td.appendChild(el("span", "unknown-why", reason));
  } else {
    td.appendChild(document.createTextNode(String(value)));
  }
  return td;
}

/**
 * The verdict banner, FIRST on the page. Its class and colour are keyed to the
 * typed value, and `dataset.verdict` carries the value itself so the reading is
 * checkable. `disagreement`/`unverified` can never take the green branch.
 */
export function verdictBanner(data, copy, opts = {}) {
  const verdict = (data && data.verdict) || "";
  const banner = el("div", "banner");
  banner.dataset.verdict = verdict;
  let glyph = "";
  if (verdict === "clean") {
    banner.style.borderLeft = "3px solid var(--running)";
    glyph = "✓"; // ✓
  } else if (verdict === "disagreement") {
    banner.classList.add("is-stopped");
    banner.style.borderLeft = "3px solid var(--stopped)";
    glyph = "⚠"; // ⚠
  } else if (verdict === "unverified") {
    banner.style.borderLeft = "3px dashed var(--unknown)";
    glyph = DASH;
  } else {
    // no_ledger, or a value the reader did not name: neutral, never green.
    glyph = "◇"; // ◇
  }
  const g = el("span", null, glyph);
  g.setAttribute("aria-hidden", "true");
  banner.appendChild(g);

  const body = el("span");
  body.appendChild(el("b", null, (copy && copy[verdict]) || verdict));
  const when = relTime(data && data.checked_at, copy, opts.nowMs);
  if (when) {
    body.appendChild(document.createTextNode(" "));
    body.appendChild(document.createTextNode(
      fill(copy && copy.checked, "when", when)));
  }
  if (typeof opts.onRecheck === "function") {
    const btn = el("button", "btn btn-quiet", (copy && copy.recheck) || "");
    btn.type = "button";
    btn.style.marginLeft = "6px";
    btn.addEventListener("click", () => opts.onRecheck());
    body.appendChild(document.createTextNode(" "));
    body.appendChild(btn);
  }
  banner.appendChild(body);
  return banner;
}

/**
 * The positions the ledger claims, each beside the chain's reading. A row is on
 * this table only because the ledger says so; a value no store carries reads
 * `—` with its reason. The total sums only the priced rows and names the chains
 * it excluded, so it is a figure a person can trust rather than a sum that
 * quietly dropped an unreadable chain to zero.
 */
export function positionsSection(data, copy) {
  const rows = (data && data.rows) || [];
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", (copy && copy.holds_title) || ""));
  head.appendChild(el("span", "section-aside",
    fill(copy && copy.holds_count, "count", rows.length)));
  section.appendChild(head);

  if (!rows.length) {
    section.appendChild(el("p", "state-body", (copy && copy.no_positions) || ""));
    return section;
  }

  const table = el("table", "table");
  const thead = el("thead");
  const hrow = el("tr");
  [copy.col_position, copy.col_chain, copy.col_amount, copy.col_worth,
   copy.col_since].forEach((label, i) => {
    hrow.appendChild(el("th", i >= 2 ? "num" : null, label || ""));
  });
  thead.appendChild(hrow);
  table.appendChild(thead);

  const tbody = el("tbody");
  let total = 0;
  let anyPriced = false;
  rows.forEach((row) => {
    const tr = el("tr");
    if (row.state) tr.dataset.state = String(row.state);

    const what = el("td", "what");
    what.dataset.label = copy.col_position || "";
    what.appendChild(document.createTextNode(row.symbol || "?"));
    // The entry price when the store carries it, else the reason it does not,
    // else why the position could not be priced — never a bare number.
    let why = "";
    if (Number.isFinite(row.entry)) {
      why = fill(copy.entry_at, "price", fmtUsd(row.entry));
    } else if (row.entry_reason) {
      why = row.entry_reason;
    } else if (row.worth_now_reason) {
      why = row.worth_now_reason;
    }
    if (why) what.appendChild(el("span", "why", why));
    tr.appendChild(what);

    const chain = el("td");
    chain.dataset.label = copy.col_chain || "";
    if (row.chain) {
      chain.textContent = String(row.chain);
    } else {
      chain.appendChild(el("span", "unknown", DASH));
    }
    tr.appendChild(chain);

    tr.appendChild(valueCell(fmtAmount(row.amount), null, copy.col_amount, copy));

    const worth = fmtUsd(row.worth_now);
    if (worth !== null) { total += row.worth_now; anyPriced = true; }
    tr.appendChild(valueCell(worth, row.worth_now_reason, copy.col_worth, copy));

    tr.appendChild(valueCell(row.since_entry, row.since_entry_reason,
                             copy.col_since, copy));
    tbody.appendChild(tr);
  });

  // The total row: only the priced positions, and it says what it left out.
  const trTotal = el("tr");
  trTotal.dataset.total = "1";
  const label = el("td", "what", (copy && copy.total_label) || "");
  trTotal.appendChild(label);
  trTotal.appendChild(el("td"));
  trTotal.appendChild(el("td"));
  const excluded = excludedChains(data);
  const totalCell = el("td", "num");
  totalCell.dataset.label = copy.col_worth || "";
  totalCell.appendChild(document.createTextNode(
    anyPriced ? fmtUsd(total) : DASH));
  if (excluded.length) {
    totalCell.appendChild(el("span", "unknown-why",
      fill(copy && copy.total_excludes, "chains", excluded.join(", "))));
  }
  trTotal.appendChild(totalCell);
  trTotal.appendChild(el("td"));
  tbody.appendChild(trTotal);

  table.appendChild(tbody);
  section.appendChild(table);
  return section;
}

/**
 * The chains Rob checked, and when. A chain that read is shown plainly; a chain
 * whose read failed is a DASHED group with its reason — never a zero balance,
 * which would read exactly like an empty wallet. This is the read STATE of each
 * money chain from the reader's `chains` block, with the cache age; spendable
 * balances live under Cash.
 */
export function chainsSection(data, copy, nowMs) {
  const chains = (data && data.chains) || {};
  const names = Object.keys(chains);
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", (copy && copy.chains_title) || ""));
  const when = relTime(data && data.checked_at, copy, nowMs);
  if (when) {
    head.appendChild(el("span", "section-aside",
      fill(copy && copy.chains_age, "when", when)));
  }
  section.appendChild(head);

  const ledger = el("div", "ledger");
  names.forEach((name) => {
    const chain = chains[name] || {};
    const unreadable = chainUnreadable(chain);
    const entry = el("div", unreadable ? "entry is-unknown" : "entry");
    entry.dataset.chain = name;
    entry.appendChild(el("h3", "entry-title", name));
    if (unreadable) {
      entry.appendChild(el("p", "entry-body", (copy && copy.chain_unread) || ""));
      if (chain.error) {
        const details = el("details");
        details.appendChild(el("summary", null, (copy && copy.chain_unread_why) || ""));
        details.appendChild(el("p", "entry-meta", chain.error));
        entry.appendChild(details);
      }
    } else {
      entry.appendChild(el("p", "entry-body", (copy && copy.chain_read) || ""));
    }
    ledger.appendChild(entry);
  });
  section.appendChild(ledger);
  return section;
}

/** The trading run lives in Work — this is navigation, not a data claim, so it
 *  reads nothing and states nothing it cannot back. */
export function runSection(copy) {
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", (copy && copy.run_title) || ""));
  head.appendChild(el("span", "section-aside", (copy && copy.run_aside) || ""));
  section.appendChild(head);
  const ledger = el("div", "ledger");
  const entry = el("div", "entry");
  entry.appendChild(el("p", "entry-body", (copy && copy.run_body) || ""));
  const actions = el("div", "entry-actions");
  const watch = el("a", "btn btn-quiet", (copy && copy.run_watch) || "");
  watch.href = "/work";
  actions.appendChild(watch);
  entry.appendChild(actions);
  ledger.appendChild(entry);
  section.appendChild(ledger);
  return section;
}

/** No wallet, on-chain sight off, or nothing to read: the one true thing and
 *  the one action, never an empty table and three zeroes. The machine reason,
 *  when there is one, sits behind a disclosure. */
export function emptyState(data, copy) {
  const state = el("div", "state");
  const glyph = el("div", "state-glyph", "◇");
  glyph.setAttribute("aria-hidden", "true");
  state.appendChild(glyph);
  state.appendChild(el("h2", "state-title", (copy && copy.empty_title) || ""));
  state.appendChild(el("p", "state-body", (copy && copy.empty_body) || ""));
  const reason = data && data.error;
  if (reason) {
    const details = el("details");
    details.appendChild(el("summary", null, (copy && copy.empty_why) || ""));
    details.appendChild(el("p", "entry-meta", reason));
    state.appendChild(details);
  }
  return state;
}

/** The dashed entry for a book that could not be read at all (the fetch itself
 *  failed) — never a confident empty book. */
export function unreadableEntry(reason, copy) {
  const entry = el("div", "state");
  entry.appendChild(el("h2", "state-title", (copy && copy.unreadable) || ""));
  if (reason) {
    const details = el("details");
    details.appendChild(el("summary", null, (copy && copy.unreadable_why) || ""));
    details.appendChild(el("p", "entry-meta", reason));
    entry.appendChild(details);
  }
  return entry;
}

/**
 * Render the Book pane into *root*, distinguishing four answers:
 *   - `unreadable` set  → the fetch failed; a dashed state with its reason;
 *   - no wallet/nothing → the empty state;
 *   - a reading         → the verdict FIRST, then positions, chains, the run.
 * Returns which it drew, for the test to assert on.
 */
export function render(root, data, copy, opts = {}) {
  root.replaceChildren();
  if (data && data.unreadable) {
    root.appendChild(unreadableEntry(data.unreadable, copy));
    return "unreadable";
  }
  if (isEmpty(data)) {
    root.appendChild(emptyState(data, copy));
    return "empty";
  }
  root.appendChild(verdictBanner(data, copy, opts));
  root.appendChild(positionsSection(data, copy));
  root.appendChild(chainsSection(data, copy, opts.nowMs));
  root.appendChild(runSection(copy));
  return "book";
}

async function load(fetcher) {
  const call = fetcher || fetch;
  const resp = await call("/api/webgate/book", { credentials: "include" });
  if (!resp.ok) throw new Error(String(resp.status));
  return await resp.json();
}

// --- small shared helpers (used by Moves / Cash / Invoices / Limits) -------- #

/** Fill every `{name}` from a vars object. Copy owns the words; this owns only
 *  the values. (Book's `fill` replaces ONE named token; this one is multi-var.) */
export function formatVars(template, vars) {
  return String(template || "").replace(/\{(\w+)\}/g, (m, key) =>
    vars && Object.prototype.hasOwnProperty.call(vars, key)
      ? String(vars[key]) : m);
}

/** A plain informational entry (empty state, loading, feature-off). */
export function noticeEntry(text) {
  const entry = el("div", "entry");
  entry.appendChild(el("p", "entry-body", text || ""));
  return entry;
}

/** The dashed entry for a store that could not be read — the reason behind a
 *  disclosure, never a confident empty list and never a fabricated zero. */
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

/** A link OUT to an explorer / another page. It opens in a new tab and never
 *  decides anything — a report never spends. */
export function linkOut(label, href) {
  const a = el("a", "btn btn-quiet", label || "");
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}

/** A dd/td money value: the figure, or `—` WITH its reason. Never `$0.00` for an
 *  unknown. Returns a DocumentFragment so a caller can append it as a cell body. */
function moneyNode(value, reason) {
  const frag = document.createDocumentFragment();
  const f = fmtUsd(value);
  if (f !== null) {
    frag.appendChild(document.createTextNode(f));
    return frag;
  }
  frag.appendChild(el("span", "unknown", DASH));
  if (reason) frag.appendChild(el("span", "unknown-why", reason));
  return frag;
}

function byId(id) { return document.getElementById(id); }

// --- Moves: in flight (bridges) + recent moves + what Rob has made ---------- #

/** One open cross-chain bridge. A bridge past its deadline is 'in flight', never
 *  'failed' — a re-sent bridge pays twice — so this states that plainly and
 *  links the ORIGIN transaction only (the destination hash is a different chain). */
export function bridgeEntry(b, copy, nowMs) {
  const entry = el("div", "entry is-running");
  entry.dataset.bridgeId = String((b && b.id) || "");
  if (b && b.state) entry.dataset.state = String(b.state);
  entry.appendChild(el("h3", "entry-title", formatVars(copy && copy.mv_bridge_route, {
    origin: (b && b.origin_chain) || DASH, dest: (b && b.dest_chain) || DASH })));
  const amt = fmtUsd(b && b.amount_usd);
  const age = relTime(b && b.created_at, copy, nowMs);
  entry.appendChild(el("p", "entry-body", formatVars(copy && copy.mv_bridge_line, {
    amount: amt || DASH, age: age || DASH })));
  entry.appendChild(el("p", "entry-body", (copy && copy.mv_bridge_safe) || ""));
  if (b && b.origin_link) {
    const actions = el("div", "entry-actions");
    actions.appendChild(linkOut(copy && copy.mv_onchain, b.origin_link));
    entry.appendChild(actions);
  }
  return entry;
}

/** In flight — open bridges from bridge_guard. An unreadable store is dashed
 *  with its reason; an empty board is a plain notice. */
export function inflightSection(data, copy, nowMs) {
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", (copy && copy.mv_inflight_title) || ""));
  const rows = (data && data.bridges) || [];
  const failed = Boolean(data && (data.error || data.unreadable || rows === null));
  if (!failed) {
    head.appendChild(el("span", "section-aside",
      fill(copy && copy.mv_inflight_aside, "count", rows.length)));
  }
  section.appendChild(head);
  const ledger = el("div", "ledger");
  if (failed || !data || data.bridges === null) {
    ledger.appendChild(dashedEntry((data && (data.error || data.unreadable)) || "",
      copy, "mv_inflight_unreadable", "mv_inflight_unreadable_why"));
  } else if (!rows.length) {
    ledger.appendChild(noticeEntry(copy && copy.mv_inflight_empty));
  } else {
    rows.forEach((b) => ledger.appendChild(bridgeEntry(b, copy, nowMs)));
  }
  section.appendChild(ledger);
  return section;
}

/** One recent move (a wallet_spend row). Amount is `—` when nothing left the
 *  wallet (a permission) or the figure is not recorded — never `$0.00`. */
export function moveRow(m, copy, nowMs) {
  const tr = el("tr");
  const when = el("td", "num");
  when.dataset.label = (copy && copy.mv_col_when) || "";
  when.textContent = relTime(m && m.ts, copy, nowMs) || DASH;
  tr.appendChild(when);
  const what = el("td", "what");
  what.dataset.label = (copy && copy.mv_col_what) || "";
  what.textContent = (m && m.action) || DASH;
  tr.appendChild(what);
  const chain = el("td");
  chain.dataset.label = (copy && copy.mv_col_chain) || "";
  if (m && m.chain) chain.textContent = String(m.chain);
  else chain.appendChild(el("span", "unknown", DASH));
  tr.appendChild(chain);
  const raw = m && m.amount_usd;
  const hasAmt = Number.isFinite(raw) && raw !== 0;
  tr.appendChild(valueCell(hasAmt ? fmtUsd(raw) : null,
    raw === 0 ? (copy && copy.mv_no_amount) : (copy && copy.mv_amount_unknown),
    copy && copy.mv_col_amount, copy));
  const act = el("td");
  if (m && m.url) act.appendChild(linkOut(copy && copy.mv_onchain, m.url));
  tr.appendChild(act);
  return tr;
}

/** Recent moves — wallet_spend rows (creations are their own tier below). An
 *  unavailable store is dashed with its reason; an unreadable row is COUNTED and
 *  named, never silently dropped. */
export function recentMovesSection(data, copy, nowMs) {
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", (copy && copy.mv_recent_title) || ""));
  section.appendChild(head);
  const rows = (data && data.moves) || [];
  if (data && (data.error || data.unavailable || data.moves === null)) {
    section.appendChild(dashedEntry((data.error || data.unavailable) || "",
      copy, "mv_recent_unreadable", "mv_recent_unreadable_why"));
    return section;
  }
  if (!rows.length) {
    section.appendChild(noticeEntry(copy && copy.mv_recent_empty));
    return section;
  }
  const table = el("table", "table");
  const thead = el("thead");
  const hrow = el("tr");
  [[copy && copy.mv_col_when, "num"], [copy && copy.mv_col_what, null],
   [copy && copy.mv_col_chain, null], [copy && copy.mv_col_amount, "num"],
   ["", null]].forEach(([label, cls]) => hrow.appendChild(el("th", cls, label || "")));
  thead.appendChild(hrow);
  table.appendChild(thead);
  const tbody = el("tbody");
  rows.forEach((m) => tbody.appendChild(moveRow(m, copy, nowMs)));
  table.appendChild(tbody);
  section.appendChild(table);
  if (data.unreadable_rows) {
    section.appendChild(el("p", "entry-meta",
      fill(copy && copy.mv_recent_partial, "count", data.unreadable_rows)));
  }
  section.appendChild(el("p", "entry-meta", (copy && copy.mv_recent_note) || ""));
  return section;
}

/** One creation Rob made on-chain: the verb, the chain, the address, when, and
 *  its cost. The address links out when the reader could build an explorer URL. */
export function creationRow(c, copy, nowMs) {
  const tr = el("tr");
  const what = el("td", "what");
  what.dataset.label = (copy && copy.mv_col_token) || "";
  what.textContent = (c && c.action) || DASH;
  tr.appendChild(what);
  const chain = el("td");
  chain.dataset.label = (copy && copy.mv_col_chain) || "";
  if (c && c.chain) chain.textContent = String(c.chain);
  else chain.appendChild(el("span", "unknown", DASH));
  tr.appendChild(chain);
  const addr = el("td");
  addr.dataset.label = (copy && copy.mv_col_address) || "";
  if (c && c.address) addr.appendChild(el("span", "val", String(c.address)));
  else addr.appendChild(el("span", "unknown", DASH));
  tr.appendChild(addr);
  const when = el("td");
  when.dataset.label = (copy && copy.mv_col_when) || "";
  when.textContent = relTime(c && c.ts, copy, nowMs) || DASH;
  tr.appendChild(when);
  tr.appendChild(valueCell(fmtUsd(c && c.usd), null, copy && copy.mv_col_cost, copy));
  const act = el("td");
  if (c && c.url) act.appendChild(linkOut(copy && copy.mv_onchain, c.url));
  tr.appendChild(act);
  return tr;
}

/** What Rob has made — creations over the SAME wallet_spend events the status
 *  snapshot reads. Unavailable (creations === null) is dashed with its reason;
 *  "nothing deployed" and "cannot see what I created" are different facts. */
export function madeSection(data, copy, nowMs) {
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", (copy && copy.mv_made_title) || ""));
  const unavailable = Boolean(data && (data.error
    || data.creations === null || data.creations === undefined));
  const rows = (data && data.creations) || [];
  if (!unavailable) {
    head.appendChild(el("span", "section-aside",
      fill(copy && copy.mv_made_aside, "count", rows.length)));
  }
  section.appendChild(head);
  if (unavailable) {
    section.appendChild(dashedEntry((data && (data.error || data.reason)) || "",
      copy, "mv_made_unreadable", "mv_made_unreadable_why"));
    return section;
  }
  if (!rows.length) {
    section.appendChild(noticeEntry(copy && copy.mv_made_empty));
    return section;
  }
  const table = el("table", "table");
  const thead = el("thead");
  const hrow = el("tr");
  [[copy && copy.mv_col_token, null], [copy && copy.mv_col_chain, null],
   [copy && copy.mv_col_address, null], [copy && copy.mv_col_when, null],
   [copy && copy.mv_col_cost, "num"], ["", null]].forEach(
    ([label, cls]) => hrow.appendChild(el("th", cls, label || "")));
  thead.appendChild(hrow);
  table.appendChild(thead);
  const tbody = el("tbody");
  rows.forEach((c) => tbody.appendChild(creationRow(c, copy, nowMs)));
  table.appendChild(tbody);
  section.appendChild(table);
  if (data.unreadable_rows) {
    section.appendChild(el("p", "entry-meta",
      fill(copy && copy.mv_made_partial, "count", data.unreadable_rows)));
  }
  return section;
}

/** Draw Moves: in flight (bridges), recent moves (wallet_spend), what Rob made
 *  (creations). Each is its own reader, so one failing is dashed on its own and
 *  never blanks the others. */
export function renderMoves(root, bridges, moves, creations, copy, opts = {}) {
  const nowMs = Number.isFinite(opts.nowMs) ? opts.nowMs : Date.now();
  root.replaceChildren();
  root.appendChild(inflightSection(bridges, copy, nowMs));
  root.appendChild(recentMovesSection(moves, copy, nowMs));
  root.appendChild(madeSection(creations, copy, nowMs));
  // A25: Money has exactly ONE mutation (settle an invoice), and this pane
  // reports bridges, launches and deployments it cannot start. Say where those
  // verbs live rather than leaving a page that looks like a trading desk with
  // its buttons missing. The reach decision is recorded: the terminal and
  // Telegram own the money verbs; the console reports them.
  root.appendChild(el("p", "sources", (copy && copy.mv_reach) || ""));
  return "moves";
}

// --- Cash: wallet identity + two ledgers, NEVER summed ---------------------- #

/**
 * The owner wallet as an inspectable identity, not a send form. Addresses,
 * cached balances, signing availability and unresolved broadcasts come from
 * the shared wallet view. Unknown/stale data is labelled; this renderer never
 * turns an unread balance into zero and never exposes a signing action.
 */
export function walletSection(wallet, copy) {
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", (copy && copy.wallet_title) || ""));
  section.appendChild(head);

  if (!wallet || wallet.error || wallet.state === "unavailable") {
    const reason = (wallet && (wallet.error || (wallet.errors || [])[0])) || "";
    section.appendChild(dashedEntry(reason, copy,
      "wallet_unavailable", "wallet_unavailable_why"));
    return section;
  }
  if (wallet.state === "disabled") {
    section.appendChild(noticeEntry(copy && copy.wallet_disabled));
    return section;
  }

  const summary = el("div", "ledger");
  const identity = el("div", "entry");
  identity.dataset.walletState = String(wallet.state || "unknown");
  const stateKey = wallet.state === "ready" ? "wallet_ready" : "wallet_public_only";
  identity.appendChild(el("h3", "entry-title", (copy && copy[stateKey]) || wallet.state));
  identity.appendChild(el("p", "entry-body", formatVars(copy && copy.wallet_network, {
    network: wallet.network || DASH,
  })));
  identity.appendChild(el("p", "entry-meta", wallet.signing_available
    ? (copy && copy.wallet_signing_ready) || ""
    : (copy && copy.wallet_signing_unavailable) || ""));
  summary.appendChild(identity);
  section.appendChild(summary);

  const accounts = Array.isArray(wallet.accounts) ? wallet.accounts : [];
  if (accounts.length) {
    const title = el("div", "section-head");
    title.appendChild(el("h3", "section-title", (copy && copy.wallet_accounts) || ""));
    section.appendChild(title);
    const table = el("table", "table");
    const thead = el("thead");
    const hrow = el("tr");
    [copy.wallet_col_role, copy.wallet_col_network, copy.wallet_col_address,
      copy.wallet_col_use].forEach((label) => hrow.appendChild(el("th", null, label || "")));
    thead.appendChild(hrow); table.appendChild(thead);
    const tbody = el("tbody");
    accounts.forEach((account) => {
      const row = el("tr");
      const role = el("td", "what", account.role || DASH);
      role.dataset.label = copy.wallet_col_role || "";
      row.appendChild(role);
      const family = el("td", null, account.family || DASH);
      family.dataset.label = copy.wallet_col_network || "";
      row.appendChild(family);
      const address = el("td", "num", account.address || DASH);
      address.dataset.label = copy.wallet_col_address || "";
      if (!account.address) address.classList.add("unknown");
      row.appendChild(address);
      const use = el("td", null, account.receive
        ? (copy && copy.wallet_receive) || ""
        : (copy && copy.wallet_do_not_fund) || "");
      use.dataset.label = copy.wallet_col_use || "";
      row.appendChild(use);
      tbody.appendChild(row);
    });
    table.appendChild(tbody); section.appendChild(table);
  }

  const balances = wallet.balances || { state: "unread", chains: [] };
  const balanceHead = el("div", "section-head");
  balanceHead.appendChild(el("h3", "section-title", (copy && copy.wallet_balances) || ""));
  const balanceState = copy && copy[`wallet_balances_${balances.state}`];
  if (balanceState) balanceHead.appendChild(el("span", "section-aside", balanceState));
  section.appendChild(balanceHead);
  const chains = Array.isArray(balances.chains) ? balances.chains : [];
  if (!chains.length) {
    section.appendChild(noticeEntry((copy && copy.wallet_balances_unread) || ""));
  } else {
    const table = el("table", "table");
    const thead = el("thead"); const hrow = el("tr");
    [copy.wallet_col_chain, copy.wallet_col_native, copy.wallet_col_usdc]
      .forEach((label, i) => hrow.appendChild(el("th", i ? "num" : null, label || "")));
    thead.appendChild(hrow); table.appendChild(thead);
    const tbody = el("tbody");
    chains.forEach((chain) => {
      const row = el("tr");
      const name = el("td", "what", chain.chain || DASH);
      name.dataset.label = copy.wallet_col_chain || "";
      row.appendChild(name);
      const native = chain.native == null ? null
        : `${fmtAmount(chain.native)} ${chain.symbol || ""}`.trim();
      row.appendChild(valueCell(native, copy.wallet_balance_unknown,
        copy.wallet_col_native, copy));
      row.appendChild(valueCell(chain.usdc == null ? null : fmtUsd(chain.usdc),
        copy.wallet_balance_unknown, copy.wallet_col_usdc, copy));
      tbody.appendChild(row);
    });
    table.appendChild(tbody); section.appendChild(table);
  }

  const unresolved = Array.isArray(wallet.unaccounted_submissions)
    ? wallet.unaccounted_submissions : [];
  if (unresolved.length) {
    const warning = el("div", "entry is-needs-you");
    warning.appendChild(el("h3", "entry-title", copy && copy.wallet_unaccounted));
    warning.appendChild(el("p", "entry-body", copy && copy.wallet_unaccounted_body));
    unresolved.forEach((item) => warning.appendChild(el("p", "entry-meta",
      `${item.chain || DASH}: ${item.tx_hash || DASH}`)));
    section.appendChild(warning);
  }
  return section;
}

/** One cash-flow book card (Income or Spent). The figure is `—` with its reason
 *  when the block is not a trustworthy read (a fabricated `$0.00` is the worst
 *  thing this card can show). */
function cashBook(cls, what, whose, figure, figureReason, lines) {
  const book = el("div", `book ${cls}`);
  book.appendChild(el("p", "book-what", what || ""));
  book.appendChild(el("p", "book-whose", whose || ""));
  const fig = el("p", "book-figure");
  const f = fmtUsd(figure);
  if (f === null) {
    fig.appendChild(el("span", "unknown", DASH));
    if (figureReason) fig.appendChild(el("span", "unknown-why", figureReason));
  } else {
    fig.textContent = f;
  }
  book.appendChild(fig);
  const dl = el("dl", "book-lines");
  lines.forEach(([dt, node]) => {
    const kv = el("div", "kv");
    kv.appendChild(el("dt", null, dt || ""));
    const dd = el("dd");
    if (node && node.nodeType) dd.appendChild(node);
    else dd.textContent = String(node);
    kv.appendChild(dd);
    dl.appendChild(kv);
  });
  book.appendChild(dl);
  return book;
}

/** One row of "what it costs you to run Rob". A failed read is `—` with its
 *  reason; the aside carries the call count or the balance note. */
function runtimeRow(label, value, reason, aside) {
  const tr = el("tr");
  tr.appendChild(el("td", "what", label || ""));
  const num = el("td", "num");
  const f = fmtUsd(value);
  if (f === null) num.appendChild(el("span", "unknown", DASH));
  else num.textContent = f;
  tr.appendChild(num);
  const meta = el("td");
  if (f === null && reason) meta.appendChild(el("span", "unknown-why", reason));
  else if (aside) meta.textContent = aside;
  tr.appendChild(meta);
  return tr;
}

/**
 * Draw Cash. `ledger` is the /api/webgate/ledger body. TWO blocks that are
 * NEVER summed: treasury (Rob's cash flow) and runtime (the owner's compute
 * bill). The `no-sum` line sits exactly where a naive design would put a total.
 * `available === false` renders the figures `—` with the ledger's own reason.
 */
export function renderCash(root, ledger, copy, opts = {}) {
  root.replaceChildren();
  if (Object.prototype.hasOwnProperty.call(opts, "wallet")) {
    root.appendChild(walletSection(opts.wallet, copy));
  }
  if (!ledger || ledger.error) {
    root.appendChild(dashedEntry((ledger && ledger.error) || "",
      copy, "cash_unreadable", "cash_unreadable_why"));
    return "unreadable";
  }
  const t = ledger.treasury || {};
  const r = ledger.runtime || {};
  const days = ledger.window_days;
  const note = ledger.note || (copy && copy.cash_dash_why) || "";
  const tOff = t.available === false;
  const rOff = r.available === false;

  const books = el("div", "books");
  books.appendChild(cashBook("is-earned", copy && copy.cash_income_what,
    formatVars(copy && copy.cash_whose, { days }),
    tOff ? null : t.income_usd, note, [
      [copy && copy.cash_paid_invoices,
       document.createTextNode(String(ledger.settled_payments != null
         ? ledger.settled_payments : DASH))],
      [copy && copy.cash_waiting, moneyNode(tOff ? null : t.pending_usd, note)],
      [copy && copy.cash_balance_now, moneyNode(t.balance_usd, note)],
    ]));
  books.appendChild(cashBook("is-spent", copy && copy.cash_spent_what,
    formatVars(copy && copy.cash_whose, { days }),
    tOff ? null : t.spend_usd, note, [
      [copy && copy.cash_net, moneyNode(tOff ? null : t.net_usd, note)],
    ]));
  books.appendChild(el("p", "no-sum", (copy && copy.cash_no_sum) || ""));
  root.appendChild(books);

  // ⚠️ Money TAKEN for something never delivered. It is not income and it is
  // not a pending invoice, so it sits in neither book — and before this line
  // the console's Cash screen was the one seat that never mentioned it, while
  // the terminal ledger has always printed it. Drawn only when the ledger says
  // there IS one; a zero is not a debt, and a line that is always there stops
  // being read.
  const refundCount = Number(t.refund_due_count);
  if (Number.isFinite(refundCount) && refundCount > 0) {
    const owed = el("p", "entry-meta is-needs-you");
    owed.dataset.refundDue = String(refundCount);
    owed.textContent = formatVars(copy && copy.cash_refund_due, {
      // An unreadable amount is a dash, never `$0.00` — "we owe nothing back"
      // is exactly the wrong reading of a figure we could not total.
      amount: fmtUsd(t.refund_due_usd) || DASH, count: refundCount,
    });
    root.appendChild(owed);
  }

  const section = el("div", "section");
  section.style.marginTop = "var(--s6)";
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", (copy && copy.cash_runtime_title) || ""));
  head.appendChild(el("span", "section-aside", (copy && copy.cash_runtime_aside) || ""));
  section.appendChild(head);
  const table = el("table", "table");
  const tbody = el("tbody");
  const calls = (n) => (Number.isFinite(n) ? n : 0).toLocaleString("en-US");
  tbody.appendChild(runtimeRow(formatVars(copy && copy.cash_runtime_window, { days }),
    rOff ? null : r.spend_window_usd, note,
    formatVars(copy && copy.cash_runtime_calls, { count: calls(r.calls_window) })));
  tbody.appendChild(runtimeRow(copy && copy.cash_runtime_total,
    rOff ? null : r.spend_total_usd, note,
    formatVars(copy && copy.cash_runtime_calls, { count: calls(r.calls_total) })));
  tbody.appendChild(runtimeRow(copy && copy.cash_provider_left,
    r.provider_balance_usd, (copy && copy.cash_provider_why) || "", ""));
  table.appendChild(tbody);
  section.appendChild(table);
  root.appendChild(section);
  return "cash";
}

// --- Invoices: who owes Rob money, and the A20 machine-income gap ------------ #

/** The typed state pill for an invoice, from its status (and deadline for a
 *  pending one). A completed invoice is the only "paid" branch. */
export function invoiceState(inv, copy, nowMs) {
  const status = (inv && inv.status) || "";
  if (status === "completed") {
    return { word: (copy && copy.inv_state_completed) || status, cls: "is-running" };
  }
  if (status === "expired") {
    return { word: (copy && copy.inv_state_expired) || status, cls: "is-stopped" };
  }
  // pending — late once past the deadline (deadline is epoch seconds).
  const now = Number.isFinite(nowMs) ? nowMs : Date.now();
  const dl = Number(inv && inv.deadline);
  if (Number.isFinite(dl) && dl > 0 && now / 1000 > dl) {
    const days = Math.max(1, Math.floor((now / 1000 - dl) / 86400));
    return { word: fill(copy && copy.inv_late, "count", days), cls: "is-needs-you" };
  }
  return { word: (copy && copy.inv_state_pending) || status, cls: "is-needs-you" };
}

/** One invoice row: who, for what, how much, its state, and — on a writable
 *  console, for a pending row — Mark paid (owner attestation, not a payment). */
export function invoiceRow(inv, copy, nowMs, readOnly) {
  const tr = el("tr");
  tr.dataset.requestId = String((inv && inv.request_id) || "");
  tr.dataset.status = String((inv && inv.status) || "");
  const who = el("td", "what");
  who.dataset.label = (copy && copy.inv_col_who) || "";
  who.textContent = (inv && inv.payer_contact) || DASH;
  tr.appendChild(who);
  const forCell = el("td");
  forCell.dataset.label = (copy && copy.inv_col_for) || "";
  if (inv && inv.purpose) forCell.textContent = String(inv.purpose);
  else forCell.appendChild(el("span", "unknown", DASH));
  tr.appendChild(forCell);
  const amt = el("td", "num");
  amt.dataset.label = (copy && copy.inv_col_amount) || "";
  amt.textContent = fmtUsd(inv && inv.amount_usd) || DASH;
  tr.appendChild(amt);
  const stateTd = el("td");
  stateTd.dataset.label = (copy && copy.inv_col_state) || "";
  const st = invoiceState(inv, copy, nowMs);
  const pill = el("span", `pill ${st.cls}`);
  pill.appendChild(el("span", "dot"));
  pill.appendChild(document.createTextNode(st.word));
  stateTd.appendChild(pill);
  tr.appendChild(stateTd);
  const act = el("td");
  if (!readOnly && inv && inv.status === "pending") {
    const btn = el("button", "btn btn-quiet", (copy && copy.inv_settle) || "");
    btn.type = "button";
    btn.dataset.settle = String((inv && inv.request_id) || "");
    act.appendChild(btn);
  }
  tr.appendChild(act);
  return tr;
}

/** The A20 machine-income banner — stated on the page, never hidden. Per-request
 *  machine payments settle under the payer's name, so this page can undercount. */
export function machineIncomeBanner(copy) {
  const banner = el("div", "banner");
  banner.style.borderLeft = "3px dashed var(--unknown)";
  banner.style.marginTop = "var(--s4)";
  const g = el("span", null, DASH);
  g.setAttribute("aria-hidden", "true");
  banner.appendChild(g);
  const body = el("span");
  body.appendChild(el("b", null, (copy && copy.inv_machine_title) || ""));
  body.appendChild(document.createTextNode(" "));
  body.appendChild(document.createTextNode((copy && copy.inv_machine_body) || ""));
  banner.appendChild(body);
  return banner;
}

/** Draw Invoices. `data` is the /api/webgate/invoices body. A failed read is
 *  dashed with its reason; the machine-income banner always follows. */
export function renderInvoices(root, data, copy, opts = {}) {
  const nowMs = Number.isFinite(opts.nowMs) ? opts.nowMs : Date.now();
  const readOnly = Boolean(opts.readOnly);
  root.replaceChildren();
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", (copy && copy.inv_title) || ""));

  if (data && data.error) {
    section.appendChild(head);
    section.appendChild(dashedEntry(data.error, copy, "inv_unreadable", "inv_unreadable_why"));
    root.appendChild(section);
    root.appendChild(machineIncomeBanner(copy));
    return "unreadable";
  }

  const rows = (data && data.invoices) || [];
  // ⚠️ A35: this used to SUM the rows on screen — and the rows on screen are
  // one page (50). An owner with 51 outstanding invoices was shown a confident
  // total that was simply wrong, and it got wronger the more they were owed.
  // The total is the server's now, over every row; a total it could not
  // compute is a dash, never a number derived from a window.
  const serverTotal = data && data.outstanding_usd_total;
  const outstanding = (typeof serverTotal === "number" && Number.isFinite(serverTotal))
    ? fmtUsd(serverTotal) : DASH;
  head.appendChild(el("span", "section-aside",
    formatVars(copy && copy.inv_aside, { amount: outstanding || DASH })));
  section.appendChild(head);

  if (!rows.length) {
    section.appendChild(noticeEntry(copy && copy.inv_empty));
  } else {
    const table = el("table", "table");
    const thead = el("thead");
    const hrow = el("tr");
    [[copy && copy.inv_col_who, null], [copy && copy.inv_col_for, null],
     [copy && copy.inv_col_amount, "num"], [copy && copy.inv_col_state, null],
     ["", null]].forEach(([label, cls]) => hrow.appendChild(el("th", cls, label || "")));
    thead.appendChild(hrow);
    table.appendChild(thead);
    const tbody = el("tbody");
    rows.forEach((inv) => tbody.appendChild(invoiceRow(inv, copy, nowMs, readOnly)));
    table.appendChild(tbody);
    section.appendChild(table);
    // A35: the page is a WINDOW. Say so when the server says there is more,
    // rather than letting the list read as the whole book.
    if (data && data.truncated) {
      section.appendChild(el("p", "entry-meta", (copy && copy.inv_truncated) || ""));
    }
    // ⚠️ One unreadable amount withdraws the TOTAL: the server answers
    // `outstanding_usd_total: null` rather than a sum with a row quietly
    // dropped out of it, so the aside above is already a dash. This line says
    // HOW MANY rows caused that — "I cannot total this" is only useful beside
    // the reason it cannot.
    const unpriced = Number(data && data.outstanding_unpriced);
    if (Number.isFinite(unpriced) && unpriced > 0) {
      section.appendChild(el("p", "entry-meta",
        formatVars(copy && copy.inv_unpriced, { count: unpriced })));
    }
    section.appendChild(el("p", "entry-meta", (copy && copy.inv_note) || ""));
  }
  root.appendChild(section);
  root.appendChild(machineIncomeBanner(copy));
  return "invoices";
}

// --- Limits: today's cap, and the link to where they are set ---------------- #

/**
 * Draw Limits. The caps LIVE in Agent — this shows the day's used/limit from the
 * SAME PolicyGate read the ledger already computed and links to Agent to change
 * them. An unreadable cap is `—` with its reason; a disabled cap says so.
 */
export function renderLimits(root, ledger, copy) {
  root.replaceChildren();
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", (copy && copy.lim_title) || ""));
  section.appendChild(head);

  const ledgerBody = el("div", "ledger");
  const entry = el("div", "entry");
  let answer = "limits";
  if (!ledger || ledger.error) {
    answer = "unreadable";
    ledgerBody.appendChild(dashedEntry((ledger && ledger.error) || "",
      copy, "lim_unknown", "lim_unknown_why"));
  } else {
    const caps = ledger.caps || {};
    const cap = caps.daily_cap_usd != null ? caps.daily_cap_usd
      : caps.wallet_daily_cap_usd;
    const used = caps.daily_used_usd;
    const left = caps.daily_left_usd;
    if (caps.wallet_daily_cap_state === "disabled") {
      entry.appendChild(el("p", "entry-body", (copy && copy.lim_no_cap) || ""));
    } else if (cap == null) {
      entry.appendChild(el("p", "entry-body", (copy && copy.lim_unknown) || ""));
    } else {
      entry.appendChild(el("p", "entry-body", formatVars(copy && copy.lim_used, {
        used: fmtUsd(used) || DASH, cap: fmtUsd(cap) || DASH })));
      if (left != null) {
        entry.appendChild(el("p", "entry-meta",
          formatVars(copy && copy.lim_left, { left: fmtUsd(left) || DASH })));
      }
    }
    ledgerBody.appendChild(entry);
  }
  section.appendChild(ledgerBody);

  const link = el("div", "ledger");
  const linkEntry = el("div", "entry");
  linkEntry.appendChild(el("p", "entry-body", (copy && copy.lim_body) || ""));
  const actions = el("div", "entry-actions");
  const a = el("a", "btn btn-primary", (copy && copy.lim_link) || "");
  a.href = "/agent";
  actions.appendChild(a);
  linkEntry.appendChild(actions);
  link.appendChild(linkEntry);
  section.appendChild(link);
  root.appendChild(section);
  return answer;
}

// --- reads ------------------------------------------------------------------ #
// Each tab has its own reader, so one failing store never blanks another. A
// non-200 is shaped `{error}` here; a reader's own honest body (unreadable /
// unavailable / available:false) is respected by the renderer above.

async function getJson(url, fetcher) {
  const call = fetcher || fetch;
  const resp = await call(url, { credentials: "include" });
  if (!resp.ok) return { error: String(resp.status) };
  return await resp.json();
}

export const loadBridges = (f) => getJson("/api/webgate/bridges", f);
export const loadMovesFeed = (f) => getJson("/api/webgate/moves", f);
export const loadCreations = (f) => getJson("/api/webgate/creations", f);
export const loadLedger = (f) => getJson("/api/webgate/ledger", f);
export const loadWallet = (f) => getJson("/api/webgate/wallet", f);
export const loadInvoices = (f) => getJson("/api/webgate/invoices", f);

/** Attest an invoice as paid via the existing owner route, over http.js's
 *  same-origin POST. Returns `{ok, message}` — the caller SAYS it on the row,
 *  and a decision retires only on success. This never decides anything itself. */
export async function settleInvoice(requestId, copy, opts = {}) {
  try {
    const res = await postJson(
      `/api/webgate/invoices/${encodeURIComponent(requestId)}/settle`, {},
      { fetcher: opts.fetcher });
    const ok = Boolean(res.ok && res.body && res.body.ok);
    const message = (res.body && res.body.message)
      || (ok ? (copy && copy.inv_settle_done) : (copy && copy.inv_settle_failed)) || "";
    return { ok, message };
  } catch (err) {
    console.error("[money] settle did not reach the console", err);
    return { ok: false, message: (copy && copy.inv_settle_failed) || "" };
  }
}

// --- tab switching ---------------------------------------------------------- #
// Book/Moves/Cash/Invoices/Limits are panes on one page. Each non-Book tab
// loads its reader ONCE, the first time it is shown, so a page open never fires
// the ledger's balance probe (a network read) for a tab nobody opened.

function selectTab(name, onShow) {
  document.querySelectorAll("[data-pane]").forEach((pane) => {
    pane.hidden = pane.dataset.pane !== name;
  });
  document.querySelectorAll("a[data-tab]").forEach((tab) => {
    if (tab.dataset.tab === name) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  });
  if (typeof onShow === "function") onShow(name);
}

function bindTabs(onShow) {
  const tabs = document.querySelectorAll("a[data-tab]");
  if (!tabs.length) return;
  tabs.forEach((tab) => {
    tab.addEventListener("click", (ev) => {
      ev.preventDefault();
      try { history.replaceState(null, "", `#${tab.dataset.tab}`); }
      catch (err) { /* ignore */ }
      selectTab(tab.dataset.tab, onShow);
    });
  });
}

function bind() {
  const copyNode = document.getElementById("money-copy");
  if (!copyNode) return;
  const copy = copyFrom(copyNode);
  const readOnly = copy.read_only === "1";

  // --- Book (eager: the verdict leads) ---
  const bookPane = byId("money-book");
  const bookState = byId("money-book-state");
  function drawBook(data) {
    if (bookState) bookState.hidden = true;
    if (bookPane) render(bookPane, data, copy, { onRecheck: reloadBook });
  }
  function reloadBook() {
    if (!bookPane) return;
    if (bookState) { bookState.textContent = copy.loading || ""; bookState.hidden = false; }
    load()
      .then(drawBook)
      .catch((err) => {
        console.error("[money] could not read the book", err);
        drawBook({ unreadable: (err && String(err.message)) || "error" });
      });
  }

  // The same snapshot section rendered by CLI/chat. Network reads are explicit.
  const liquidityPane = byId("money-liquidity");
  function reloadLiquidity(onchain = false) {
    if (!liquidityPane) return;
    getJson("/api/webgate/liquidity" + (onchain ? "?onchain=true" : ""))
      .then((section) => {
        // ⚠️ A4: `getJson` answers `{error: "<status>"}` on ANY non-200, and
        // that object was rendered as though it were a Section — no `name`, no
        // `lines`, no `state`. The pane drew an empty heading and nothing
        // else, so a refused or failed liquidity read looked exactly like a
        // book with no positions in it. Branch on `error` FIRST.
        if (!section || section.error) {
          liquidityPane.replaceChildren(dashedEntry(
            (section && section.error) || "", copy,
            "liq_unreadable", "liq_unreadable_why"));
          return;
        }
        liquidityPane.replaceChildren(el("h2", "section-title", section.name));
        const lines = section.state === "unavailable" ? [section.reason] : section.lines;
        for (const line of lines || []) liquidityPane.appendChild(el("p", "state-body", line));
        for (const position of (section.data && section.data.held) || []) {
          liquidityPane.appendChild(el("p", "state-body",
            `${position.chain} #${position.token_id}: ${position.token0} / ${position.token1}; ` +
            `[${position.tick_lower}, ${position.tick_upper}]`));
        }
      })
      .catch((err) => {
        liquidityPane.replaceChildren(dashedEntry(String(err.message || err), copy,
          "liq_unreadable", "liq_unreadable_why"));
      });
  }
  const liquidityRecheck = byId("money-liquidity-recheck");
  if (liquidityRecheck) liquidityRecheck.addEventListener("click", () => reloadLiquidity(true));
  reloadLiquidity();

  // The ledger backs BOTH Cash and Limits — read it once and share the promise
  // (the reader fires a network balance probe, so a second call would double it).
  let ledgerPromise = null;
  const getLedger = () => (ledgerPromise || (ledgerPromise = loadLedger()));

  function withState(paneId, stateId, msg, run) {
    const pane = byId(paneId);
    const state = byId(stateId);
    if (!pane) return;
    if (state) { state.textContent = msg || ""; state.hidden = false; }
    run(pane, () => { if (state) state.hidden = true; });
  }

  function drawMoves() {
    withState("money-moves", "money-moves-state", copy.mv_loading, (pane, done) => {
      Promise.all([loadBridges(), loadMovesFeed(), loadCreations()])
        .then(([b, m, c]) => { done(); renderMoves(pane, b, m, c, copy); })
        .catch((err) => {
          console.error("[money] could not read Moves", err);
          done();
          renderMoves(pane, { error: String(err && err.message) },
            { error: String(err && err.message) },
            { error: String(err && err.message) }, copy);
        });
    });
  }

  function drawCash() {
    withState("money-cash", "money-cash-state", copy.cash_loading, (pane, done) => {
      Promise.all([getLedger(), loadWallet()])
        .then(([led, wallet]) => { done(); renderCash(pane, led, copy, { wallet }); })
        .catch((err) => { done(); renderCash(pane,
          { error: String(err && err.message) }, copy,
          { wallet: { error: String(err && err.message) } }); });
    });
  }

  function drawInvoices() {
    withState("money-invoices", "money-invoices-state", copy.inv_loading, (pane, done) => {
      loadInvoices()
        .then((data) => { done(); renderInvoices(pane, data, copy, { readOnly }); })
        .catch((err) => { done(); renderInvoices(pane, { error: String(err && err.message) }, copy, { readOnly }); });
    });
  }

  function drawLimits() {
    withState("money-limits", "money-limits-state", copy.lim_loading, (pane, done) => {
      getLedger()
        .then((led) => { done(); renderLimits(pane, led, copy); })
        .catch((err) => { done(); renderLimits(pane, { error: String(err && err.message) }, copy); });
    });
  }

  const fired = {};
  const lazy = { moves: drawMoves, cash: drawCash, invoices: drawInvoices, limits: drawLimits };
  function ensure(name) {
    const fn = lazy[name];
    if (!fn || fired[name]) return;
    fired[name] = true;
    fn();
  }

  // Settle is a POST to the existing owner-attestation route; the row says back
  // exactly what the server answered, and a decision retires only on success.
  const invPane = byId("money-invoices");
  if (invPane) {
    invPane.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button[data-settle]");
      if (!btn) return;
      btn.disabled = true;
      const tr = btn.closest("tr");
      const { ok, message } = await settleInvoice(btn.dataset.settle, copy);
      if (tr) {
        let answer = tr.querySelector(".settle-answer");
        const cell = tr.lastElementChild;
        if (cell) {
          if (!answer) {
            answer = document.createElement("span");
            answer.className = "why settle-answer";
            cell.appendChild(answer);
          }
          answer.textContent = message;
        }
      }
      if (ok) btn.remove();
      else btn.disabled = false;
    });
  }

  bindTabs(ensure);
  reloadBook();

  // Honour an initial hash pointing at a non-Book tab (a bookmarked #cash).
  const initial = (location.hash || "").replace(/^#/, "");
  if (initial && initial !== "book" && lazy[initial]) selectTab(initial, ensure);
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
}
