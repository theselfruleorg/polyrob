/**
 * pause.js — the control the head line already names (2026-09-21 audit, A2).
 *
 * ⚠️ The defect this closes: `core.status_render.pause_headline` — the ONE
 * sentence every seat renders — tells the owner to press "the Resume button"
 * or "the Pause button", and the console shell carried no such control on any
 * screen. A sentence that names a control that does not exist is worse than
 * silence: the owner goes looking for it.
 *
 * Four rules, the same ones every other 043 module keeps:
 *
 * 1. **It decides nothing.** A click is a POST to the existing
 *    `/api/webgate/{pause,resume}` — the same `core.surfaces.owner_admin`
 *    seam `polyrob autonomy pause`, the REPL `/pause` and Telegram `/pause`
 *    go through. The button says back exactly what the server answered, which
 *    is the VERIFIED read-back state, never a green tick on a write.
 * 2. **It reflects the state it read, never the state it asked for.** The
 *    label comes from `GET /api/webgate/pause`, re-read after every write, so
 *    a refused or partial resume can never leave the button lying.
 * 3. **It draws no verb it cannot act on.** Under `WEBVIEW_READ_ONLY` the
 *    template renders no button at all (an absent control, not a disabled one
 *    that 403s), so this binds nothing.
 * 4. **Every word comes from the copy layer**, handed over on `#shell-copy`'s
 *    data attributes — no 043 template carries an inline script.
 *
 * The same module owns the `#live-notice` line (A32): the `activity` room is
 * owner/admin-gated and refuses a seat that may not join it, and before this
 * `live.js` dropped that refusal on the floor, so the console simply stopped
 * updating with no explanation.
 */
import { postJson, serverAnswer } from './http.js';

/** The copy the server handed over, as a plain object. */
export function copyFrom(node) {
  return node ? { ...node.dataset } : {};
}

/**
 * Is autonomy paused, according to a `/api/webgate/pause` body?
 *
 * `null` when the body says nothing about it — an unreadable record must not
 * render as "running", which is the one reading a paused agent may never get.
 */
export function pausedFrom(body) {
  if (!body || typeof body !== 'object') return null;
  if (typeof body.paused === 'boolean') return body.paused;
  if (typeof body.halted === 'boolean') return body.halted;
  const state = body.state;
  if (state && typeof state.paused === 'boolean') return state.paused;
  return null;
}

/**
 * Put *paused* on the button: the label, the verb it will POST, and its own
 * pressed state. `null` (unknown) leaves the label where it was and marks the
 * control uncertain rather than offering a verb over a state nobody read.
 */
export function applyState(button, paused, copy) {
  if (!button) return null;
  if (paused === null || paused === undefined) {
    button.dataset.paused = 'unknown';
    button.classList.add('is-uncertain');
    return null;
  }
  button.classList.remove('is-uncertain');
  button.dataset.paused = paused ? '1' : '0';
  button.dataset.verb = paused ? 'resume' : 'pause';
  const label = paused ? (copy && copy.resume) : (copy && copy.pause);
  const words = button.querySelector('.kbd');
  if (words) words.textContent = label || '';
  else button.textContent = label || '';
  button.setAttribute('aria-label', label || (copy && copy.pause_label) || '');
  return paused;
}

/** Read the live pause record. Returns the body, or `null` on any refusal. */
export async function readState(fetcher) {
  try {
    const call = fetcher || fetch;
    const resp = await call('/api/webgate/pause', { credentials: 'include' });
    if (!resp.ok) return null;
    return await resp.json();
  } catch (err) {
    return null;
  }
}

/**
 * Pause or resume. Returns `{paused, message}` — `paused` is the state the
 * SERVER reported back (never the one we asked for), `message` its own
 * sentence. A transport failure is the copy layer's fallback, never silence.
 */
export async function toggle(verb, copy, opts = {}) {
  const url = verb === 'resume' ? '/api/webgate/resume' : '/api/webgate/pause';
  const payload = verb === 'resume' ? {} : { scopes: ['all'] };
  try {
    const { body } = await postJson(url, payload, { fetcher: opts.fetcher });
    return {
      paused: pausedFrom(body),
      message: serverAnswer(body, (copy && copy.pause_unreachable) || ''),
    };
  } catch (err) {
    console.error('[pause] the change did not reach the console', err);
    return { paused: null, message: (copy && copy.pause_unreachable) || '' };
  }
}

// --------------------------------------------------------------------- wiring

/** Show the answer beside the head line, replacing any previous one. */
function say(message) {
  const note = document.getElementById('live-notice');
  if (!note) return;
  note.textContent = message || '';
  note.hidden = !message;
}

function bind() {
  const copy = copyFrom(document.getElementById('shell-copy'));
  const button = document.getElementById('head-pause');
  // A32: live.js relays the activity room's refusal here rather than dropping
  // it — a console that quietly stops updating looks like a quiet agent.
  document.addEventListener('polyrob:live-refused', (ev) => {
    say(serverAnswer(ev && ev.detail, copy.live_refused || ''));
  });
  if (!button) return; // read-only console, or a page without the shell head

  const refresh = () => readState().then((body) => applyState(button, pausedFrom(body), copy));
  refresh();

  button.addEventListener('click', async () => {
    const verb = button.dataset.verb === 'resume' ? 'resume' : 'pause';
    button.disabled = true;
    try {
      const { paused, message } = await toggle(verb, copy);
      say(message);
      // The write's own answer first, then the record re-read: the button may
      // never claim a state the runtime does not enforce.
      applyState(button, paused, copy);
      await refresh();
    } finally {
      button.disabled = false;
    }
  });

  document.addEventListener('polyrob:tick', refresh);
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
}
