/**
 * workpane.js — the Work pane beside the chat (043 §3): Files and Timeline.
 *
 * The pane the chat has never had: what this chat MADE, beside what it DID. It
 * sits beside the thread on a bound session (a disclosure, not a nav slot) and
 * reads three existing sources, changing nothing:
 *
 *   Files    = GET /api/webgate/artifacts  (the ledger: what THIS chat wrote)
 *            + GET /api/session/{id}/workspace/tree?path=inbound  (what you gave)
 *              in three tiers: READY for you (page/report/data, with a verdict),
 *              Rob's WORKING files (code/file, newest 8 + Show all), and FROM
 *              YOU. The folder behind the chat — on a server the ONE shared
 *              project folder — is one link with its count (070 W0.15).
 *   Timeline = GET /api/session/{id}/feed/events, one narrated line per action,
 *              through the SAME narrator the transcript uses — never a second
 *              phrasing, never a machine name.
 *
 * Four rules it keeps:
 *
 * 1. **One narrator, and it is not here.** A Timeline line is
 *    `transcript.js::narrationLine` over the feed event — the server-computed
 *    `data.narration` first, the scrubbed `result_preview` next, a name-free
 *    copy line last. This module never re-implements the narrator.
 * 2. **It says nothing it did not read.** An empty folder is the empty state,
 *    never a blank; a folder it could not read is named, never a confident
 *    empty list. A deliverable's verdict is the ledger's typed value
 *    (ok / changed / missing / unknown), never a guess.
 * 3. **Read-only.** Every call here is a GET. The pane shows what exists; it
 *    sends, publishes and deletes nothing — those are the chat's own verbs,
 *    owner-approved, and they do not live in a monitor.
 * 4. **Every word comes from the copy layer**, handed over on `#workpane-copy`'s
 *    data attributes — the way chats.js and worklog.js read theirs, because no
 *    043 template carries an inline script.
 */
import { narrationLine, mmss, isControllerVerb } from './transcript.js';

/** The feed event types that ARE an action, matching the transcript's set. */
const TOOL_TYPES = new Set(['tool_result', 'tool_execution', 'tool_started']);

/** The copy the server handed over on a data-node, as a plain object. */
export function copyFrom(node) {
  return node ? { ...node.dataset } : {};
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** Fill a `{count}` template. Copy owns the words; this owns only the number. */
function fill(template, count) {
  return String(template || '').replace('{count}', String(count));
}

/** The Timeline's count: "1 step", "{count} steps" (070 E.13), or the dash
 *  for a read that failed. Exported for tests. */
export function timelineCount(n, copy) {
  if (n === 1 && copy && copy.timeline_count_one) return copy.timeline_count_one;
  return fill(copy && copy.timeline_count, n);
}

/** A feed timestamp in seconds, whether it arrived as seconds or milliseconds. */
export function toSeconds(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n)) return NaN;
  return n > 1e12 ? n / 1000 : n;
}

/** The plain word for a ledger verdict, from copy. An unknown verdict shows its
 *  own id rather than a guess — the ledger returns only the four. */
export function verdictLabel(verdict, copy) {
  const key = `verdict_${String(verdict || '').trim()}`;
  return (copy && copy[key]) || String(verdict || '');
}

/** The ledger kinds that are a deliverable ("Ready for you"); every other kind
 *  (code, file, …) is one of Rob's working files. */
const READY_KINDS = new Set(['page', 'report', 'data']);

/** How many rows a long list shows before "Show all {count}". */
export const SHOW_FIRST = 8;

/**
 * The three tiers of Files (070 W0.15). THIS chat's files come from the
 * artifact ledger only — the folder tree is no longer a source of them:
 *   - `ready`   — ledger rows of kind page / report / data, with their verdict;
 *   - `working` — every other ledger row (code, file), newest first;
 *   - `given`   — the `inbound/` folder: what you handed Rob.
 * `artifacts` is the ledger's rows (`name`, `path` relative to this chat's
 * workspace or null, `kind`, `verdict`); `inbound` is the `?path=inbound` tree.
 */
export function buildFiles(artifacts, inbound) {
  const arts = Array.isArray(artifacts) ? artifacts : [];
  const row = (a) => ({
    name: String((a && (a.name || a.path)) || '').split('/').pop(),
    path: (a && a.path) || null,
    kind: a && a.kind,
    verdict: a && a.verdict,
    url: a && a.url,
    id: a && a.id,
  });
  const ready = arts.filter((a) => a && READY_KINDS.has(a.kind)).map(row);
  const working = arts.filter((a) => a && !READY_KINDS.has(a.kind)).map(row).reverse();
  const given = ((inbound && inbound.children) || [])
    .filter((c) => c && c.name && c.type !== 'dir')
    .map((c) => ({ name: c.name, path: `inbound/${c.name}` }));
  return { ready, working, given };
}

/**
 * The read URL for one workspace-relative path, or `''` when either half is
 * missing — an anchor with no target is a link that looks live and is not.
 */
export function fileHref(sessionId, path) {
  if (!sessionId || !path) return '';
  return `/api/session/${encodeURIComponent(sessionId)}/workspace/file`
    + `?path=${encodeURIComponent(path)}`;
}

/** Fill named `{placeholders}` from *values*. Copy owns the words. */
function format(template, values) {
  return String(template || '').replace(/\{(\w+)\}/g, (m, k) => (k in values ? String(values[k]) : m));
}

/** The words of the folder link: the shared folder or this chat's own. */
export function folderLinkLabel(folder, copy) {
  if (!folder) return '';
  const count = Number(folder.total) || 0;
  if (!folder.shared) return format(copy && copy.folder_link, { count });
  return format(copy && (folder.truncated ? copy.shared_link_more : copy.shared_link), { count });
}
/**
 * One narrated line per action for the Timeline. Deduped by call id (running
 * then done is ONE line, its terminal state), narrated through the transcript's
 * own narrator, timed against the first action's clock (`0:06` / `1:12`).
 */
export function timelineLines(events, copy, nowMs) {
  // The controller verbs (send_message, done) are never a step — the Timeline
  // and the thread count the same steps.
  // One event, one line: an event the fast push and the watcher both carried
  // has one `_id` (070 W0.9); then one line per call id.
  const ids = new Set();
  const tools = (events || []).filter((e) => {
    if (!e || !TOOL_TYPES.has(e.type) || isControllerVerb(e.data)) return false;
    if (e._id) {
      if (ids.has(e._id)) return false;
      ids.add(e._id);
    }
    return true;
  });
  const order = [];
  const byKey = new Map();
  let synthetic = 0;
  for (const e of tools) {
    const data = e.data || {};
    const key = data.call_id != null ? String(data.call_id) : `#${synthetic++}`;
    if (!byKey.has(key)) order.push(key);
    byKey.set(key, e); // last wins → the terminal (done/failed) state
  }
  const chosen = order.map((k) => byKey.get(k));
  const secs = chosen.map((e) => toSeconds(e.timestamp)).filter(Number.isFinite);
  const base = secs.length ? Math.min(...secs) : 0;
  return chosen.map((e) => {
    const data = e.data || {};
    const line = narrationLine(data, copy);
    const t = toSeconds(e.timestamp);
    const time = Number.isFinite(t) ? mmss(Math.max(0, t - base)) : '';
    const success = data.success;
    const state = success === false ? 'stopped' : success === true ? 'done' : 'running';
    return { line, time, state };
  });
}

/** The dashed entry a read failure renders — the honest answer that is neither
 *  "empty" nor a confident list. `sentence` is copy; nothing machine here. */
export function unreadableEntry(sentence) {
  const entry = el('div', 'entry is-unknown');
  entry.appendChild(el('h2', 'entry-title', sentence || ''));
  return entry;
}

/** One Files row: the value (name), and — for a deliverable — its verdict in
 *  the `.why` slot. With an `href` the name is a LINK to the file's own read
 *  route (A23); without one it stays plain text, never a dead anchor. Returns
 *  a DOM node; nothing here builds HTML from a string. */
function fileRow(name, why, verdict, href, path) {
  const row = el('tr');
  if (verdict) row.dataset.verdict = String(verdict);
  const cell = el('td', 'what');
  cell.dataset.label = '';
  if (path) cell.title = String(path);
  if (href) {
    const link = el('a', 'val', name);
    link.href = href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    cell.appendChild(link);
  } else {
    cell.appendChild(el('span', 'val', name));
  }
  if (why) cell.appendChild(el('span', 'why', why));
  row.appendChild(cell);
  row.appendChild(el('td'));
  return row;
}

/**
 * Render Files into *root*, distinguishing the answers:
 *   - both sources unreadable → a dashed entry with the honest sentence;
 *   - the ledger unreadable   → the named failure and the Given tier only;
 *   - no files in any tier     → the empty state;
 *   - files                    → the tiered table (Working capped at 8).
 * `data` is `{artifacts, inbound, folder, artifactsUnreadable, inboundUnreadable,
 * sessionId, fetchTree}`; `folder` is the `?depth=1` tree, drawn as ONE link
 * ("Browse the shared folder (50)"). Returns which answer it drew.
 */
export function renderFiles(root, state, data, copy) {
  root.replaceChildren();
  const artifacts = data && data.artifacts;
  const sessionId = (data && data.sessionId) || '';
  const hrefFor = (path) => fileHref(sessionId, path);
  const artsUnreadable = Boolean(data && (data.artifactsUnreadable || artifacts === null));
  const inboundUnreadable = Boolean(data && data.inboundUnreadable);

  const table = el('table', 'table');
  const tbody = el('tbody');
  const tier = (labelKey, items, rowFor, cap) => {
    if (!items.length) return;
    const head = el('tr');
    const headCell = el('td', 'what');
    headCell.dataset.label = '';
    headCell.appendChild(document.createTextNode((copy && copy[labelKey]) || ''));
    head.appendChild(headCell);
    head.appendChild(el('td'));
    tbody.appendChild(head);
    const shown = cap ? items.slice(0, cap) : items;
    shown.forEach((it) => tbody.appendChild(rowFor(it)));
    if (cap && items.length > cap) {
      const more = el('tr');
      const cell = el('td', 'what');
      const btn = el('button', 'btn btn-quiet', format(copy && copy.show_all, { count: items.length }));
      btn.type = 'button';
      btn.addEventListener('click', () => {
        items.slice(cap).forEach((it) => tbody.insertBefore(rowFor(it), more));
        more.remove();
      });
      cell.appendChild(btn);
      more.appendChild(cell);
      more.appendChild(el('td'));
      tbody.appendChild(more);
    }
  };
  const flush = () => {
    if (tbody.children.length) {
      table.appendChild(tbody);
      root.appendChild(table);
    }
  };
  const readyRow = (a) => fileRow(a.name, verdictLabel(a.verdict, copy), a.verdict, hrefFor(a.path), a.path);
  const plainRow = (f) => fileRow(f.name, '', null, hrefFor(f.path), f.path);

  if (artsUnreadable && inboundUnreadable) {
    root.appendChild(unreadableEntry(copy && copy.files_unreadable));
    if (state) state.hidden = true;
    return 'unreadable';
  }
  const built = buildFiles(artsUnreadable ? [] : artifacts, data && data.inbound);
  let answer;
  if (artsUnreadable) {
    // Without the ledger this chat's own files are unknown: name the failure,
    // and show only what you gave it (that needs no ledger).
    root.appendChild(unreadableEntry(copy && copy.files_ledger_unreadable));
    tier('tier_given', built.given, plainRow);
    flush();
    if (state) state.hidden = true;
    answer = 'partial';
  } else {
    if (inboundUnreadable) root.appendChild(unreadableEntry(copy && copy.files_tree_unreadable));
    const total = built.ready.length + built.working.length + built.given.length;
    if (!total && !inboundUnreadable) {
      if (state) {
        state.textContent = (copy && copy.files_empty) || '';
        state.hidden = false;
      }
      answer = 'empty';
    } else {
      if (state) state.hidden = true;
      tier('tier_ready', built.ready, readyRow);
      tier('tier_working', built.working, plainRow, SHOW_FIRST);
      tier('tier_given', built.given, plainRow);
      flush();
      answer = inboundUnreadable ? 'partial' : 'rows';
    }
  }
  folderLink(root, data, copy);
  return answer;
}

/** The ONE link to the folder behind this chat (the shared project folder on a
 *  server). A click lists it, capped, newest first; a folder opens on click. */
function folderLink(root, data, copy) {
  const folder = data && data.folder;
  if (!folder || folder.error || !Array.isArray(folder.children)) return;
  // This chat's own folder with nothing in it is no link at all.
  if (!folder.shared && !(Number(folder.total) > 0)) return;
  const wrap = el('div', 'folder-browse');
  const btn = el('button', 'btn btn-quiet folder-link', folderLinkLabel(folder, copy));
  btn.type = 'button';
  btn.setAttribute('aria-expanded', 'false');
  const list = el('div', 'folder-list');
  list.hidden = true;
  btn.addEventListener('click', async () => {
    const open = list.hidden;
    list.hidden = !open;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && !list.dataset.loaded) {
      list.dataset.loaded = '1';
      await drawFolder(list, '', data, copy, folder.shared);
    }
  });
  wrap.appendChild(btn);
  wrap.appendChild(list);
  root.appendChild(wrap);
}

/** List one folder level into *list*, capped at {@link SHOW_FIRST}. */
export async function drawFolder(list, path, data, copy, shared) {
  const fetchTree = data && data.fetchTree;
  const tree = fetchTree ? await fetchTree(path) : null;
  list.replaceChildren();
  if (!path) {
    if (shared) {
      list.appendChild(el('h3', 'section-title', (copy && copy.shared_title) || ''));
      list.appendChild(el('p', 'entry-meta', (copy && copy.shared_note) || ''));
    }
  }
  if (!tree || tree.error || !Array.isArray(tree.children)) {
    list.appendChild(unreadableEntry(copy && copy.files_unreadable));
    return 'unreadable';
  }
  const table = el('table', 'table');
  const tbody = el('tbody');
  const sessionId = (data && data.sessionId) || '';
  const rowFor = (child) => {
    const rel = path ? `${path}/${child.name}` : child.name;
    if (child.type !== 'dir') return fileRow(child.name, '', null, fileHref(sessionId, rel), rel);
    const row = el('tr');
    const cell = el('td', 'what');
    const open = el('button', 'btn btn-quiet val', `${child.name}/`);
    open.type = 'button';
    open.title = rel;
    const inner = el('div', 'folder-list');
    open.addEventListener('click', () => {
      if (inner.dataset.loaded) { inner.hidden = !inner.hidden; return; }
      inner.dataset.loaded = '1';
      drawFolder(inner, rel, data, copy, shared);
    });
    cell.appendChild(open);
    cell.appendChild(inner);
    row.appendChild(cell);
    row.appendChild(el('td'));
    return row;
  };
  const items = tree.children;
  items.slice(0, SHOW_FIRST).forEach((c) => tbody.appendChild(rowFor(c)));
  if (items.length > SHOW_FIRST) {
    const more = el('tr');
    const cell = el('td', 'what');
    const btn = el('button', 'btn btn-quiet', format(copy && copy.show_all, { count: items.length }));
    btn.type = 'button';
    btn.addEventListener('click', () => {
      items.slice(SHOW_FIRST).forEach((c) => tbody.insertBefore(rowFor(c), more));
      more.remove();
    });
    cell.appendChild(btn);
    more.appendChild(cell);
    more.appendChild(el('td'));
    tbody.appendChild(more);
  }
  table.appendChild(tbody);
  list.appendChild(table);
  return 'rows';
}

/**
 * Render the Timeline into *root*, distinguishing the same three answers:
 * `events === null` is a read that failed; no action events is a genuine empty
 * timeline; otherwise the narrated `.act` rows, one line per action.
 */
export function renderTimeline(root, state, events, copy, opts = {}) {
  root.replaceChildren();
  if (events === null) {
    root.appendChild(unreadableEntry(copy && copy.timeline_unreadable));
    if (state) state.hidden = true;
    return 'unreadable';
  }
  const lines = timelineLines(events, copy, opts.nowMs);
  if (!lines.length) {
    if (state) {
      state.textContent = (copy && copy.timeline_empty) || '';
      state.hidden = false;
    }
    return 'empty';
  }
  if (state) state.hidden = true;
  lines.forEach(({ line, time, state: st }) => {
    const cls = st === 'running' ? 'act is-running'
      : st === 'stopped' ? 'act is-stopped'
        : 'act';
    const node = el('div', cls);
    node.appendChild(el('span', 'act-what', line));
    node.appendChild(el('span', 'act-time val', time));
    root.appendChild(node);
  });
  return 'rows';
}

// --------------------------------------------------------------------- wiring
// The live binding: two GETs for Files, one for the Timeline. It is deliberately
// thin and is exercised by a running console, not the unit rig — the rig drives
// the pure functions above (renderFiles / renderTimeline / buildFiles /
// timelineLines), which is where the logic is.

const ACTIVITY_DEBOUNCE_MS = 5000;

async function getJson(url) {
  try {
    const resp = await fetch(url, { credentials: 'include' });
    if (!resp.ok) return null; // an HTTP refusal reads as unreadable, not empty
    return await resp.json();
  } catch (err) {
    return null;
  }
}

async function loadFiles(sessionId, filesRoot, stateNode, copy) {
  const id = encodeURIComponent(sessionId);
  const tree = (path, depth = 1) => getJson(
    `/api/session/${id}/workspace/tree?depth=${depth}${path ? `&path=${encodeURIComponent(path)}` : ''}`);
  // THIS chat's files are the ledger's rows; the tree feeds only "From you"
  // (inbound/) and the one folder link with its count.
  const [arts, inbound, folder] = await Promise.all([
    getJson(`/api/webgate/artifacts?session_id=${id}`),
    tree('inbound'),
    tree(''),
  ]);
  renderFiles(filesRoot, stateNode, {
    artifacts: arts ? arts.artifacts : null,
    artifactsUnreadable: arts === null || Boolean(arts && arts.error),
    inbound,
    inboundUnreadable: inbound === null || Boolean(inbound && inbound.error),
    folder,
    sessionId,
    fetchTree: (path) => tree(path),
  }, copy);
}

async function loadTimeline(sessionId, tlRoot, stateNode, asideNode, copy) {
  const id = encodeURIComponent(sessionId);
  const data = await getJson(`/api/session/${id}/feed/events?limit=300`);
  const events = data && Array.isArray(data.events) ? data.events : (data === null ? null : []);
  renderTimeline(tlRoot, stateNode, events, copy);
  if (asideNode) {
    const n = events === null ? '—' : timelineLines(events, copy).length;
    asideNode.textContent = timelineCount(n, copy);
  }
}

/** Below 900 px the pane is a drawer: the toggle opens and closes it, and
 *  Escape closes it (070 W0.16). Exported for tests. */
export function bindToggle(toggle, pane, doc = document) {
  if (!toggle || !pane) return;
  const set = (open) => {
    pane.classList.toggle('is-open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  toggle.addEventListener('click', () => set(!pane.classList.contains('is-open')));
  doc.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && pane.classList.contains('is-open')) {
      set(false);
      toggle.focus();
    }
  });
}

export function mount() {
  const filesRoot = document.getElementById('workpane-files');
  const tlRoot = document.getElementById('workpane-timeline');
  if (!filesRoot && !tlRoot) return null; // not a bound session — nothing to draw
  bindToggle(document.getElementById('work-pane-toggle'), document.getElementById('work-pane'));
  const body = document.body;
  const sessionId = body && body.dataset ? body.dataset.sessionId : '';
  if (!sessionId || sessionId === 'new') return null;

  // The narrator's fallback keys ride on the transcript-copy node (chat.act.*),
  // so the Timeline says the same name-free line the thread does.
  const copy = {
    ...copyFrom(document.getElementById('transcript-copy')),
    ...copyFrom(document.getElementById('workpane-copy')),
  };

  let pending = false;
  async function refresh() {
    if (pending) return;
    pending = true;
    try {
      await Promise.all([
        filesRoot && loadFiles(sessionId, filesRoot, document.getElementById('workpane-files-state'), copy),
        tlRoot && loadTimeline(sessionId, tlRoot, document.getElementById('workpane-timeline-state'), document.getElementById('workpane-timeline-aside'), copy),
      ]);
    } finally { pending = false; }
  }
  // 070 W0.14: re-read on THIS session's activity (debounced), and on the
  // fallback tick, which live.js fires only while the socket is down.
  let soon = null;
  document.addEventListener('polyrob:activity', (event) => {
    const detail = event && event.detail;
    if (!detail || detail.session_id !== sessionId || soon) return;
    soon = setTimeout(() => { soon = null; refresh(); }, ACTIVITY_DEBOUNCE_MS);
  });
  document.addEventListener('polyrob:tick', refresh);
  if (filesRoot) {
    const state = document.getElementById('workpane-files-state');
    if (state) { state.textContent = copy.loading || ''; state.hidden = false; }

  }
  if (tlRoot) {
    const state = document.getElementById('workpane-timeline-state');
    if (state) { state.textContent = copy.loading || ''; state.hidden = false; }

  }
  refresh();
  return true;
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
}
