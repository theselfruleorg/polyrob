/**
 * live.js — the console hears the agent (2026-09-16 audit, B3).
 *
 * Before this module only the bound chat (`transcript.js`) joined a Socket.IO
 * room; Work, the Inbox badge and the head line were one-shot fetches, so a
 * person watching the console saw a frozen snapshot while cron runs, goal
 * dispatches and payments went by. This module:
 *
 * 1. joins the `activity` room (`join_activity`, owner/admin-gated on the
 *    server — a stranger is refused and nothing here changes that) and
 *    re-dispatches every `activity_event` on `document` as
 *    `polyrob:activity`, so any destination module can redraw itself;
 * 2. ticks `polyrob:tick` every 30 s ONLY while the socket is down (070 W0.14:
 *    a tick re-read the workspace tree and the feed on every open chat, all
 *    day, even with the socket up);
 * 3. refreshes the frame's own two lines — the head truth and the Inbox badge —
 *    from `/api/webgate/head`, which renders them with the SAME Python the page
 *    used, so the words never fork.
 *
 * It decides nothing and renders no copy of its own. A failed read leaves the
 * server-rendered line where it was — never a blank, never a stale "running"
 * dressed up as fresh.
 */

import { loadSocketIo } from './socket-client.js';

const TICK_MS = 30000;
const HEAD_DEBOUNCE_MS = 2500;


/** Apply a `/api/webgate/head` body to the frame. Pure DOM; exported for tests. */
export function applyHead(body, root) {
  const doc = root || document;
  if (!body || typeof body !== 'object') return;
  const truth = doc.querySelector('.head-truth');
  if (truth && typeof body.headline === 'string' && typeof body.waiting_line === 'string') {
    truth.textContent = `${body.headline} ${body.waiting_line}`.trim();
  }
  const inbox = doc.querySelector('.nav-item[href="/inbox"]');
  if (!inbox) return;
  let badge = inbox.querySelector('.nav-badge');
  const text = typeof body.badge === 'string' ? body.badge : '';
  if (!text) {
    if (badge) badge.remove();
    inbox.removeAttribute('aria-label');
    return;
  }
  if (!badge) {
    badge = document.createElement('span');
    badge.setAttribute('aria-hidden', 'true');
    inbox.insertBefore(badge, inbox.firstChild);
  }
  badge.className = body.partial ? 'nav-badge is-uncertain' : 'nav-badge';
  badge.textContent = text;
  if (body.aria) inbox.setAttribute('aria-label', body.aria);
}

async function refreshHead(fetcher) {
  try {
    const resp = await (fetcher || fetch)('/api/webgate/head', { credentials: 'include' });
    if (!resp.ok) return;
    applyHead(await resp.json());
  } catch (err) {
    // The server-rendered line stays; a failed refresh must not blank it.
  }
}

function dispatch(name, detail) {
  document.dispatchEvent(new CustomEvent(name, { detail }));
}

/** The socket state the tick reads. `connected` is false until a connect. */
export function liveState() {
  return { connected: false };
}

/** One interval beat: `polyrob:tick` only while the socket is down; the head
 *  line is refreshed either way. Exported for tests. */
export function beat(state, fire = dispatch, refresh = refreshHead) {
  if (!state.connected) fire('polyrob:tick');
  refresh();
}

/** Bind the activity socket's events to *state* and `document`. Exported for tests.
 *
 * FE5: `connected` turns true only when the server ACKS a `join_activity` it
 * did not refuse (a refusal arrives before the ack on the same socket). A
 * refused join leaves the socket up but the room unjoined: `connected` stays
 * false, so the fallback tick keeps Work › Now and the Log fresh.
 * FE18: the head's live notice clears on that ack, never when a retried join
 * is merely sent. */
export function wireSocket(socket, state, fire = dispatch, later = setTimeout) {
  let refusals = 0;
  const join = () => {
    const before = refusals;
    socket.emit('join_activity', {}, () => {
      if (refusals !== before) return;
      state.connected = true;
      // 070 W0.17: a good (re)join clears the head's live notice.
      fire('polyrob:live-ok');
    });
  };
  socket.on('connect', join);
  socket.on('disconnect', () => { state.connected = false; });
  socket.on('connect_error', () => { state.connected = false; });
  socket.on('activity_event', (ev) => fire('polyrob:activity', ev));
  // A32: `join_activity` is owner/admin-gated and the server answers a refusal
  // on the `error` event. Nothing listened for it, so a seat that may not join
  // the cross-tenant stream simply saw a console that never updated again —
  // indistinguishable from a quiet agent. pause.js renders the sentence.
  socket.on('error', (body) => {
    refusals += 1;
    state.connected = false;
    fire('polyrob:live-refused', body);
    // 070 W0.17: the server keeps the socket on a rate limit; join again once
    // the window has passed, and the next ACKED join clears the notice.
    if (body && body.code === 'rate_limited') {
      const wait = Number(body.retry_after) > 0 ? Number(body.retry_after) : 60;
      later(join, wait * 1000);
    }
  });
}

/**
 * FE4: re-read a small state (the Pause button, the Apps list) whenever the
 * console may have missed a change: the fallback tick (socket down), a
 * debounced activity event (socket up), a good re-join, and the tab becoming
 * visible again. The tick alone fires only while the socket is DOWN, so a
 * healthy socket left those reads stale for the page's life. Exported for tests.
 */
export function onLiveChange(refresh, opts = {}) {
  const doc = opts.doc || document;
  const later = opts.later || setTimeout;
  const ms = opts.ms === undefined ? HEAD_DEBOUNCE_MS : opts.ms;
  let pending = false;
  const soon = () => {
    if (pending) return;
    pending = true;
    later(() => { pending = false; refresh(); }, ms);
  };
  doc.addEventListener('polyrob:tick', () => refresh());
  doc.addEventListener('polyrob:activity', soon);
  doc.addEventListener('polyrob:live-ok', soon);
  doc.addEventListener('visibilitychange', () => {
    if (doc.visibilityState === 'visible') soon();
  });
}

async function connect(state) {
  let io;
  try {
    io = await loadSocketIo();
  } catch (err) {
    console.error('[live] no live updates', err);
    return;
  }
  const socket = io({ path: '/socket.io', transports: ['polling', 'websocket'], reconnection: true });
  wireSocket(socket, state);
}

function bind() {
  if (!document.querySelector('.head-truth')) return; // not the shell
  let pending = null;
  const headSoon = () => {
    if (pending) return;
    pending = setTimeout(() => { pending = null; refreshHead(); }, HEAD_DEBOUNCE_MS);
  };
  document.addEventListener('polyrob:activity', headSoon);
  const state = liveState();
  setInterval(() => beat(state), TICK_MS);
  connect(state);
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
}
