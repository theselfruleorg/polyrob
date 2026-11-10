/**
 * chat-open.js — the composer, on the cold open and on a bound session (043 C6).
 *
 * Two jobs, and only two:
 *
 * 1. **Cold open.** Bind the starters and the composer. A submit creates a
 *    session with nothing but the task, so `resolve_session_provider_model`
 *    picks the model the operator's runtime config already names — the model
 *    picker, the tool grid and the step slider are gone, not hidden.
 * 2. **Bound session.** Hand over to `transcript.js`, the 043 running transcript
 *    (narrated actions, the receipt, Stop and Steer). It reads the session off
 *    `document.body`'s data attributes, so this page still carries no inline
 *    script. The legacy `/static/js/session.js` + `/static/js/chat.js` binding
 *    it replaced — the 14k-line god-file inside the new shell — is gone.
 *
 * Nothing here writes copy: a message a person reads comes from the server
 * through the template, or from the module that already owns it.
 */
import { postJson, serverAnswer } from './http.js';
import { copyFrom as attachCopyFrom, held, sendHeld } from './file-attach.js';

const body = document.body;
const sessionId = body?.dataset?.sessionId || '';
const isNew = body?.dataset?.isNew === 'true' || !sessionId || sessionId === 'new';

/** The machine code of a refusal, wherever the server put it. */
function refusalCode(data) {
  return (data && (data.code || data.detail?.code || data.error?.code)) || '';
}

/**
 * 113 W5 (C-06, CH-3): the cold open's notes are a Transcript over
 * `#chat-open-notes`, so a verb answer carries the same note and card buttons
 * as in a chat, and a card tap runs here (there is no session to post to).
 * Built on first use; `transcript.js` loads socket.io only when it connects,
 * which the cold open never does.
 */
let openNotes = null;
async function notes(fetcher) {
  if (openNotes && openNotes.thread && openNotes.thread.isConnected) return openNotes;
  const thread = document.getElementById('chat-open-notes');
  if (!thread) return null;
  const mod = await import('./transcript.js');
  openNotes = new mod.Transcript(thread, mod.copyFrom(document.getElementById('transcript-copy')),
    { sessionId: '', fetcher });
  openNotes.send = (line) => startSession(line, { fetcher });
  return openNotes;
}

/** Create a session from one line of text and go to it. Resolves `{ok}`:
 *  true when the line was a verb the console answered here. */
export async function startSession(task, { fetcher } = {}) {
  const composer = document.getElementById('chat-composer');
  const send = document.getElementById('chat-send-btn');
  const input = document.getElementById('chat-input');
  if (composer?.getAttribute('aria-busy') === 'true') return { ok: false };
  if (send) send.disabled = true;
  if (composer) composer.setAttribute('aria-busy', 'true');
  try {
    // The console session is the HttpOnly same-origin cookie. Do not prefer a
    // legacy localStorage bearer over that cookie: a stale bearer wins the
    // server's header precedence and turns a valid owner session into the
    // misleading "Invalid token" refusal the owner used to see here.
    // 113 W6 (C-02): with a held file the chat is made unstarted, the file goes
    // to its folder, and the first line starts it (`startWithFiles`).
    if (held.files.length || held.sessionId) return await startWithFiles(task, { fetcher });
    const resp = await postJson('/api/task/sessions', { task, auto_start: true }, { fetcher });
    const data = resp.body || {};
    // 070 W0.13: this process has no language model. Say so, keep the text,
    // re-open Send, and stay on the page (nothing was created).
    if (resp.status === 503 && refusalCode(data) === 'no_model') {
      const tx = await notes(fetcher);
      if (tx) tx._noModelNote(composer?.dataset);
      if (send) send.disabled = false;
      if (composer) composer.removeAttribute('aria-busy');
      return { ok: false };
    }
    // A17: an owner verb typed on the COLD OPEN is answered inline by the same
    // verb plane a bound chat uses — it makes no session, so there is nothing
    // to navigate to. Before this the create route turned `/halt` into a TASK:
    // the console spun up a session whose job was to think about the word
    // "/halt" while nothing halted.
    if (typeof data.command_reply === 'string' && data.command_reply.trim()) {
      const tx = await notes(fetcher);
      if (tx) tx._cardButtons(tx._commandNote(data), task, data);
      if (input && input.value.trim() === task) input.value = '';
      if (send) send.disabled = false;
      if (composer) composer.removeAttribute('aria-busy');
      return { ok: true };
    }
    if (!resp.ok || !data.session_id) throw new Error(serverAnswer(data, composer?.dataset.unreachable));
    window.location.assign(`/c/${encodeURIComponent(data.session_id)}`);
    return { ok: true };
  } catch (err) {
    // The page keeps its own words; this only re-opens the door.
    if (send) send.disabled = false;
    if (composer) composer.removeAttribute('aria-busy');
    const status = document.getElementById('chat-create-status');
    if (status) status.textContent = err.message || composer?.dataset.unreachable;
    console.error('[chat] could not start a session', err);
    return { ok: false };
  }
}

/** Open the composer again after a refusal; the typed text stays. */
function reopen() {
  const send = document.getElementById('chat-send-btn');
  const composer = document.getElementById('chat-composer');
  if (send) send.disabled = false;
  if (composer) composer.removeAttribute('aria-busy');
}

/**
 * The start page with held files (113 W6, C-02): create the chat unstarted
 * (`wait_for_uploads`; with no model the console's create hop refuses first),
 * send the files, then send the line with them, which starts the chat. A file
 * that did not arrive keeps the page: its line says why, and the next Send
 * goes to the same chat.
 */
async function startWithFiles(task, { fetcher } = {}) {
  const composer = document.getElementById('chat-composer');
  const words = attachCopyFrom(document.getElementById('attach-copy'));
  const fail = (message) => {
    const status = document.getElementById('chat-create-status');
    if (status) status.textContent = message || composer?.dataset.unreachable || '';
    reopen();
    return { ok: false };
  };
  const noModel = async () => {
    const tx = await notes(fetcher);
    if (tx) tx._noModelNote(composer?.dataset);
    reopen();
    return { ok: false };
  };
  let id = held.sessionId;
  if (!id) {
    const resp = await postJson('/api/task/sessions',
      { task, auto_start: false, wait_for_uploads: true }, { fetcher });
    const data = resp.body || {};
    if (resp.status === 503 && refusalCode(data) === 'no_model') return noModel();
    if (typeof data.command_reply === 'string' && data.command_reply.trim()) {
      const tx = await notes(fetcher);
      if (tx) tx._cardButtons(tx._commandNote(data), task, data);
      reopen();
      return { ok: true };
    }
    if (!resp.ok || !data.session_id) return fail(serverAnswer(data, composer?.dataset.unreachable));
    id = data.session_id;
  }
  const sent = await sendHeld(id, words, { fetcher });
  if (!sent.ok) return fail('');
  const res = await postJson(`/api/session/${encodeURIComponent(id)}/messages`,
    { text: task, kind: 'comment', metadata: {}, attached_files: sent.paths }, { fetcher });
  if (res.status === 503 && refusalCode(res.body) === 'no_model') return noModel();
  if (!res.ok) return fail(serverAnswer(res.body || {}, composer?.dataset.unreachable));
  window.location.assign(`/c/${encodeURIComponent(id)}`);
  return { ok: true };
}

function bindColdOpen() {
  const input = document.getElementById('chat-input');
  const form = document.getElementById('chat-composer');
  if (!form || !input) return;

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const task = input.value.trim();
    if (task) startSession(task);
  });

  // Enter sends; shift-enter is a newline. The same rule the REPL follows.
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event('submit'));
    }
  });

  document.querySelectorAll('.starter').forEach((button) => {
    button.addEventListener('click', () => {
      input.value = button.textContent.trim();
      input.focus();
    });
  });
}

/**
 * Hand a bound session to the running transcript.
 * Imported dynamically so the cold open never pays for socket.io.
 */
async function bindSession() {
  try {
    const mod = await import('./transcript.js');
    if (mod.mount) mod.mount();
  } catch (err) {
    const status = document.getElementById('chat-create-status');
    if (status) status.textContent = document.getElementById('chat-composer')?.dataset.unreachable || '';
    console.error('[chat] the running transcript did not load', err);
  }
}

if (isNew) {
  bindColdOpen();
} else {
  bindSession();
}
