// Work's two create forms (A42 + A44, 2026-09-21 audit).
//
// What matters here is small and specific:
//   * the form posts to the existing writer and keeps a refused draft;
//   * a blank control is OMITTED, not sent as an empty string (the writer's own
//     default must apply, which is not the same as "set it to empty");
//   * a number is a number and the tools box is a list of names;
//   * an unticked checkbox is an explicit `false`, never an absence;
//   * the dead `data-read-only` guard is GONE — no template ever set it, and a
//     guard that cannot fire reads as a defence that is not there.
import { it, expect, vi } from 'vitest';
import { bindCreate, payloadFrom } from '../static/app/work-create.js';

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

for (const kind of ['goals', 'cron']) it(`creates ${kind} through existing writer and preserves refused draft`, async () => {
  const form = document.createElement('form'); form.dataset.create = kind;
  form.innerHTML = '<textarea name="title">A task</textarea><input name="schedule" value="every 1h"><button type="submit">Create</button><p role="status"></p>';
  const fetcher = vi.fn(async () => ({ ok: false, status: 400, json: async () => ({ message: 'Refused by owner policy' }) }));
  bindCreate(form, { fetcher }); form.dispatchEvent(new Event('submit', { cancelable: true })); await flush();
  expect(fetcher.mock.calls[0][0]).toBe(`/api/webgate/${kind}`);
  expect(form.querySelector('[role=status]').textContent).toBe('Refused by owner policy');
  expect(form.querySelector('textarea').value).toBe('A task');
});

it('omits a blank control instead of sending an empty value', () => {
  const form = document.createElement('form');
  form.innerHTML = '<textarea name="title">Ship it</textarea><textarea name="body"></textarea>'
    + '<input name="priority" value=""><input name="max_steps" value="">';
  const body = payloadFrom(form);
  expect(body).toEqual({ title: 'Ship it' });
});

it('types a goal payload: numbers are numbers, tools is a list of names', () => {
  const form = document.createElement('form');
  form.innerHTML = '<textarea name="title">Ship it</textarea><textarea name="body">the detail</textarea>'
    + '<input name="priority" value="7"><input name="max_steps" value="24">'
    + '<textarea name="tools">web_search\nfilesystem\n</textarea>';
  expect(payloadFrom(form)).toEqual({
    title: 'Ship it', body: 'the detail', priority: 7, max_steps: 24,
    tools: ['web_search', 'filesystem'],
  });
});

it('sends a non-numeric number field as nothing rather than as text', () => {
  const form = document.createElement('form');
  form.innerHTML = '<input name="priority" value="soon">';
  expect(payloadFrom(form)).toEqual({});
});

it('sends an unticked checkbox as an explicit false', () => {
  const form = document.createElement('form');
  form.innerHTML = '<input name="task" value="tick"><input name="wake_agent" type="checkbox" value="true">';
  expect(payloadFrom(form)).toEqual({ task: 'tick', wake_agent: false });
  form.querySelector('input[type=checkbox]').checked = true;
  expect(payloadFrom(form)).toEqual({ task: 'tick', wake_agent: true });
});

it('carries the schedule delivery fields when they are chosen', () => {
  const form = document.createElement('form');
  form.innerHTML = '<input name="task" value="daily recap"><input name="schedule" value="every 1d">'
    + '<select name="deliver"><option value="">none</option><option value="email" selected>Email</option></select>'
    + '<input name="deliver_target" value="owner@example.com">';
  expect(payloadFrom(form)).toEqual({
    task: 'daily recap', schedule: 'every 1d',
    deliver: 'email', deliver_target: 'owner@example.com',
  });
});

it('no longer honours a data-read-only attribute nothing sets', () => {
  const form = document.createElement('form');
  form.dataset.readOnly = '1';
  document.body.append(form);
  bindCreate(form);
  expect(form.isConnected).toBe(true);
});
