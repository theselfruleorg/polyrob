/**
 * work-apps.js — Work › Apps: everything Rob built that you can open (043 WS-P2).
 *
 * The owner's ask was one place where everything Rob has built is reachable.
 * This draws it as THREE tiers, by reach:
 *
 *   1. **Online** — durable app services (the `app_services` registry), each
 *      with its state; a live app with a public URL links out.
 *   2. **Published** — artifact ledger rows that carry a published URL.
 *   3. **Made, but not shared** — artifact ledger rows with no URL: files that
 *      only exist in the folder of the chat that made them.
 *
 * Four rules it keeps, the same ones work-now.js keeps:
 *
 * 1. **Every word comes from the copy layer.** No 043 template carries an inline
 *    script (tests/unit/webview/test_no_new_inline_script.py), so the words ride
 *    on `#work-apps-copy`'s data attributes and this reads them off `dataset`.
 * 2. **An unreadable store is dashed with its reason, never a silent drop and
 *    never a confident empty list.** A reader answers with an `error` field on a
 *    failed read; that becomes the dashed entry.
 * 3. **"Nothing built" is only claimed when every store answered and every one
 *    was empty.** If one store errored, the empty state is NOT shown — the page
 *    names the store that did not answer instead.
 * 4. **It decides nothing.** Approve / kill live in the Inbox (a pending app IS
 *    the owner ask); this tab is reach, and links out to a live app, never a
 *    money or deploy verb.
 *
 * ⚠️ The artifact ledger reader (`/api/webgate/artifacts`) is SESSION-scoped and
 * the whole-Work view names no session, so the Published and Made tiers read
 * empty here today; they fill on a per-chat view. The sources note says so.
 * Tabs are wired by work-now.js (the one Work subnav), so this only fills the
 * Apps pane.
 */

/** The copy the server handed over, as a plain object. */
export function copyFrom(node) {
  return node ? { ...node.dataset } : {};
}

/** Fill a `{count}` template. Copy owns the words; this owns only the number. */
function fill(template, count) {
  return String(template || "").replace("{count}", String(count));
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** A plain informational entry (empty state, loading). */
export function noticeEntry(text) {
  const entry = el("div", "entry");
  entry.appendChild(el("p", "entry-body", text || ""));
  return entry;
}

/** The dashed entry for a store that could not be read. `reason` is the machine
 *  detail behind a disclosure; the sentence a person reads is copy. */
export function unreadableEntry(reason, copy, titleKey, whyKey) {
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

/** A link OUT to a live app or a published page — a new tab, and it decides
 *  nothing (a report never deploys). */
export function linkOut(label, href) {
  const a = el("a", "btn btn-quiet", label || "");
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}

// --- Online: durable app services ------------------------------------------- #

//: The registry's statuses, mapped to the shared pill/entry classes. An
//: unknown status is neutral, never "online".
const STATUS_CLASS = {
  live: "is-running", deploying: "is-running", approved: "is-running",
  pending: "is-needs-you",
  unhealthy: "is-stopped", failed: "is-stopped",
  stopped: "is-stopped", paused: "is-stopped",
};
const STATUS_WORD = {
  live: "status_live", deploying: "status_deploying", approved: "status_approved",
  pending: "status_pending", unhealthy: "status_unhealthy",
  failed: "status_failed", stopped: "status_stopped", paused: "status_paused",
};

/** One app service: its name (linked when it has a public URL), its plain
 *  state, where it is, and an Open it for a reachable one. */
export function appEntry(app, copy) {
  const status = (app && app.status) || "";
  const cls = STATUS_CLASS[status];
  const entry = el("div", cls ? `entry ${cls}` : "entry");
  entry.dataset.slug = String((app && app.slug) || "");
  entry.dataset.status = status;

  const title = el("h3", "entry-title");
  if (app && app.public_url) {
    const a = el("a", null, app.public_url);
    a.href = app.public_url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    title.appendChild(a);
  } else {
    title.textContent = (app && app.slug) || "";
  }
  entry.appendChild(title);

  entry.appendChild(el("p", "entry-body",
    (copy && copy[STATUS_WORD[status]]) || status || ""));
  if (app && app.where) entry.appendChild(el("p", "entry-meta", String(app.where)));
  if (app && app.pending_reason) {
    entry.appendChild(el("p", "entry-meta", String(app.pending_reason)));
  }
  if (app && app.public_url) {
    const actions = el("div", "entry-actions");
    actions.appendChild(linkOut(copy && copy.online_open, app.public_url));
    entry.appendChild(actions);
  }
  return entry;
}

/** Online — the app_services registry. A failed read is dashed with its reason;
 *  an empty registry is a plain notice. */
export function onlineSection(appsData, copy) {
  const apps = (appsData && appsData.apps) || [];
  const failed = Boolean(appsData && appsData.error);
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", (copy && copy.online_title) || ""));
  if (!failed && apps.length) {
    head.appendChild(el("span", "section-aside", fill(copy && copy.online_aside, apps.length)));
  }
  section.appendChild(head);
  const ledger = el("div", "ledger");
  if (failed) {
    ledger.appendChild(unreadableEntry(appsData.error, copy,
      "online_unreadable", "online_unreadable_why"));
  } else if (!apps.length) {
    ledger.appendChild(noticeEntry(copy && copy.online_empty));
  } else {
    apps.forEach((a) => ledger.appendChild(appEntry(a, copy)));
  }
  section.appendChild(ledger);
  return section;
}

// --- Published + Made, but not shared (the artifact ledger) ----------------- #

/** Published — artifact rows that carry a URL. A failed read is dashed; an
 *  empty result is a plain notice (session-scoped, per the sources note). */
export function publishedSection(published, artifactsData, copy) {
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", (copy && copy.published_title) || ""));
  head.appendChild(el("span", "section-aside", (copy && copy.published_aside) || ""));
  section.appendChild(head);
  if (artifactsData && artifactsData.error) {
    section.appendChild(unreadableEntry(artifactsData.error, copy,
      "published_unreadable", "published_unreadable_why"));
    return section;
  }
  if (!published.length) {
    section.appendChild(noticeEntry(copy && copy.published_empty));
    return section;
  }
  const table = el("table", "table");
  const thead = el("thead");
  const hrow = el("tr");
  [copy && copy.col_what, copy && copy.col_link].forEach(
    (label) => hrow.appendChild(el("th", null, label || "")));
  hrow.appendChild(el("th"));
  thead.appendChild(hrow);
  table.appendChild(thead);
  const tbody = el("tbody");
  published.forEach((a) => {
    const tr = el("tr");
    const what = el("td", "what");
    what.dataset.label = (copy && copy.col_what) || "";
    what.appendChild(document.createTextNode((a && a.path) || "?"));
    if (a && a.kind) what.appendChild(el("span", "why", String(a.kind)));
    tr.appendChild(what);
    const link = el("td");
    link.dataset.label = (copy && copy.col_link) || "";
    const anchor = el("a", null, a.url);
    anchor.href = a.url;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    link.appendChild(anchor);
    tr.appendChild(link);
    const act = el("td");
    act.appendChild(linkOut(copy && copy.open_link, a.url));
    tr.appendChild(act);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  section.appendChild(table);
  return section;
}

/** Made, but not shared — artifact rows with no URL: files in a chat folder.
 *  A failed read is dashed; an empty result is a plain notice. */
export function madeSection(made, artifactsData, copy) {
  const section = el("div", "section");
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", (copy && copy.made_title) || ""));
  head.appendChild(el("span", "section-aside", (copy && copy.made_aside) || ""));
  section.appendChild(head);
  if (artifactsData && artifactsData.error) {
    section.appendChild(unreadableEntry(artifactsData.error, copy,
      "made_unreadable", "made_unreadable_why"));
    return section;
  }
  if (!made.length) {
    section.appendChild(noticeEntry(copy && copy.made_empty));
    return section;
  }
  const table = el("table", "table");
  const thead = el("thead");
  const hrow = el("tr");
  [copy && copy.col_file, copy && copy.col_kind].forEach(
    (label) => hrow.appendChild(el("th", null, label || "")));
  thead.appendChild(hrow);
  table.appendChild(thead);
  const tbody = el("tbody");
  made.forEach((a) => {
    const tr = el("tr");
    const file = el("td", "what");
    file.dataset.label = (copy && copy.col_file) || "";
    file.appendChild(el("span", "val", (a && a.path) || "?"));
    tr.appendChild(file);
    const kind = el("td");
    kind.dataset.label = (copy && copy.col_kind) || "";
    kind.textContent = (a && a.kind) || "";
    tr.appendChild(kind);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  section.appendChild(table);
  section.appendChild(el("p", "entry-meta", (copy && copy.made_note) || ""));
  return section;
}

// --- the empty state -------------------------------------------------------- #

/** Nothing built: the one true thing and the one action. Shown ONLY when every
 *  store answered and every store was empty. */
export function emptyState(copy) {
  const state = el("div", "state");
  const glyph = el("div", "state-glyph", "◇");
  glyph.setAttribute("aria-hidden", "true");
  state.appendChild(glyph);
  state.appendChild(el("h2", "state-title", (copy && copy.empty_title) || ""));
  state.appendChild(el("p", "state-body", (copy && copy.empty_body) || ""));
  const actions = el("div", "state-actions");
  const a = el("a", "btn btn-primary", (copy && copy.empty_action) || "");
  a.href = "/new";
  actions.appendChild(a);
  state.appendChild(actions);
  return state;
}

/**
 * Draw the Apps tab. `appsData` is the /api/webgate/apps body, `artifactsData`
 * the /api/webgate/artifacts body. Returns which answer it drew ("empty" |
 * "apps"), for tests. The empty state fires ONLY when every store answered and
 * every one was empty — a store that errored is named, never swept into "nothing
 * built".
 */
export function renderApps(root, appsData, artifactsData, copy) {
  appsData ||= { error: copy?.online_unreadable || "—" };
  artifactsData ||= { error: copy?.made_unreadable || "—" };
  root.replaceChildren();
  const apps = (appsData && appsData.apps) || [];
  const arts = (artifactsData && artifactsData.artifacts) || [];
  const appsFailed = !appsData || Boolean(appsData.error);
  const artsFailed = !artifactsData || Boolean(artifactsData.error);
  const published = arts.filter((a) => a && a.url);
  const made = arts.filter((a) => a && !a.url);

  if (!apps.length && !published.length && !made.length
      && !appsFailed && !artsFailed) {
    root.appendChild(emptyState(copy));
    return "empty";
  }
  root.appendChild(onlineSection(appsData, copy));
  root.appendChild(publishedSection(published, artifactsData, copy));
  root.appendChild(madeSection(made, artifactsData, copy));
  return "apps";
}

// --- reads ------------------------------------------------------------------ #

async function getJson(url, fetcher) {
  const call = fetcher || fetch;
  const resp = await call(url, { credentials: "include" });
  if (!resp.ok) return { error: String(resp.status) };
  return await resp.json();
}

export const loadApps = (f) => getJson("/api/webgate/apps", f);
export const loadArtifacts = (f) => getJson("/api/webgate/artifacts", f);

// --- wiring ----------------------------------------------------------------- #
// Tab switching is owned by work-now.js (the one Work subnav). This only fills
// the Apps pane; the reads are cheap (a registry row + a session-less ledger
// answer), so it loads eagerly rather than racing work-now.js for a show hook.

function bind() {
  const copyNode = document.getElementById("work-apps-copy");
  const pane = document.getElementById("work-apps");
  if (!copyNode || !pane) return;
  const copy = copyFrom(copyNode);
  const state = document.getElementById("work-apps-state");

  let pending = false;
  async function refresh() {
    if (pending) return;
    pending = true;
    try {
      const failed = err => ({ error: String(err.message || err) });
      const [apps, artifacts] = await Promise.all([
        loadApps().catch(failed), loadArtifacts().catch(failed),
      ]);
      if (state) state.hidden = true;
      renderApps(pane, apps, artifacts, copy);
    } finally { pending = false; }
  }
  if (state) { state.textContent = copy.loading || ""; state.hidden = false; }
  document.addEventListener('polyrob:tick', refresh);
  refresh();
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
}
