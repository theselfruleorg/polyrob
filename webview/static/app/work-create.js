/**
 * work-create.js — the two Work create forms (goal, schedule).
 *
 * It decides nothing: every field is handed to the existing `owner_create`
 * writers behind `/api/webgate/{goals,cron}`, and the row says back exactly
 * what the server answered. All scheduling, grant and duplicate policy lives
 * there.
 *
 * ⚠️ A42 (2026-09-21 audit): this file used to start with
 * `if (form.dataset.readOnly === '1') { form.remove(); }` — a guard on an
 * attribute NO template has ever set. It read as a read-only defence and was
 * none; the real one is the template, which renders no form at all under
 * `WEBVIEW_READ_ONLY`. A guard that cannot fire is worse than no guard,
 * because the next reader trusts it, so it is gone rather than "fixed".
 *
 * ⚠️ A44: the forms now carry the fields the writers already accept (a goal's
 * body / priority / tools / step budget, a schedule's delivery and wake). The
 * payload is TYPED here — an empty control is OMITTED rather than sent as an
 * empty string, a number is a number, and the tools box is one name per line,
 * which is the list shape the writer validates.
 */
import { postJson, serverAnswer } from './http.js';

//: Fields the writers want as an integer, not as text.
const NUMERIC = new Set(['priority', 'max_steps']);
//: Fields the writers want as a list of names, typed one per line.
const LINE_LIST = new Set(['tools']);
//: Fields that are a plain yes/no. A checkbox is ABSENT from FormData when it
//: is unticked, so an explicit false has to be built rather than inferred.
const BOOLEAN = new Set(['wake_agent']);

/**
 * The typed payload for one form.
 *
 * Rules, in order: a blank control is left out entirely (the writer's own
 * default then applies, which is not the same as "set it to empty"); a numeric
 * field that is not a number is left out rather than sent as text the server
 * would have to guess at; a line list becomes an array of non-empty names; a
 * checkbox becomes a real boolean either way.
 */
export function payloadFrom(form) {
  const data = new FormData(form);
  const out = {};
  for (const [name, raw] of data.entries()) {
    const value = typeof raw === 'string' ? raw.trim() : raw;
    if (BOOLEAN.has(name)) { out[name] = true; continue; }
    if (value === '' || value === undefined || value === null) continue;
    if (NUMERIC.has(name)) {
      const n = Number(value);
      if (Number.isFinite(n)) out[name] = n;
      continue;
    }
    if (LINE_LIST.has(name)) {
      const items = String(value).split(/[\n,]/).map(s => s.trim()).filter(Boolean);
      if (items.length) out[name] = items;
      continue;
    }
    out[name] = value;
  }
  // An unticked checkbox never reaches FormData, so say `false` for every
  // boolean field the form declares but did not submit.
  form.querySelectorAll('input[type=checkbox][name]').forEach((box) => {
    if (BOOLEAN.has(box.name) && !(box.name in out)) out[box.name] = false;
  });
  return out;
}

export function bindCreate(form, { fetcher } = {}) {
  let pending = false;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (pending) return;
    pending = true;
    const button = form.querySelector('button[type=submit]');
    const result = form.querySelector('[role=status]');
    const values = payloadFrom(form);
    const draft = JSON.stringify(Object.fromEntries(new FormData(form)));
    button.disabled = true;
    form.setAttribute('aria-busy', 'true');
    try {
      const res = await postJson(`/api/webgate/${form.dataset.create}`, values, { fetcher });
      result.textContent = serverAnswer(res.body, form.dataset.unreachable);
      if (res.ok && res.body?.ok !== false) {
        if (JSON.stringify(Object.fromEntries(new FormData(form))) === draft) form.reset();
        document.dispatchEvent(new CustomEvent('polyrob:activity'));
      }
    } catch (err) { result.textContent = form.dataset.unreachable; }
    finally { pending = false; button.disabled = false; form.removeAttribute('aria-busy'); }
  });
}
if (typeof document !== "undefined") document.querySelectorAll('form[data-create]').forEach(form => bindCreate(form));
