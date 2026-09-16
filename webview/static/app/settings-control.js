/** Typed setting editor. Policy/validation stays in existing server writers. */
import { patchJson, serverAnswer } from './http.js';

const node = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};


export function settingControl(spec, copy, { flag = false, fetcher } = {}) {
  const root = node('div', 'setting-control');
  if (copy.read_only !== '0' || (flag && spec.console_writable !== true)) {
    if (spec.write_reason) root.append(node('p', 'why', spec.write_reason));
    return root;
  }
  const key = spec.key || spec.name;
  root.dataset.settingKey = key;
  const label = spec.description || key;
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
    input.value = Array.isArray(spec.value) ? spec.value.join('\n') : String(spec.value ?? '');
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
  actions.append(save, confirm, cancel); root.append(actions, status);
  if (flag) root.append(node('p', 'why', copy.control_restart));
  function cancelConfirm() { confirm.hidden = true; cancel.hidden = true; save.hidden = false; }
  cancel.addEventListener('click', () => { cancelConfirm(); status.textContent = ''; });
  function syncValue(value) {
    if (spec.type === 'bool' && knownBool) {
      boolValue = value === true || /^(true|1|on|yes)$/i.test(String(value));
      input.setAttribute('aria-checked', String(boolValue));
      input.classList.toggle('is-on', boolValue);
    } else input.value = Array.isArray(value) ? value.join('\n') : String(value ?? '');
  }
  function value() {
    if (spec.type === 'bool') return knownBool ? boolValue : input.value === 'true';
    if (spec.type === 'list') return input.value.split('\n').map(v => v.trim()).filter(Boolean);
    // Keep scalar strings: server coercion is authoritative and rejects blank numbers.
    return input.value;
  }
  async function write(confirmed) {
    if (spec.type === 'bool' && !knownBool && !input.value) return;
    const proposed = value();
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
            : copy.control_saved || status.textContent;
          const effective = root.closest('td')?.querySelector('[data-effective]');
          if (effective) effective.textContent = JSON.stringify(res.body.value);
          const source = root.closest('tr')?.querySelector('[data-source]');
          if (source && res.body.source != null) source.textContent = String(res.body.source);
          if (key === 'ui.show_avatar') document.body.dataset.showAvatar = String(res.body.value);
          if (key === 'ui.theme') document.dispatchEvent(new CustomEvent('console-theme', { detail: res.body.value }));
        }
        cancelConfirm();
      } else if (res.body?.guarded) {
        save.hidden = true; confirm.hidden = cancel.hidden = false;
      }
    } catch (err) { status.textContent = copy.unreachable || String(err); }
    finally { input.disabled = save.disabled = confirm.disabled = cancel.disabled = false; }
  }
  save.addEventListener('click', () => {
    if (spec.sensitivity === 'guarded') {
      status.textContent = copy.control_confirmation;
      save.hidden = true; confirm.hidden = cancel.hidden = false; confirm.focus();
    } else write(false);
  });
  confirm.addEventListener('click', () => write(true));
  return root;
}
