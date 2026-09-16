/**
 * inbox.js — the Inbox's decision buttons actually decide (043 C5, fix round 1).
 *
 * ⚠️ The regression this exists for: `inbox.html` emitted `data-verb` buttons
 * and NOTHING bound them, on every posture. A decision surface whose buttons do
 * nothing is worse than one with no buttons — it looks like the owner acted.
 *
 * Three rules:
 *
 * 1. **It decides nothing itself.** Every click is a POST to one of the three
 *    existing routes (`webview/inbox.py`), which dispatch to the deciders the
 *    CLI and Telegram already use. This file carries no policy.
 * 2. **It says what came back, verbatim.** The endpoint answers
 *    `{ok, message}`; the card renders that message whether it is a success or
 *    a refusal. A button that silently fails is the thing being fixed.
 * 3. **It writes no copy.** Every word a person reads is rendered by the server
 *    through the copy layer; what is built here is the answer the server sent.
 *    The one exception is a transport failure the server never saw, which the
 *    template hands over on `#inbox-copy`.
 *
 * Under `WEBVIEW_READ_ONLY` the buttons are not rendered at all (the template
 * states the posture once, like the composer does), so this binds nothing.
 *
 * The POST itself is `http.js` — see it for what `webgate.csrf_guard` actually
 * checks, which is a same-origin `Origin` header and NOT a token.
 */
import { postJson } from './http.js';

/** The route for one decision. Both segments are encoded: an id can carry a
 *  colon (`telegram:12345`) or a slash. */
export function decisionUrl(kind, id, verb) {
  return `/api/webgate/inbox/${encodeURIComponent(kind)}`
    + `/${encodeURIComponent(id)}/${encodeURIComponent(verb)}`;
}

/**
 * POST one decision. Returns `{ok, message}` — never throws, because a thrown
 * error on a click is a button that did nothing with no explanation.
 * `fallback` is the server-rendered sentence for a transport failure.
 */
export async function decide(kind, id, verb, { fetcher, fallback } = {}) {
  try {
    const { ok, status, body } = await postJson(
      decisionUrl(kind, id, verb), undefined, { fetcher });
    if (body && typeof body.message === 'string') {
      return { ok: Boolean(body.ok), message: body.message };
    }
    const detail = body && typeof body.detail === 'string' ? body.detail : '';
    if (ok) return { ok: true, message: detail };
    return { ok: false, message: detail || `${status}` };
  } catch (err) {
    console.error('[inbox] the decision did not reach the console', err);
    return { ok: false, message: fallback || '' };
  }
}

/** Show the answer on the card, and retire the buttons a decision consumed. */
export function applyResult(card, result) {
  if (!card) return;
  let line = card.querySelector('.entry-answer');
  if (!line) {
    line = document.createElement('p');
    line.className = 'entry-meta entry-answer';
    card.appendChild(line);
  }
  line.textContent = result.message || '';
  line.dataset.ok = result.ok ? 'true' : 'false';
  if (result.ok) {
    const actions = card.querySelector('.entry-actions');
    if (actions) actions.remove();
    card.dataset.decided = 'true';
  }
  return line;
}

function copyFrom(node) {
  return node ? { ...node.dataset } : {};
}

function bind() {
  const copy = copyFrom(document.getElementById('inbox-copy'));
  document.querySelectorAll('.entry-actions button[data-verb]').forEach((button) => {
    button.addEventListener('click', async () => {
      const card = button.closest('.entry');
      const buttons = card ? card.querySelectorAll('.entry-actions button') : [button];
      buttons.forEach((b) => { b.disabled = true; });
      const result = await decide(button.dataset.kind, button.dataset.id,
                                  button.dataset.verb,
                                  { fallback: copy.unreachable });
      applyResult(card, result);
      if (!result.ok) buttons.forEach((b) => { b.disabled = false; });
    });
  });
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
}
