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
 * 2. ticks `polyrob:tick` every 30 s as the fallback when the socket is down;
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

async function connect() {
  let io;
  try {
    io = await loadSocketIo();
  } catch (err) {
    console.error('[live] no live updates', err);
    return;
  }
  const socket = io({ path: '/socket.io', transports: ['polling', 'websocket'], reconnection: true });
  socket.on('connect', () => socket.emit('join_activity', {}));
  socket.on('activity_event', (ev) => dispatch('polyrob:activity', ev));
  // A32: `join_activity` is owner/admin-gated and the server answers a refusal
  // on the `error` event. Nothing listened for it, so a seat that may not join
  // the cross-tenant stream simply saw a console that never updated again —
  // indistinguishable from a quiet agent. pause.js renders the sentence.
  socket.on('error', (body) => dispatch('polyrob:live-refused', body));
}

function bind() {
  if (!document.querySelector('.head-truth')) return; // not the shell
  let pending = null;
  const headSoon = () => {
    if (pending) return;
    pending = setTimeout(() => { pending = null; refreshHead(); }, HEAD_DEBOUNCE_MS);
  };
  document.addEventListener('polyrob:activity', headSoon);
  setInterval(() => { dispatch('polyrob:tick'); refreshHead(); }, TICK_MS);
  connect();
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
}
