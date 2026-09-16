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

const body = document.body;
const sessionId = body?.dataset?.sessionId || '';
const isNew = body?.dataset?.isNew === 'true' || !sessionId || sessionId === 'new';

/** Create a session from one line of text and go to it. */
async function startSession(task) {
  const composer = document.getElementById('chat-composer');
  const send = document.getElementById('chat-send-btn');
  if (composer?.getAttribute('aria-busy') === 'true') return;
  if (send) send.disabled = true;
  if (composer) composer.setAttribute('aria-busy', 'true');
  try {
    // The console session is the HttpOnly same-origin cookie. Do not prefer a
    // legacy localStorage bearer over that cookie: a stale bearer wins the
    // server's header precedence and turns a valid owner session into the
    // misleading "Invalid token" refusal the owner used to see here.
    const resp = await postJson('/api/task/sessions', { task, auto_start: true });
    const data = resp.body || {};
    if (!resp.ok || !data.session_id) throw new Error(serverAnswer(data, composer?.dataset.unreachable));
    window.location.assign(`/c/${encodeURIComponent(data.session_id)}`);
  } catch (err) {
    // The page keeps its own words; this only re-opens the door.
    if (send) send.disabled = false;
    if (composer) composer.removeAttribute('aria-busy');
    const status = document.getElementById('chat-create-status');
    if (status) status.textContent = err.message || composer?.dataset.unreachable;
    console.error('[chat] could not start a session', err);
  }
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
