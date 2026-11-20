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
import { postJson, serverAnswer } from './http.js';
import { drawCard, press } from './cards.js';

export const PICKERS_URL = '/api/webgate/cards/pickers';
export const QUOTE_URL = '/api/webgate/cards/quote';
export const WALLET_URL = '/api/webgate/wallet';

/** True only when the wallet read SAYS there is no wallet. A failed read keeps
 *  the form: the quote's own gates still answer, and unknown is not "none". */
export async function walletIsOff(fetcher) {
  try {
    const res = await fetcher(WALLET_URL, { credentials: 'include' });
    if (!res || !res.ok) return false;
    const body = await res.json();
    return Boolean(body && body.state === 'disabled');
  } catch (err) { return false; }
}

/** No wallet: the form would quote a move nothing can make. Hide it (and its
 *  note) and say what the console's Money › Book says, with the one action. */
export function showNoWallet(section, form, copy) {
  form.hidden = true;
  const note = form.nextElementSibling;
  if (note && note.classList.contains('entry-meta')) note.hidden = true;
  const entry = document.createElement('div');
  entry.className = 'entry';
  entry.dataset.noWallet = '1';
  for (const [cls, text] of [['entry-title', copy.no_wallet], ['entry-body', copy.no_wallet_how]]) {
    const p = document.createElement('p');
    p.className = cls;
    p.textContent = text || '';
    entry.appendChild(p);
  }
  form.before(entry);
}

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
  if (await walletIsOff(fetcher)) {
    showNoWallet(section, form, copy);
    return { noWallet: true };
  }
  let pickers = { chains: [], swap_chains: [], tokens: [], recipients: [] };
  // FE7: a failed read leaves the Chain list EMPTY (it is a select, it cannot
  // be typed), so the form says the read failed instead of going quietly blank.
  let pickersFailed = false;
  try {
    const res = await fetcher(PICKERS_URL, { credentials: 'include' });
    if (res && res.ok) pickers = await res.json();
    else pickersFailed = true;
  } catch (err) { pickersFailed = true; }
  if (!pickers || typeof pickers !== 'object') {
    pickers = { chains: [], swap_chains: [], tokens: [], recipients: [] };
    pickersFailed = true;
  }

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
  if (pickersFailed) {
    const p = document.createElement('p');
    p.className = 'entry-meta';
    p.dataset.pickersFailed = '1';
    p.setAttribute('role', 'status');
    p.textContent = copy.pickers_failed || '';
    form.after(p);
  } else if ((pickers.unreadable || []).length) {
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
        const r = await press(c.id, act, { fetcher: postFetcher, fallback: copy.unreachable,
                                           refused: copy.refused });
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
      // A refusal (read-only, not the wallet's owner) names itself in
      // `detail`/`error`, not `message`: show what the server said.
      show(body && body.card ? body.message : serverAnswer(body, copy.unreachable),
           body && body.card);
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
