/**
 * cards.js — the Inbox's action cards (core/surfaces/cards.py).
 *
 * A card is one decision: a money quote to Confirm / Refresh / Cancel, an
 * agent's proposal to quote, or an agent's question to answer. Telegram draws
 * the same card with buttons and the REPL with its tap tokens.
 *
 * Three rules, the inbox.js rules:
 *
 * 1. **It decides nothing itself.** A click POSTs `/api/webgate/cards/{id}/{act}`,
 *    which runs the card's tap token through the console's owner-verb plane —
 *    the same gates as typing it on any seat.
 * 2. **It says what came back, verbatim**, and redraws the card from the
 *    server's answer: a decided card has no buttons.
 * 3. **It writes no copy.** The card text and the button labels come from the
 *    server; the two transport sentences ride on the section's data attributes.
 */
import { postJson } from './http.js';

export function cardsUrl() { return '/api/webgate/cards'; }

export function pressUrl(id, act) {
  return `/api/webgate/cards/${encodeURIComponent(id)}/${encodeURIComponent(act)}`;
}

/** One card as a ledger entry. `onPress(card, act)` handles a click. */
export function drawCard(card, { readOnly = false, onPress } = {}) {
  const entry = document.createElement('div');
  entry.className = 'entry is-needs-you';
  entry.dataset.cardId = card.id;
  const title = document.createElement('h2');
  title.className = 'entry-title';
  title.textContent = card.title || '';
  entry.appendChild(title);
  const body = document.createElement('pre');
  body.className = 'entry-body entry-card';
  body.textContent = card.text || '';
  entry.appendChild(body);
  if (!readOnly && Array.isArray(card.buttons) && card.buttons.length) {
    const row = document.createElement('div');
    row.className = 'entry-actions';
    for (const b of card.buttons) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `btn${b.primary ? ' btn-primary' : ''}`;
      button.textContent = b.label || b.act;
      button.dataset.act = b.act;
      button.addEventListener('click', () => onPress && onPress(card, b.act, entry));
      row.appendChild(button);
    }
    entry.appendChild(row);
  }
  return entry;
}

/** POST one tap; `{ok, message, card}`, never throws. */
export async function press(id, act, { fetcher, fallback } = {}) {
  try {
    const { body } = await postJson(pressUrl(id, act), undefined, { fetcher });
    if (body && typeof body.message === 'string') {
      return { ok: Boolean(body.ok), message: body.message, card: body.card || null };
    }
    return { ok: false, message: fallback || '', card: null };
  } catch (err) {
    console.error('[cards] the tap did not reach the console', err);
    return { ok: false, message: fallback || '', card: null };
  }
}

/** Load and draw the open cards into `section`. Returns how many were drawn. */
export async function load(section, { fetcher = globalThis.fetch, postFetcher } = {}) {
  if (!section) return 0;
  const list = section.querySelector('[data-cards-list]');
  const copy = { ...section.dataset };
  const readOnly = copy.readOnly === 'true';
  let body = null;
  try {
    const res = await fetcher(cardsUrl(), { credentials: 'include' });
    body = res && res.ok ? await res.json() : null;
  } catch (err) {
    body = null;
  }
  list.replaceChildren();
  if (!body || body.readable === false) {
    const p = document.createElement('p');
    p.className = 'entry-meta';
    p.textContent = copy.unreadable || '';
    list.appendChild(p);
    section.hidden = false;
    return 0;
  }
  const cards = Array.isArray(body.cards) ? body.cards : [];
  const onPress = async (card, act, entry) => {
    entry.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    const result = await press(card.id, act, { fetcher: postFetcher, fallback: copy.unreachable });
    const next = result.card ? drawCard(result.card, { readOnly, onPress }) : entry;
    if (next !== entry) entry.replaceWith(next);
    let line = next.querySelector('.entry-answer');
    if (!line) {
      line = document.createElement('p');
      line.className = 'entry-meta entry-answer';
      next.appendChild(line);
    }
    line.textContent = result.message || '';
    line.dataset.ok = result.ok ? 'true' : 'false';
    if (!result.card) entry.querySelectorAll('button').forEach((b) => { b.disabled = false; });
  };
  for (const card of cards) list.appendChild(drawCard(card, { readOnly, onPress }));
  section.hidden = cards.length === 0;
  return cards.length;
}

if (typeof document !== 'undefined') {
  const start = () => load(document.getElementById('inbox-cards'));
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
}
