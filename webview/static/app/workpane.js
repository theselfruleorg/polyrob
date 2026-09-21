/**
 * workpane.js — the Work pane beside the chat (043 §3): Files and Timeline.
 *
 * The pane the chat has never had: what this chat MADE, beside what it DID. It
 * sits beside the thread on a bound session (a disclosure, not a nav slot) and
 * reads three existing sources, changing nothing:
 *
 *   Files    = GET /api/session/{id}/workspace/tree  (this chat's own folder)
 *            + GET /api/webgate/artifacts            (the typed ledger rows)
 *              merged into the three tiers the mockup draws
 *              (docs/design/040/web/chat-work-pane.html): what is READY for you
 *              (the ledger's deliverables, each with its verdict), Rob's own
 *              WORKING files, and what you GAVE it (the inbound/ folder).
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
import { narrationLine, mmss } from './transcript.js';

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

/** A feed timestamp in seconds, whether it arrived as seconds or milliseconds. */
export function toSeconds(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n)) return NaN;
  return n > 1e12 ? n / 1000 : n;
}

/**
 * Every FILE path in the workspace tree, relative to the workspace root, with
 * directories walked but never emitted as their own row. A missing/!readable
 * tree yields an empty list — the caller decides whether that is "empty" or
 * "unreadable" from the tree's own `error` flag.
 */
export function flattenTree(tree) {
  const out = [];
  const walk = (node, prefix) => {
    const children = (node && node.children) || [];
    for (const child of children) {
      if (!child || !child.name) continue;
      const rel = prefix ? `${prefix}/${child.name}` : child.name;
      if (child.type === 'dir') walk(child, rel);
      else out.push(rel);
    }
  };
  walk(tree, '');
  return out;
}

/** The plain word for a ledger verdict, from copy. An unknown verdict shows its
 *  own id rather than a guess — the ledger returns only the four. */
export function verdictLabel(verdict, copy) {
  const key = `verdict_${String(verdict || '').trim()}`;
  return (copy && copy[key]) || String(verdict || '');
}

/**
 * The three tiers of Files, merged from the workspace tree and the ledger:
 *   - `ready`   — the typed artifacts (deliverables), each with its verdict;
 *   - `working` — workspace files that are NOT a deliverable and NOT inbound;
 *   - `given`   — the `inbound/` folder, what you handed Rob.
 * A workspace file whose basename matches a ledger row is a deliverable and is
 * shown in `ready` only, never doubled into `working`.
 */
export function buildFiles(tree, artifacts) {
  const arts = Array.isArray(artifacts) ? artifacts : [];
  const artNames = new Set(
    arts.map((a) => String((a && a.path) || '').split('/').pop()).filter(Boolean),
  );
  const given = [];
  const working = [];
  for (const path of flattenTree(tree)) {
    const name = path.split('/').pop();
    if (path.startsWith('inbound/')) {
      given.push({ name, path });
    } else if (!artNames.has(name)) {
      working.push({ name, path });
    }
  }
  const ready = arts.map((a) => ({
    name: String((a && a.path) || '').split('/').pop() || String((a && a.path) || ''),
    // A23: the workspace-relative path, kept so a deliverable can be OPENED,
    // not only named. The ledger's `path` is what `/workspace/file?path=`
    // takes, so nothing here reconstructs one.
    path: String((a && a.path) || ''),
    kind: a && a.kind,
    verdict: a && a.verdict,
    url: a && a.url,
    id: a && a.id,
  }));
  return { ready, working, given };
}

/**
 * The read URL for one workspace-relative path, or `''` when either half is
 * missing — an anchor with no target is a link that looks live and is not.
 *
 * ⚠️ A23: the whole per-session read family (`workspace/file`, `serve`,
 * screenshots, stats) had NO console caller. `workpane.js` carried each row's
 * `path` and emitted no `href`, so the pane could list what a chat made and
 * open none of it.
 */
export function fileHref(sessionId, path) {
  if (!sessionId || !path) return '';
  return `/api/session/${encodeURIComponent(sessionId)}/workspace/file`
    + `?path=${encodeURIComponent(path)}`;
}

/**
 * One narrated line per action for the Timeline. Deduped by call id (running
 * then done is ONE line, its terminal state), narrated through the transcript's
 * own narrator, timed against the first action's clock (`0:06` / `1:12`).
 */
export function timelineLines(events, copy, nowMs) {
  const tools = (events || []).filter((e) => e && TOOL_TYPES.has(e.type));
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
function fileRow(name, why, verdict, href) {
  const row = el('tr');
  if (verdict) row.dataset.verdict = String(verdict);
  const cell = el('td', 'what');
  cell.dataset.label = '';
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
 * Render Files into *root*, distinguishing three answers:
 *   - both sources unreadable → a dashed entry with the honest sentence;
 *   - no files in any tier     → the empty state (a genuine empty folder);
 *   - files                    → the tiered table.
 * `data` is `{tree, artifacts, treeUnreadable, artifactsUnreadable}` where a
 * `null`/flagged source is a READ that failed, distinct from an empty one.
 * Returns which answer it drew, for the test to assert on.
 */
export function renderFiles(root, state, data, copy) {
  root.replaceChildren();
  const tree = data && data.tree;
  const artifacts = data && data.artifacts;
  // A23: every row that names a workspace file links to its own read route.
  const sessionId = (data && data.sessionId) || '';
  const hrefFor = (path) => fileHref(sessionId, path);
  const treeUnreadable = Boolean(
    data && (data.treeUnreadable || tree === null || (tree && tree.error)),
  );
  const artsUnreadable = Boolean(
    data && (data.artifactsUnreadable || artifacts === null),
  );

  // One table, filled by the tier helper — reused across every answer below.
  const table = el('table', 'table');
  const tbody = el('tbody');
  const tier = (labelKey, whyKey, items, rowFor) => {
    if (!items.length) return;
    const head = el('tr');
    const headCell = el('td', 'what');
    headCell.dataset.label = '';
    headCell.appendChild(document.createTextNode((copy && copy[labelKey]) || ''));
    headCell.appendChild(el('span', 'why', (copy && copy[whyKey]) || ''));
    head.appendChild(headCell);
    head.appendChild(el('td'));
    tbody.appendChild(head);
    items.forEach((it) => tbody.appendChild(rowFor(it)));
  };
  const flush = () => {
    if (tbody.children.length) {
      table.appendChild(tbody);
      root.appendChild(table);
    }
  };

  // Both sources failed — nothing to show but the honest sentence.
  if (treeUnreadable && artsUnreadable) {
    root.appendChild(unreadableEntry(copy && copy.files_unreadable));
    if (state) state.hidden = true;
    return 'unreadable';
  }

  // The ledger reads, the FOLDER does not. Show the deliverables we DO have
  // (with their verdicts), and NAME the folder read that failed — working and
  // given are folder-derived, so hiding them behind a confident-empty would be
  // exactly the honest-state failure this branch exists to prevent.
  if (treeUnreadable) {
    root.appendChild(unreadableEntry(copy && copy.files_tree_unreadable));
    tier('tier_ready', 'tier_ready_why', buildFiles(null, artifacts).ready,
      (a) => fileRow(a.name, verdictLabel(a.verdict, copy), a.verdict, hrefFor(a.path)));
    flush();
    if (state) state.hidden = true;
    return 'partial';
  }

  // The folder reads, the LEDGER does not. Without the ledger we cannot tell a
  // finished deliverable from a working file, so we do NOT relabel them as
  // "working" (a known deliverable reading as a scratch file is a confident
  // lie). The folder's files go under a NEUTRAL heading, inbound stays its own
  // honest tier (it needs no ledger), and the ledger read that failed is named.
  if (artsUnreadable) {
    root.appendChild(unreadableEntry(copy && copy.files_ledger_unreadable));
    const built = buildFiles(tree, null); // empty ledger → working = every non-inbound file
    tier('tier_folder', 'tier_folder_why', built.working,
      (f) => fileRow(f.name, '', null, hrefFor(f.path)));
    tier('tier_given', 'tier_given_why', built.given,
      (f) => fileRow(f.name, '', null, hrefFor(f.path)));
    flush();
    if (state) state.hidden = true;
    return 'partial';
  }

  // Both sources read — the normal three tiers.
  const built = buildFiles(tree, artifacts);
  const total = built.ready.length + built.working.length + built.given.length;
  if (!total) {
    if (state) {
      state.textContent = (copy && copy.files_empty) || '';
      state.hidden = false;
    }
    return 'empty';
  }
  if (state) state.hidden = true;
  tier('tier_ready', 'tier_ready_why', built.ready,
    (a) => fileRow(a.name, verdictLabel(a.verdict, copy), a.verdict, hrefFor(a.path)));
  tier('tier_working', 'tier_working_why', built.working,
    (f) => fileRow(f.name, '', null, hrefFor(f.path)));
  tier('tier_given', 'tier_given_why', built.given,
    (f) => fileRow(f.name, '', null, hrefFor(f.path)));
  flush();
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
  const [tree, arts] = await Promise.all([
    getJson(`/api/session/${id}/workspace/tree`),
    getJson(`/api/webgate/artifacts?session_id=${id}`),
  ]);
  // The artifacts endpoint answers `{artifacts, error}`; `error` set is a read
  // that failed, distinct from an empty list.
  const artifactsUnreadable = arts === null || Boolean(arts && arts.error);
  renderFiles(filesRoot, stateNode, {
    tree,
    artifacts: arts ? arts.artifacts : null,
    treeUnreadable: tree === null || Boolean(tree && tree.error),
    artifactsUnreadable,
    sessionId,
  }, copy);
}

async function loadTimeline(sessionId, tlRoot, stateNode, asideNode, copy) {
  const id = encodeURIComponent(sessionId);
  const data = await getJson(`/api/session/${id}/feed/events?limit=300`);
  const events = data && Array.isArray(data.events) ? data.events : (data === null ? null : []);
  renderTimeline(tlRoot, stateNode, events, copy);
  if (asideNode) {
    const n = events === null ? '—' : timelineLines(events, copy).length;
    asideNode.textContent = fill(copy.timeline_count, n);
  }
}

export function mount() {
  const filesRoot = document.getElementById('workpane-files');
  const tlRoot = document.getElementById('workpane-timeline');
  if (!filesRoot && !tlRoot) return null; // not a bound session — nothing to draw
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
