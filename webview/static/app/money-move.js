/**
 * money-move.js — Money › Moves › Make a move (action cards).
 *
 * The owner builds a /send or /swap from pickers (chain, trusted token,
 * recent recipient). The server turns the fields into the QUOTE line and runs
 * it as the owner typing it; the answer is an action card whose Confirm runs
 * the exact quoted line once (core/surfaces/cards.py).
 *
 * The inbox.js rules hold: it decides nothing (every click is a POST the
 * server gates), it shows what came back verbatim, and it writes no copy —
 * the words ride on #money-move's data attributes and the card's own text.
 */
import { postJson } from './http.js';
import { drawCard, press } from './cards.js';

export const PICKERS_URL = '/api/webgate/cards/pickers';
export const QUOTE_URL = '/api/webgate/cards/quote';

/** The request body for the form's current values. Swap buys `token_out`. */
export function quoteBody(values) {
  const v = values || {};
  const verb = v.verb === 'swap' ? 'swap' : 'send';
  const body = { verb, amount: v.amount, token: v.token, chain: v.chain,
                 to: verb === 'swap' ? v.token_out : v.to };
  if (verb === 'swap' && v.slippage) body.slippage = v.slippage;
  return body;
}

/** The token choices for one chain: `native`, then the trusted tokens there. */
export function tokenOptions(pickers, chain, nativeWord = 'native') {
  const out = [{ value: 'native', label: nativeWord }];
  for (const t of (pickers && pickers.tokens) || []) {
    if (t.chain === chain && t.address) out.push({ value: t.address, label: t.symbol || t.address });
  }
  return out;
}

function fill(select, values) {
  select.replaceChildren();
  for (const v of values) {
    const o = document.createElement('option');
    o.value = v; o.textContent = v;
    select.appendChild(o);
  }
}

function fillList(list, options) {
  list.replaceChildren();
  for (const opt of options) {
    const o = document.createElement('option');
    o.value = opt.value;
    if (opt.label && opt.label !== opt.value) o.label = opt.label;
    list.appendChild(o);
  }
}

export async function mount(section, { fetcher = globalThis.fetch, postFetcher } = {}) {
  if (!section) return null;
  const form = section.querySelector('#money-move-form');
  const result = section.querySelector('#money-move-result');
  const copy = { ...section.dataset };
  let pickers = { chains: [], swap_chains: [], tokens: [], recipients: [] };
  try {
    const res = await fetcher(PICKERS_URL, { credentials: 'include' });
    if (res && res.ok) pickers = await res.json();
  } catch (err) { /* the form still works with typed values */ }

  const verbOf = () => form.elements.verb.value;
  const refresh = () => {
    const swap = verbOf() === 'swap';
    section.querySelectorAll('[data-for]').forEach((el) => {
      el.hidden = el.dataset.for !== (swap ? 'swap' : 'send');
    });
    const chains = swap ? pickers.swap_chains : pickers.chains;
    const keep = form.elements.chain.value;
    fill(form.elements.chain, chains || []);
    if ((chains || []).includes(keep)) form.elements.chain.value = keep;
    fillList(section.querySelector('#money-move-tokens'),
      tokenOptions(pickers, form.elements.chain.value, copy.native));
  };
  fillList(section.querySelector('#money-move-recipients'),
    (pickers.recipients || []).map((a) => ({ value: a })));
  if ((pickers.unreadable || []).length) {
    const p = document.createElement('p');
    p.className = 'entry-meta';
    p.textContent = copy.unreadable || '';
    form.after(p);
  }
  form.elements.verb.addEventListener('change', refresh);
  form.elements.chain.addEventListener('change', refresh);
  refresh();

  const show = (message, card) => {
    result.replaceChildren();
    if (card) {
      const onPress = async (c, act, entry) => {
        entry.querySelectorAll('button').forEach((b) => { b.disabled = true; });
        const r = await press(c.id, act, { fetcher: postFetcher, fallback: copy.unreachable });
        show(r.message, r.card);
      };
      result.appendChild(drawCard(card, { onPress }));
      if (message && card.state !== 'open') {
        const line = document.createElement('pre');
        line.className = 'entry-body entry-answer';
        line.textContent = message;
        result.appendChild(line);
      }
      return;
    }
    const pre = document.createElement('pre');
    pre.className = 'entry-body entry-answer';
    pre.textContent = message || copy.unreachable || '';
    result.appendChild(pre);
  };

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const values = Object.fromEntries(new FormData(form).entries());
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const { body } = await postJson(QUOTE_URL, quoteBody(values), { fetcher: postFetcher });
      show(body && body.message, body && body.card);
    } catch (err) {
      show(copy.unreachable, null);
    } finally { submit.disabled = false; }
  });
  return { refresh, show };
}

if (typeof document !== 'undefined') {
  const start = () => mount(document.getElementById('money-move'));
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
}
