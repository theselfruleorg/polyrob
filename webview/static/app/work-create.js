/** Goal/schedule forms delegate all scheduling and grant policy to owner_create. */
import { postJson, serverAnswer } from './http.js';
export function bindCreate(form, { fetcher } = {}) {
  if (form.dataset.readOnly === '1') { form.remove(); return; }
  let pending = false;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (pending) return;
    pending = true;
    const button = form.querySelector('button[type=submit]');
    const result = form.querySelector('[role=status]');
    const values = Object.fromEntries(new FormData(form));
    const draft = JSON.stringify(values);
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
if (typeof document !== 'undefined') document.querySelectorAll('form[data-create]').forEach(form => bindCreate(form));
