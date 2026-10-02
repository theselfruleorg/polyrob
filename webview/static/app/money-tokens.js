/**
 * money-tokens.js — Money › Book: what the identity check reads (W1).
 *
 * On 2026-09-25 a look-alike "PNL" the wallet held blocked the real token for a
 * day, and no screen showed the store that blocked it. This module draws, under
 * the book:
 *
 *   1. the ONE-SYMBOL-TWO-CONTRACTS warning from every chain's reconcile report;
 *   2. the positions the rail TRACKS that the ledger does not list, each with
 *      its lifecycle (open / quarantined / written off) and the owner's
 *      Write off / Undo quarantine buttons;
 *   3. the tokens Rob TRUSTS, by source, the ones the owner said are not the
 *      real token, and a small form to trust one.
 *
 * Every button asks before it acts (`window.confirm`) and posts to
 * `/api/webgate/tokens/{action}` — the same `core.wallet.token_trust`
 * functions `/wallet trust`, `/writeoff` and `/unquarantine` call on Telegram
 * and the REPL. Every word comes from the copy layer on `#money-copy`.
 */
import { postJson } from "./http.js";
import { fmtUsd } from "./format.js";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function fillAll(template, vars) {
  let out = String(template || "");
  for (const [k, v] of Object.entries(vars || {})) out = out.replace(`{${k}}`, String(v));
  return out;
}

function money(n) {
  // FE11: a sub-cent figure is written out, never a confident `$0.00`.
  return Number.isFinite(n) && n > 0 ? fmtUsd(n) : null;
}

function head(title, aside) {
  const h = el("div", "section-head");
  h.appendChild(el("h2", "section-title", title || ""));
  if (aside) h.appendChild(el("span", "section-aside", aside));
  return h;
}

function actButton(label, act, row) {
  const b = el("button", "btn btn-quiet", label || act);
  b.type = "button";
  b.dataset.tokenAct = act;
  b.dataset.chain = String(row.chain || "");
  b.dataset.address = String(row.address || "");
  if (row.symbol) b.dataset.symbol = String(row.symbol);
  return b;
}

/** Every chain's collision warning lines, in chain order. */
export function collisionLines(data) {
  const out = [];
  const chains = (data && data.chains) || {};
  for (const name of Object.keys(chains).sort()) {
    const report = (chains[name] && chains[name].report) || {};
    for (const line of report.collisions || []) {
      if (String(line).trim()) out.push(`${name}: ${line}`);
    }
  }
  return out;
}

/** The lifecycle words for one row, or "" for an open position. */
export function lifecycleWords(row, copy) {
  const state = String((row && row.lifecycle) || "open");
  if (state === "open") return "";
  const words = (copy && copy[`state_${state}`]) || state;
  return row.lifecycle_reason ? `${words} (${row.lifecycle_reason})` : words;
}

/** Collisions + the positions the rail tracks that the ledger does not list. */
export function trackedSection(data, copy, readOnly) {
  const section = el("div", "section");
  const lines = collisionLines(data);
  if (lines.length) {
    section.appendChild(head(copy && copy.collision));
    for (const line of lines) section.appendChild(el("p", "state-body", line));
  }
  const rows = (data && data.tracked) || [];
  if (!rows.length) return section;
  section.appendChild(head(copy && copy.tracked_title, copy && copy.tracked_aside));
  const list = el("ul", "list");
  for (const row of rows) {
    const li = el("li");
    const cost = money(row.entry);
    const state = lifecycleWords(row, copy);
    li.appendChild(el("span", "what",
      `${row.symbol || "?"} · ${row.chain || "?"} · ${row.address}`));
    li.appendChild(el("span", "why", [
      state,
      cost ? fillAll(copy && copy.cost, { price: cost }) : (copy && copy.no_cost),
    ].filter(Boolean).join("; ")));
    if (!readOnly && row.lifecycle !== "written_off") {
      li.appendChild(actButton(copy && copy.tok_writeoff, "writeoff", row));
    }
    if (!readOnly && row.lifecycle === "quarantined") {
      li.appendChild(actButton(copy && copy.tok_unquarantine, "unquarantine", row));
    }
    list.appendChild(li);
  }
  section.appendChild(list);
  return section;
}

/** The tokens Rob trusts, the owner's not-trusted list, and a trust form. */
export function tokensSection(view, copy, readOnly) {
  const section = el("div", "section");
  section.appendChild(head(copy && copy.tok_title, copy && copy.tok_aside));
  if (!view || view.error) {
    section.appendChild(el("p", "state-body",
      fillAll(copy && copy.tok_unreadable, { what: (view && view.error) || "" })));
    return section;
  }
  const list = el("ul", "list");
  for (const row of view.trusted || []) {
    const li = el("li");
    li.appendChild(el("span", "what",
      `${row.symbol || "?"} · ${row.chain} · ${row.address}`));
    li.appendChild(el("span", "why",
      (copy && copy[`src_${row.source}`]) || row.source));
    if (!readOnly && (row.source === "owner_pin" || row.source === "owner_approved")) {
      li.appendChild(actButton(copy && copy.tok_untrust, "untrust", row));
    }
    list.appendChild(li);
  }
  section.appendChild(list);
  if ((view.rejected || []).length) {
    section.appendChild(head(copy && copy.tok_rejected_title));
    const rej = el("ul", "list");
    for (const row of view.rejected) {
      const li = el("li");
      li.appendChild(el("span", "what",
        `${row.symbol || "?"} · ${row.chain} · ${row.address}`));
      if (!readOnly) li.appendChild(actButton(copy && copy.tok_trust, "trust", row));
      rej.appendChild(li);
    }
    section.appendChild(rej);
  }
  for (const what of view.unreadable || []) {
    section.appendChild(el("p", "state-body",
      fillAll(copy && copy.tok_unreadable, { what })));
  }
  if (!readOnly) {
    const form = el("form", "token-trust-form");
    for (const [name, label] of [["chain", copy && copy.tok_form_chain],
                                 ["address", copy && copy.tok_form_address],
                                 ["symbol", copy && copy.tok_form_symbol]]) {
      const input = el("input");
      input.name = name;
      input.placeholder = label || name;
      input.setAttribute("aria-label", label || name);
      form.appendChild(input);
    }
    const submit = el("button", "btn", copy && copy.tok_trust);
    submit.type = "submit";
    form.appendChild(submit);
    section.appendChild(form);
  }
  return section;
}

/** Confirm, post, and say back what the server answered. Returns
 *  `{ok, message}`; `confirmFn` and `fetcher` are injectable for tests. */
export async function runTokenAction(act, row, copy, opts = {}) {
  const ask = opts.confirmFn || ((text) => window.confirm(text));
  const question = fillAll(copy && copy[`tok_confirm_${act}`],
    { address: row.address, chain: row.chain });
  if (!ask(question)) return { ok: false, message: "", cancelled: true };
  try {
    const res = await postJson(`/api/webgate/tokens/${encodeURIComponent(act)}`,
      { chain: row.chain, address: row.address, symbol: row.symbol || "",
        reason: row.reason || "" }, { fetcher: opts.fetcher });
    const ok = Boolean(res.ok && res.body && res.body.ok);
    return { ok, message: (res.body && res.body.message) || (copy && copy.tok_failed) || "" };
  } catch (err) {
    console.error("[money] token action did not reach the console", err);
    return { ok: false, message: (copy && copy.tok_failed) || "" };
  }
}

export async function loadTokens(fetcher) {
  const call = fetcher || fetch;
  const resp = await call("/api/webgate/tokens", { credentials: "include" });
  if (!resp.ok) return { error: String(resp.status) };
  return resp.json();
}

/** Wire the buttons and the form inside *root*; `onDone()` redraws. */
export function bindTokenActions(root, copy, onDone) {
  if (!root) return;
  root.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-token-act]");
    if (!btn) return;
    btn.disabled = true;
    const row = { chain: btn.dataset.chain, address: btn.dataset.address,
                  symbol: btn.dataset.symbol || "" };
    const { ok, message, cancelled } = await runTokenAction(btn.dataset.tokenAct, row, copy);
    btn.disabled = false;
    if (cancelled) return;
    const note = el("span", "why", message);
    btn.after(note);
    if (ok && onDone) onDone();
  });
  root.addEventListener("submit", async (ev) => {
    const form = ev.target.closest("form.token-trust-form");
    if (!form) return;
    ev.preventDefault();
    const row = { chain: form.chain.value.trim(), address: form.address.value.trim(),
                  symbol: form.symbol.value.trim() };
    if (!row.chain || !row.address) return;
    const { ok, message, cancelled } = await runTokenAction("trust", row, copy);
    if (cancelled) return;
    form.appendChild(el("span", "why", message));
    if (ok && onDone) onDone();
  });
}
