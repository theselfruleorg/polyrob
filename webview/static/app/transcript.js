/**
 * transcript.js — the running transcript (043 A14 / A16, Stop and Steer).
 *
 * This is what `/c/{id}` shows once a session is bound: the turns, the narrated
 * actions, and the receipt the turn's Stop and Steer sit on. It REPLACES the
 * legacy `/static/js/chat.js` + `/static/js/session.js` binding that chat-open.js
 * used to import — the 14k-line god-file rendered inside the new shell — with one
 * module that reads the same feed and draws the mockup (docs/design/040/web/
 * chat-running.html) using only classes app.css already carries.
 *
 * Three rules it keeps:
 *
 * 1. **One narrator, and it is not here.** The human line for an action is
 *    computed ONCE in Python (agents/task/telemetry/narrate.py) and shipped on
 *    the feed event as `data.narration`. This module renders that string; it
 *    never re-implements the narrator. If a legacy event arrives without one, it
 *    falls back to the scrubbed `result_preview`, then to a name-free copy line
 *    — never a machine name, never a `[step]` trace token.
 * 2. **It says nothing it did not read.** No thread element → it does nothing.
 *    An unknown cost is a dash, not `$0.00`. A status it has never seen leaves
 *    the receipt where it was rather than claiming "done".
 * 3. **Stop and Steer add no authority.** Stop is the existing cancel endpoint;
 *    Steer is the existing message endpoint. Both ride `http.js::postJson`
 *    (`credentials:'include'`, same-origin — see that file for why there is no
 *    CSRF token). Neither exists on a read-only console.
 *
 * Every user-visible word comes from the copy layer, handed over on the hidden
 * `#transcript-copy` node's data attributes — the same way chats.js reads its
 * words, because no 043 template carries an inline script.
 */
import { loadSocketIo } from './socket-client.js';
import { postJson, serverAnswer } from './http.js';

/** The feed event types this transcript draws, grouped by how it draws them. */
const USER_TYPES = new Set(['user_message']);
const REPLY_TYPES = new Set(['agent_message', 'final_message', 'result']);
const TOOL_TYPES = new Set(['tool_result', 'tool_execution', 'tool_started']);
const COST_TYPES = new Set(['llm_request']);
const STATUS_TYPES = new Set(['status']);

/** Session statuses that end a turn, split by which receipt state they mean. */
const DONE_STATUS = new Set(['completed', 'done', 'finished']);
const STOPPED_STATUS = new Set(['failed', 'cancelled', 'canceled', 'error', 'stopped', 'suspended']);

/** MM:SS for an act's own elapsed clock, matching the mockup's `0:04` / `1:12`. */
export function mmss(totalSeconds) {
  const s = Math.max(0, Math.floor(Number(totalSeconds) || 0));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}:${String(rem).padStart(2, '0')}`;
}

/** "1m 12s" / "41s" for the receipt line, matching the mockup's phrasing. */
export function elapsedWords(ms) {
  const s = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem ? `${m}m ${rem}s` : `${m}m`;
}

/** A cost the transcript can HONESTLY show, or the dash when it read none. */
export function costLabel(cost, dash) {
  if (typeof cost !== 'number' || !Number.isFinite(cost)) return dash || '—';
  return `$${cost.toFixed(2)}`;
}

/**
 * The one line for an action. Server-computed narration first, then the scrubbed
 * preview a legacy event carries, then a name-free copy fallback. NEVER a raw
 * tool id and NEVER a repr — that is the narrator's contract, honoured on the
 * fallback path too.
 */
export function narrationLine(data, copy) {
  const d = data || {};
  const narration = typeof d.narration === 'string' ? d.narration.trim() : '';
  if (narration) return narration;
  const preview = typeof d.result_preview === 'string' ? d.result_preview.trim() : '';
  if (preview) {
    // One line only: a preview can be multi-line, and the transcript row is one.
    const line = preview.split(/\r?\n/, 1)[0].trim();
    if (line) return line;
  }
  return (copy && (d.success === false ? copy.act_failed : copy.act_did_work)) || '';
}

/** The receipt state class for a session status, or `null` when it is unknown. */
function statusToState(raw) {
  const s = String(raw || '').trim().toLowerCase();
  if (DONE_STATUS.has(s)) return 'done';
  if (STOPPED_STATUS.has(s)) return 'stopped';
  return null; // still working, or a status we have never seen — leave it be
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/**
 * Fill a receipt template (`Working {elapsed}, {actions} actions so far,
 * {cost}.`) into a fragment, wrapping each substituted value in a `.val` span
 * the way the mockup does — so the copy stays the SSOT and the emphasis is the
 * design system's, not a second string.
 */
function fillReceipt(template, values) {
  const frag = document.createDocumentFragment();
  const parts = String(template || '').split(/(\{[a-z_]+\})/g);
  for (const part of parts) {
    const m = /^\{([a-z_]+)\}$/.exec(part);
    if (m && Object.prototype.hasOwnProperty.call(values, m[1])) {
      const span = el('span', 'val', values[m[1]]);
      frag.appendChild(span);
    } else if (part) {
      frag.appendChild(document.createTextNode(part));
    }
  }
  return frag;
}

/** The copy the server handed over on `#transcript-copy`, as a plain object. */
export function copyFrom(node) {
  return node ? { ...node.dataset } : {};
}

/**
 * An event's time in ONE unit (seconds), for ordering the backfill.
 *
 * ⚠️ The feed dir mixes two schemes: telemetry writes `{seq}_{type}.json` with a
 * float-seconds `timestamp`, and `SessionManager.add_to_feed` writes
 * `{type}_{ms}.json`; the backfill endpoint sorts by FILENAME, which clusters by
 * TYPE rather than time, and it derives an INTEGER-MS `timestamp` for a file that
 * carries none. So a raw timestamp sort would mix seconds and ms. A real Unix
 * second is ~1.7e9 and a millisecond one ~1.7e12 — normalise the ms case down.
 */
export function chronoTs(event) {
  let ts = Number(event && event.timestamp);
  if (!Number.isFinite(ts)) return 0;
  if (ts > 1e12) ts = ts / 1000; // a filename-derived ms timestamp → seconds
  return ts;
}

/** The monotonic sequence when the event carries one (telemetry does; an
 *  add_to_feed event does not), else 0 — a tiebreaker, never the primary key. */
export function chronoSeq(event) {
  const s = Number(event && event._seq);
  return Number.isFinite(s) ? s : 0;
}

/**
 * The running transcript over one thread element.
 *
 * `apply(event)` is the whole ingress: a feed event (from the HTTP backfill or a
 * live `feed_update`) goes in, the DOM changes. It is idempotent — the backfill
 * and the socket can carry the same event, and applying it twice does not
 * double-draw (a bubble is keyed by timestamp+text, an action by its call id).
 */
export class Transcript {
  constructor(thread, copy, opts = {}) {
    this.thread = thread;
    this.copy = copy || {};
    this.sessionId = opts.sessionId || '';
    this.readOnly = Boolean(opts.readOnly);
    this.fetcher = opts.fetcher; // forwarded to postJson; undefined = real fetch
    this.now = opts.now || (() => Date.now());
    this.input = opts.input || null; // the composer textarea, for Steer
    this.seen = new Set(); // bubble idempotency (timestamp:text)
    this.synthetic = 0; // act key when an event carries no call_id
    this.turn = null; // the open rob turn, or null between turns
    this.remoteNotice = null; // the one 409 "live in the agent" line, reused
  }

  apply(event) {
    if (!this.thread || !event || typeof event !== 'object') return;
    const type = event.type;
    const data = event.data || {};
    const ts = event.timestamp;
    if (USER_TYPES.has(type)) return this._user(data, ts);
    if (REPLY_TYPES.has(type)) return this._reply(data, ts);
    if (TOOL_TYPES.has(type)) return this._tool(data);
    if (COST_TYPES.has(type)) return this._cost(data);
    if (STATUS_TYPES.has(type)) return this._status(data);
    // Everything else (step, planner, evaluation, …) is not drawn in the
    // transcript — the acts and the reply are the visible turn.
  }

  /**
   * Apply a backfilled batch CHRONOLOGICALLY.
   *
   * ⚠️ The backfill arrives filename-sorted, which clusters by event type, not
   * time — so a reopened chat would draw the reply before the question. Order by
   * normalised timestamp (seconds), with the sequence as a tiebreaker; the sort
   * is stable, so same-key events keep the endpoint's order.
   */
  applyAll(events) {
    (events || [])
      .slice()
      .sort((a, b) => (chronoTs(a) - chronoTs(b)) || (chronoSeq(a) - chronoSeq(b)))
      .forEach((event) => this.apply(event));
  }

  _user(data, ts) {
    const text = String(data.text || data.message || '').trim();
    if (!text) return;
    const key = `u:${ts}:${text}`;
    if (this.seen.has(key)) return;
    this.seen.add(key);
    const turn = el('div', 'turn turn-you');
    turn.appendChild(el('div', 'bubble', text));
    this.thread.appendChild(turn);
    // A new person-turn closes the agent's turn: the next action opens a fresh
    // one, with its own clock, count and receipt.
    this.turn = null;
  }

  _ensureTurn() {
    if (this.turn) return this.turn;
    const root = el('div', 'turn turn-rob');
    const said = el('div', 'said');
    const acts = document.createElement('div');
    acts.style.marginTop = 'var(--s4)';
    const receipt = el('p', 'receipt');
    receipt.hidden = true; // shown once the turn has something to receipt
    root.appendChild(said);
    root.appendChild(acts);
    root.appendChild(receipt);
    this.thread.appendChild(root);
    this.turn = {
      root, said, acts, receipt,
      actNodes: new Map(),
      bubbles: new Set(),
      count: 0,
      cost: null,
      start: this.now(),
      state: 'working',
    };
    return this.turn;
  }

  _reply(data, ts) {
    const text = String(data.text || data.message || data.result || data.response || '').trim();
    if (!text) return;
    const key = `a:${ts}:${text}`;
    if (this.seen.has(key)) return;
    this.seen.add(key);
    const turn = this._ensureTurn();
    // R2 backstop: a byte-identical repeat bubble within a turn is suppressed
    // (the same rule cli/ui/rich_renderer.py keeps).
    const norm = text.toLowerCase();
    if (turn.bubbles.has(norm)) return;
    turn.bubbles.add(norm);
    turn.said.appendChild(el('p', null, text));
    this._receipt();
  }

  _tool(data) {
    const turn = this._ensureTurn();
    const callId = data.call_id != null ? String(data.call_id) : `#${this.synthetic++}`;
    const success = data.success;
    const phase = success === true ? 'done' : success === false ? 'stopped' : 'running';
    const seconds = Math.max(0, (this.now() - turn.start) / 1000);
    // The typed `tool_result` carries the server-narrated line; the untyped
    // `tool_execution` does not. Both collapse to one row by call id, and the
    // live feed watcher can deliver them in either order in one batch.
    const hasNarration = typeof data.narration === 'string' && data.narration.trim() !== '';

    let node = turn.actNodes.get(callId);
    if (!node) {
      node = el('div', 'act');
      node.appendChild(el('span', 'act-what', narrationLine(data, this.copy)));
      node.appendChild(el('span', 'act-time val', mmss(seconds)));
      turn.acts.appendChild(node);
      turn.actNodes.set(callId, node);
      turn.count += 1;
    } else {
      // ⚠️ Do NOT downgrade a narrated line: a `tool_execution` (no narration)
      // applied AFTER the narrated `tool_result` must not blank it back to the
      // raw preview. Only overwrite when the incoming event brings narration.
      if (hasNarration) node.querySelector('.act-what').textContent = narrationLine(data, this.copy);
      node.querySelector('.act-time').textContent = mmss(seconds);
    }
    node.className = phase === 'running' ? 'act is-running'
      : phase === 'stopped' ? 'act is-stopped'
        : 'act';
    this._receipt();
  }

  _cost(data) {
    const raw = data.cost_estimate != null ? data.cost_estimate
      : data.cost != null ? data.cost
        : data.cost_usd;
    const n = Number(raw);
    if (!Number.isFinite(n)) return;
    const turn = this._ensureTurn();
    turn.cost = (turn.cost || 0) + n;
    this._receipt();
  }

  _status(data) {
    const state = statusToState(data.status || data.state);
    if (!state || !this.turn) return;
    this.turn.state = state;
    this._receipt();
  }

  /** Redraw the current turn's receipt from its own counters. */
  _receipt() {
    const turn = this.turn;
    if (!turn) return;
    const tpl = turn.state === 'done' ? this.copy.receipt_done
      : turn.state === 'stopped' ? this.copy.receipt_stopped
        : this.copy.receipt_working;
    const values = {
      elapsed: elapsedWords(this.now() - turn.start),
      actions: turn.count,
      cost: costLabel(turn.cost, this.copy.cost_unknown),
    };
    turn.receipt.replaceChildren(fillReceipt(tpl, values));
    turn.receipt.hidden = false;
    // Stop and Steer live on the receipt, not in a toolbar — and only while the
    // turn is live and the console may touch it.
    if (turn.state === 'working' && !this.readOnly) {
      const stop = el('button', 'btn-quiet btn', this.copy.stop || 'Stop');
      stop.type = 'button';
      stop.style.padding = '2px 8px';
      stop.style.marginLeft = '6px';
      stop.addEventListener('click', () => this.stop());
      const steer = el('button', 'btn-quiet btn', this.copy.steer || 'Steer');
      steer.type = 'button';
      steer.style.padding = '2px 8px';
      steer.addEventListener('click', () => this.steer());
      turn.receipt.appendChild(document.createTextNode(' '));
      turn.receipt.appendChild(stop);
      turn.receipt.appendChild(steer);
    }
  }

  /** Stop → the existing cancel endpoint. Optimistic, like the legacy path. */
  async stop() {
    const url = `/api/task/sessions/${encodeURIComponent(this.sessionId)}/cancel`;
    try {
      const res = await postJson(url, undefined, { fetcher: this.fetcher });
      // A32: a 409 is not a failure — the session is live in Rob's OWN process,
      // not this console, so the console cannot stop it from here. Render the
      // honest "live in the agent" line (same as send()), NEVER a silent no-op.
      if (res && res.status === 409) {
        this._remoteNotice(res.body);
        return;
      }
      if (!res.ok) this._errorNotice(res.body);
      if (res.ok && this.turn) {
        this.turn.state = 'stopped';
        this._receipt();
      }
    } catch (err) {
      this._errorNotice(null);
      console.error('[transcript] stop failed', err);
    }
  }

  /** Steer → send the composer's text to the live session, or focus it. */
  async steer() {
    const text = this.input ? String(this.input.value || '').trim() : '';
    if (!text) {
      if (this.input) this.input.focus();
      return;
    }
    const draft = this.input?.value;
    const res = await this.send(text);
    if (res?.ok && this.input?.value === draft) this.input.value = '';
  }

  /** Send one message to the live session (the composer's own submit path). */
  async send(text) {
    if (this.sending) return { ok: false, pending: true };
    this.sending = true;
    const url = `/api/session/${encodeURIComponent(this.sessionId)}/messages`;
    try {
      const res = await postJson(url, { text, kind: 'comment', metadata: {} }, { fetcher: this.fetcher });
      // A16: a leading owner verb is handled by the console's own verb plane
      // (`console_commands.maybe_handle_console_command`) and answered inline
      // as `command_reply` — it never reaches the agent, so no feed event ever
      // arrives for it. Nothing rendered that field, so `/halt` typed into the
      // chat box acted and then looked exactly like a message that vanished.
      this._commandNote(res && res.body);
      // A32: a 409 is not a failure — the session is live in Rob's OWN process,
      // not this console, so the console cannot steer it from here. Render the
      // honest "live in the agent" line (with owner_pid + a retry hint), NEVER a
      // failure bubble. LOCAL/here sessions never see this.
      if (res && res.status === 409) this._remoteNotice(res.body);
      else if (!res.ok) this._errorNotice(res.body);
      return res;
    } catch (err) {
      this._errorNotice(null);
      console.error('[transcript] send failed', err);
      return { ok: false, status: 0, body: null };
    } finally { this.sending = false; }
  }

  /**
   * The one 409 line: this chat is live in the agent process, so the console
   * cannot steer it. It reuses a single `.note` node (repeated 409s replace it,
   * they do not stack) and is NOT a `.turn-rob`/`.said` bubble — a 409 is a
   * state, not the agent speaking, and not the `chat.failed.*` family. The
   * owner_pid rides on the guard's `detail` either shape it can arrive in
   * (a bare FastAPI 409 → `{detail:{owner_pid}}`, or the console's send wrapper
   * → `{error, detail:{owner_pid}}`); an unknown pid renders a dash, never a
   * confident number.
   */
  _readNotice(kind, message) {
    if (!this.thread) return;
    let notice = this.thread.querySelector(`[data-read-notice="${kind}"]`);
    if (!message) { notice?.remove(); return; }
    if (!notice) {
      notice = el('p', 'note'); notice.dataset.readNotice = kind;
      notice.setAttribute('role', 'status'); this.thread.appendChild(notice);
    }
    notice.textContent = message;
  }

  /**
   * A16 — the inline answer to an owner verb typed into the chat box.
   *
   * `console_commands.maybe_handle_console_command` runs `/halt`, `/pending`,
   * `/status` and friends on the SHARED owner-verb plane and answers
   * `{"success": true, "command_reply": "…"}`; the verb never reaches the
   * agent, so no feed event is ever written for it. Rendered as a `.note` in
   * the thread — NOT a `.turn-rob`/`.said` bubble, because the console
   * answered, not the agent. Each verb leaves its own answer rather than
   * replacing the previous one.
   */
  _commandNote(body) {
    if (!this.thread || !body) return null;
    const reply = typeof body.command_reply === 'string' ? body.command_reply.trim() : '';
    if (!reply) return null;
    const note = el('p', 'note');
    note.dataset.commandReply = '1';
    note.setAttribute('role', 'status');
    note.textContent = reply;
    this.thread.appendChild(note);
    return note;
  }

  _errorNotice(body) {
    if (!this.thread) return;
    if (!this.errorNotice) {
      this.errorNotice = el('p', 'note');
      this.errorNotice.setAttribute('role', 'status');
      this.thread.appendChild(this.errorNotice);
    }
    this.errorNotice.textContent = serverAnswer(body, this.copy.action_unavailable);
  }

  _remoteNotice(body) {
    if (!this.thread) return;
    const detail = (body && body.detail && typeof body.detail === 'object')
      ? body.detail : (body || {});
    const pid = detail.owner_pid != null ? detail.owner_pid
      : (body ? body.owner_pid : undefined);
    const pidLabel = (pid != null && pid !== '') ? String(pid) : '—';
    const line = String(this.copy.live_at_agent || '').replace('{pid}', pidLabel);
    if (!this.remoteNotice) {
      this.remoteNotice = el('p', 'note');
      this.thread.appendChild(this.remoteNotice);
    }
    this.remoteNotice.textContent = line;
    this.remoteNotice.hidden = false;
  }
}

// --------------------------------------------------------------------- wiring
// Below here is the live wiring — socket.io + the HTTP backfill + the composer.
// It is deliberately thin and is exercised by a running console, not the unit
// rig: the rig drives `Transcript.apply` directly (that is where the logic is).

async function backfill(transcript, sessionId) {
  try {
    const resp = await fetch(
      `/api/session/${encodeURIComponent(sessionId)}/feed/events?limit=300`,
      { credentials: 'include' },
    );
    if (!resp.ok) { transcript._readNotice('history', transcript.copy.history_unavailable); return; }
    const data = await resp.json();
    if (!Array.isArray(data.events)) throw new Error('Invalid feed');
    transcript.applyAll(data.events);
    transcript._readNotice('history', '');
  } catch (err) {
    transcript._readNotice('history', transcript.copy.history_unavailable);
    console.error('[transcript] backfill failed', err);
  }
}

async function connect(transcript, sessionId) {
  let io;
  try {
    io = await loadSocketIo();
  } catch (err) {
    transcript._readNotice('live', transcript.copy.live_unavailable);
    console.error('[transcript] no live updates', err);
    return;
  }
  const socket = io({
    path: '/socket.io',
    transports: ['polling', 'websocket'],
    reconnection: true,
  });
  socket.on('connect', () => {
    transcript._readNotice('live', '');
    socket.emit('join_session', { session_id: sessionId });
  });
  socket.on('disconnect', () => transcript._readNotice('live', transcript.copy.live_unavailable));
  socket.on('connect_error', () => transcript._readNotice('live', transcript.copy.live_unavailable));
  socket.on('error', body => transcript._readNotice('live', serverAnswer(body, transcript.copy.live_unavailable)));
  socket.on('feed_update', (item) => transcript.apply(item));
}

/**
 * Mount the transcript on a bound session. Returns the `Transcript` instance (or
 * `null` when there is no thread — the cold open, or the ownership-unknown
 * state, which render no thread and want no socket).
 */
export function mount() {
  const body = document.body;
  const thread = document.getElementById('chat-messages');
  if (!thread) return null;
  const sessionId = body?.dataset?.sessionId || '';
  if (!sessionId || sessionId === 'new') return null;
  const readOnly = body?.dataset?.readOnly === 'true';
  const input = document.getElementById('chat-input');
  const copy = copyFrom(document.getElementById('transcript-copy'));
  const transcript = new Transcript(thread, copy, { sessionId, readOnly, input });

  // The composer on a bound session is Steer: it talks to the running agent.
  const form = document.getElementById('chat-composer');
  if (form && input && !readOnly) {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      const draft = input.value;
      const res = await transcript.send(text);
      if (res?.ok && input.value === draft) input.value = '';
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event('submit'));
      }
    });
  }

  backfill(transcript, sessionId).then(() => connect(transcript, sessionId));
  return transcript;
}
