/** Typed setting editor. Policy/validation stays in existing server writers. */
import { patchJson, serverAnswer } from './http.js';

const node = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};


/**
 * One setting editor. With `bare: true` (113 W5, CP-1: one Save per group) it
 * draws the input and its status line only, and hangs `root.setting =
 * {changed(), write(confirmed), guarded}` on the node for `groupSave`; without
 * it, the row carries its own Save / Confirm / Cancel as before (Advanced).
 */
export function settingControl(spec, copy, { flag = false, fetcher, bare = false } = {}) {
  const root = node('div', 'setting-control');
  if (copy.read_only !== '0' || (flag && spec.console_writable !== true)) {
    const why = spec.write_reason || copy.control_readonly;  // a pref row carries no reason of its own
    if (why) root.append(node('p', 'why', why));
    return root;
  }
  const key = spec.key || spec.name;
  root.dataset.settingKey = key;
  const label = spec.label || spec.description || key;
  // 113 W6: a list editor holds entries only. A value that is not a list (the
  // catalog's "empty" word, a raw env line) is never put in the box, so Save
  // can never store it as an entry.
  const shown = (v) => (Array.isArray(v) ? v.join('\n')
    : spec.type === 'list' ? '' : String(v ?? ''));
  let input;
  let boolValue = spec.value === true || /^(true|1|on|yes)$/i.test(String(spec.value));
  const knownBool = /^(true|false|1|0|on|off|yes|no)$/i.test(String(spec.value));
  if (spec.type === 'bool' && !knownBool) {
    input = node('select');
    for (const v of ['', 'true', 'false']) {
      const option = node('option', '', v || '—'); option.value = v;
      option.disabled = v === ''; input.append(option);
    }
    input.value = '';
  } else if (spec.type === 'bool') {
    input = node('button', 'switch');
    input.type = 'button';
    input.setAttribute('role', 'switch');
    const draw = () => {
      input.setAttribute('aria-checked', String(boolValue));
      input.classList.toggle('is-on', boolValue);
    };
    draw();
    input.addEventListener('click', () => { boolValue = !boolValue; draw(); cancelConfirm(); });
  } else if (spec.type === 'enum') {
    input = node('select');
    if (spec.value == null || !(spec.enum_values || []).includes(spec.value)) {
      const unknown = node('option', '', '—'); unknown.value = ''; unknown.disabled = true;
      unknown.selected = true; input.append(unknown);
    }
    (spec.enum_values || []).forEach(value => {
      const option = node('option', '', value); option.value = value;
      option.selected = value === spec.value; input.append(option);
    });
  } else {
    input = node(spec.type === 'list' ? 'textarea' : 'input');
    if (spec.type === 'int' || spec.type === 'float') {
      input.type = 'number'; input.step = spec.type === 'int' ? '1' : 'any';
      if (spec.min != null) input.min = spec.min;
      if (spec.max != null) input.max = spec.max;
    } else if (spec.type !== 'list') input.type = 'text';
    input.value = shown(spec.value);
  }
  input.setAttribute('aria-label', label);
  if (spec.type !== 'bool' || !knownBool) input.addEventListener('input', () => cancelConfirm());
  root.append(input);
  if (spec.type === 'list') root.append(node('p', 'why', copy.control_list));
  const actions = node('div', 'entry-actions');
  const save = node('button', 'btn', copy.control_save); save.type = 'button';
  const confirm = node('button', 'btn', copy.control_confirm); confirm.type = 'button'; confirm.hidden = true;
  const cancel = node('button', 'btn btn-quiet', copy.control_cancel); cancel.type = 'button'; cancel.hidden = true;
  const status = node('p', 'why'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  actions.append(save, confirm, cancel);
  if (bare) root.append(status); else root.append(actions, status);
  if (flag) root.append(node('p', 'why', copy.control_restart));
  function cancelConfirm() { confirm.hidden = true; cancel.hidden = true; save.hidden = false; }
  cancel.addEventListener('click', () => { cancelConfirm(); status.textContent = ''; });
  function syncValue(value) {
    if (spec.type === 'bool' && knownBool) {
      boolValue = value === true || /^(true|1|on|yes)$/i.test(String(value));
      input.setAttribute('aria-checked', String(boolValue));
      input.classList.toggle('is-on', boolValue);
    } else input.value = shown(value);
  }
  function value() {
    if (spec.type === 'bool') return knownBool ? boolValue : input.value === 'true';
    if (spec.type === 'list') return input.value.split('\n').map(v => v.trim()).filter(Boolean);
    // Keep scalar strings: server coercion is authoritative and rejects blank numbers.
    return input.value;
  }
  let saved = shown(spec.type === 'bool' && knownBool ? boolValue : spec.value);
  const current = () => (spec.type === 'bool' && !knownBool && !input.value ? saved : shown(value()));
  async function write(confirmed) {
    if (spec.type === 'bool' && !knownBool && !input.value) return { ok: false };
    const proposed = value();
    let outcome = { ok: false };
    input.disabled = save.disabled = confirm.disabled = cancel.disabled = true;
    try {
      const url = flag ? `/api/webgate/config/${encodeURIComponent(key)}` : '/api/webgate/preferences';
      const payload = flag ? { value: String(proposed) } : { key, value: proposed };
      if (confirmed) payload.confirm = true;
      const res = await patchJson(url, payload, { fetcher });
      status.textContent = serverAnswer(res.body, copy.unreachable);
      if (res.ok && res.body?.ok !== false) {
        if (!flag && Object.hasOwn(res.body || {}, 'value')) {
          syncValue(res.body.value);
          status.textContent = res.body.queued
            ? `${copy.control_queued || ''} ${res.body.queued.map(item => item.error ? `${item.entry}: ${item.error}` : item.entry).join('; ')}`.trim()
            : `${copy.control_saved || status.textContent} ${(res.body.applies && copy[`control_applies_${String(res.body.applies).replace('-', '_')}`]) || ''}`.trim();
          const effective = root.closest('td')?.querySelector('[data-effective]');
          if (effective) effective.textContent = Array.isArray(res.body.value) && !res.body.value.length
            ? (copy.set_list_none || '') : JSON.stringify(res.body.value);
          const source = root.closest('tr')?.querySelector('[data-source]');
          if (source && res.body.source != null) source.textContent = String(res.body.source);
          if (key === 'ui.show_avatar') document.body.dataset.showAvatar = String(res.body.value);
          if (key === 'ui.theme') document.dispatchEvent(new CustomEvent('console-theme', { detail: res.body.value }));
        }
        saved = current();
        outcome = { ok: true };
        cancelConfirm();
      } else if (res.body?.guarded) {
        outcome = { ok: false, guarded: true };
        save.hidden = true; confirm.hidden = cancel.hidden = false;
      }
    } catch (err) { status.textContent = copy.unreachable || String(err); }
    finally { input.disabled = save.disabled = confirm.disabled = cancel.disabled = false; }
    return outcome;
  }
  root.setting = {
    changed: () => current() !== saved,
    write,
    guarded: spec.sensitivity === 'guarded',
  };
  save.addEventListener('click', () => {
    if (spec.sensitivity === 'guarded') {
      status.textContent = copy.control_confirmation;
      save.hidden = true; confirm.hidden = cancel.hidden = false; confirm.focus();
    } else write(false);
  });
  confirm.addEventListener('click', () => write(true));
  return root;
}

/**
 * ONE Save for a group of bare controls (113 W5, CP-1). Save writes every
 * control that changed; a group with a guarded change asks once (Confirm /
 * Cancel) and then writes them all with `confirm: true`. Each row says its own
 * answer on its own status line; the group line says only "Nothing changed."
 */
export function groupSave(controls, copy) {
  const actions = node('div', 'entry-actions setting-group-actions');
  const save = node('button', 'btn', copy.control_save); save.type = 'button';
  const confirm = node('button', 'btn', copy.control_confirm); confirm.type = 'button'; confirm.hidden = true;
  const cancel = node('button', 'btn btn-quiet', copy.control_cancel); cancel.type = 'button'; cancel.hidden = true;
  const status = node('p', 'why'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  actions.append(save, confirm, cancel, status);
  const live = () => controls.filter((c) => c && c.setting);
  const reset = () => { confirm.hidden = cancel.hidden = true; save.hidden = false; };
  async function writeAll(confirmed) {
    const changed = live().filter((c) => c.setting.changed());
    save.disabled = confirm.disabled = cancel.disabled = true;
    try {
      for (const c of changed) await c.setting.write(confirmed);   // one at a time, in row order
    } finally { save.disabled = confirm.disabled = cancel.disabled = false; reset(); }
  }
  save.addEventListener('click', () => {
    const changed = live().filter((c) => c.setting.changed());
    if (!changed.length) { status.textContent = copy.nothing_changed || ''; return; }
    status.textContent = '';
    if (changed.some((c) => c.setting.guarded)) {
      status.textContent = copy.control_confirmation || '';
      save.hidden = true; confirm.hidden = cancel.hidden = false; confirm.focus();
      return;
    }
    writeAll(false);
  });
  confirm.addEventListener('click', () => { status.textContent = ''; writeAll(true); });
  cancel.addEventListener('click', () => { reset(); status.textContent = ''; });
  return actions;
}
