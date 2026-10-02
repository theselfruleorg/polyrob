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
import { fmtUsd } from './format.js';

/** The feed event types this transcript draws, grouped by how it draws them. */
const USER_TYPES = new Set(['user_message', 'user_message_during_execution']);
/** A follow-up typed while a run works is written as
 *  `user_message_during_execution`, its text cut at this many characters
 *  (agents/task/agent/hitl_manager.py). */
const DURING_CUT = 200;
/** Two owner bubbles with the same text inside this window are one message
 *  (the console's own follow-up writes both event types). */
const USER_DEDUPE_MS = 60000;
const REPLY_TYPES = new Set(['agent_message', 'final_message', 'result']);
const TOOL_TYPES = new Set(['tool_result', 'tool_execution', 'tool_started']);
const COST_TYPES = new Set(['llm_request']);
const STATUS_TYPES = new Set(['status']);
/** The events that END a turn: `done` writes `task_complete` (scheme B), a
 *  session end writes `session_completion` (scheme A). */
const TURN_CLOSE_TYPES = new Set(['task_complete', 'session_completion']);

/**
 * The controller verbs are bookkeeping, never a step (070 W0.6): `send_message`
 * IS the reply (its text arrives as `agent_message`) and `done` closes the turn.
 * `message` (the outbound tool) is a real step and is NOT here.
 */
const CONTROLLER_VERBS = new Set(['send_message', 'done']);

/** True for a tool event of a controller verb — no row, no count. */
export function isControllerVerb(data) {
  const d = data || {};
  return CONTROLLER_VERBS.has(String(d.action_name || d.name || '').trim().toLowerCase());
}
/** A console verb's answer that arrived LATER (a `/send … go` or `/bridge … go`
 *  runs in the background; `console_commands._console_deliver` pushes its
 *  result on the live socket). Drawn exactly like the inline `command_reply`. */
const COMMAND_REPLY_TYPES = new Set(['command_reply']);

/** Action cards (core/surfaces/cards.py). A card's tap token. */
const CARD_TOKEN = /\/card_[0-9a-f]{10}_(?:ok|no|re|[1-6])(?![\w])/g;
/** The verbs whose console answer may be a card: the confirmable money verbs
 *  and `/cards`. Buttons are EXPLICIT, never inferred from arbitrary text — an
 *  answer to any other verb (`/thread` quotes the agent's own words) gets none,
 *  exactly the Telegram rule in core/surfaces/actions.py. */
const CARD_VERBS = new Set(['/send', '/swap', '/bridge', '/pay', '/claim', '/launch',
  '/deploy', '/nft', '/identity', '/writeoff', '/unquarantine', '/wallet']);
// ⚠️ `/cards` is deliberately absent: its answer lists each card's title, and a
// choice card's title is the MODEL's question. The Inbox draws those buttons
// from the store instead.

/**
 * The card taps a console answer offers, in order: `[{token, act}]` (at most 8:
 * a builder step shows up to 6 options plus Cancel).
 * Only for an answer to a card verb, or to a card tap itself.
 */
export function cardTaps(sentText, reply) {
  const verb = String(sentText || '').trim().split(/\s+/)[0].toLowerCase();
  if (!(CARD_VERBS.has(verb) || /^\/card_[0-9a-f]{10}_/.test(verb))) return [];
  const out = [];
  const seen = new Set();
  for (const m of String(reply || '').matchAll(CARD_TOKEN)) {
    if (seen.has(m[0])) continue;
    seen.add(m[0]);
    out.push({ token: m[0], act: m[0].split('_').pop() });
    if (out.length >= 8) break;   // 6 options + Cancel on a builder step
  }
  return out;
}

/** The button words for one act — from the server's copy node, never here. */
export function cardLabel(act, copy) {
  if (act === 'ok') return copy.card_ok || act;
  if (act === 're') return copy.card_re || act;
  if (act === 'no') return copy.card_no || act;
  return String(copy.card_pick || '{n}').replace('{n}', act);
}

/** Session statuses, split by the receipt state they mean (070 W0.7).
 *  `suspended` is a `send_message(wait_for_response=True)` pause: Rob waits for
 *  the owner's answer — it is NOT stopped. */
const WORKING_STATUS = new Set(['running', 'resumed', 'created', 'initializing']);
const DONE_STATUS = new Set(['completed', 'done', 'finished']);
const WAITING_STATUS = new Set(['suspended', 'waiting', 'paused']);
const FAILED_STATUS = new Set(['failed', 'error']);
const STOPPED_STATUS = new Set(['cancelled', 'canceled', 'stopped']);

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
  // FE11: a sub-cent cost is written out, never a confident `$0.00`.
  return fmtUsd(cost);
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

/** The receipt state for a session status:
 *  `working|done|waiting|failed|stopped`, or `null` for a status never seen. */
export function statusToState(raw) {
  const s = String(raw || '').trim().toLowerCase();
  if (WORKING_STATUS.has(s)) return 'working';
  if (DONE_STATUS.has(s)) return 'done';
  if (WAITING_STATUS.has(s)) return 'waiting';
  if (FAILED_STATUS.has(s)) return 'failed';
  if (STOPPED_STATUS.has(s)) return 'stopped';
  return null; // a status we have never seen — leave the receipt be
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
      span.dataset.slot = m[1];
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

/**
 * An event's own time in ms — the clock every act offset and every receipt
 * duration is drawn from (070 W0.5). A backfill applies a whole chat in one
 * tick, so the render time is the wrong clock: it drew every act at `0:00`
 * and every receipt as `0s`. `fallbackMs` (the render time) is used only for
 * an event that carries no timestamp at all.
 */
export function eventMs(event, fallbackMs) {
  const ts = chronoTs(event);
  return ts > 0 ? ts * 1000 : fallbackMs;
}

/** The receipt's step count in words: "no steps", "1 step", "3 steps". */
export function stepsWords(count, copy) {
  const n = Number(count) || 0;
  if (n <= 0) return (copy && copy.steps_none) || '0';
  if (n === 1) return (copy && copy.steps_one) || '1';
  return String((copy && copy.steps_many) || '{count}').replace('{count}', String(n));
}

/** The receipt's cost sentence: "Cost $0.02." or "Cost not known yet." */
export function costWords(cost, copy) {
  if (typeof cost !== 'number' || !Number.isFinite(cost)) return (copy && copy.cost_unknown) || '';
  return String((copy && copy.cost_known) || '{amount}').replace('{amount}', costLabel(cost));
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
    this.userBubbles = new Map(); // owner bubble key -> {at, node, during}
    // 070 W0.9: every telemetry event reaches the socket twice (the fast push
    // and the feed watcher), and the backfill may carry it too — one `_id`,
    // one row. Scheme B events have no `_id` and keep their own keys.
    this.seenIds = new Set();
    // Live events that arrive while the backfill loads wait here, then apply
    // in arrival order after it (`flush`). `null` = not buffering.
    this.buffer = null;
    this.synthetic = 0; // act key when an event carries no call_id
    this.turn = null; // the open rob turn, or null between turns
    this.remoteNotice = null; // the one 409 "live in the agent" line, reused
    // False while a backfill is applied; true once the socket is live. A live
    // working turn counts up to now; otherwise a receipt ends at the turn's
    // last event.
    this.live = Boolean(opts.live);
    // 070 E.13: is a live process running this session (the page's
    // `data-run-live`)? `false` = the run ended; `null` = not known. A turn
    // still working after the backfill of a run that is not live never got its
    // closing event: it reads "Stopped … without a final word", not "Working
    // for …" forever.
    this.runLive = opts.runLive === true || opts.runLive === false ? opts.runLive : null;
  }

  apply(event) {
    if (!this.thread || !event || typeof event !== 'object') return;
    if (event._id) {
      if (this.seenIds.has(event._id)) return;
      this.seenIds.add(event._id);
    }
    const type = event.type;
    const data = event.data || {};
    const ts = event.timestamp;
    if (USER_TYPES.has(type)) return this._user(data, ts, event, type);
    if (REPLY_TYPES.has(type)) return this._reply(data, ts, event);
    if (TOOL_TYPES.has(type)) return this._tool(data, event);
    if (COST_TYPES.has(type)) return this._cost(data, event);
    if (STATUS_TYPES.has(type)) return this._status(data, event);
    if (TURN_CLOSE_TYPES.has(type)) return this._close(event, 'done');
    if (COMMAND_REPLY_TYPES.has(type)) {
      const text = String(data.text || '').trim();
      const key = `c:${ts}:${text}`;
      if (!text || this.seen.has(key)) return null;
      this.seen.add(key);
      return this._commandNote({ command_reply: text });
    }
    // Everything else (step, planner, evaluation, …) is not drawn in the
    // transcript — the acts and the reply are the visible turn.
  }

  /** Start holding live events (the room is joined before the backfill). */
  startBuffer() { this.buffer = []; }

  /** A live `feed_update`: held while the backfill loads, else applied. */
  receive(event) {
    if (this.buffer) this.buffer.push(event);
    else this.apply(event);
  }

  /** The backfill is drawn: apply the held live events in arrival order, stop
   *  buffering, and let a working turn count up to now. */
  flush() {
    const held = this.buffer || [];
    this.buffer = null;
    held.forEach((event) => this.apply(event));
    this.live = true;
    if (this.runLive === false && this.turn && this.turn.state === 'working') {
      this._close(null, 'ended');
    }
    if (this.turn) this._receipt();
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

  /**
   * 070 W0.12: draw the stored first line (the owner's question from
   * `task.json`) as the first bubble, BEFORE the backfill. A later
   * `user_message` with the same text is absorbed once, whatever its time.
   */
  seedFirstLine(text) {
    const line = String(text || '').trim();
    if (!line || !this.thread) return;
    const key = `u:${line.replace(/\s+/g, ' ').slice(0, DURING_CUT)}`;
    if (this.userBubbles.has(key)) return;
    const turn = el('div', 'turn turn-you');
    const bubble = el('div', 'bubble', line);
    turn.appendChild(bubble);
    this.thread.appendChild(turn);
    this.userBubbles.set(key, { at: 0, node: bubble, during: false, seed: true, typed: new Set() });
  }

  _user(data, ts, event, type) {
    let text = String(data.text || data.message || data.message_text || '').trim();
    if (!text) return;
    const during = type === 'user_message_during_execution';
    if (during && text.length === DURING_CUT) text += '…';
    // One message, one bubble: the key is the first 200 characters of the
    // whitespace-normalised text, and a repeat within a minute is the same
    // message arriving as both event types (or twice from the socket).
    const key = `u:${text.replace(/…$/, '').replace(/\s+/g, ' ').slice(0, DURING_CUT)}`;
    const at = eventMs(event, this.now());
    const prior = this.userBubbles.get(key);
    if (prior && prior.seed) {
      // The stored first line already drew this message: take its clock once.
      prior.seed = false;
      prior.at = at;
      if (event && event._id) prior.typed.add(type);
      return;
    }
    // FE6: two events of the SAME type with distinct `_id`s are two messages
    // ("yes" to two asks), however close in time. The window still merges the
    // during-execution copy with its `user_message` (two types, two ids) and
    // id-less events; one `_id` seen twice never reaches here (`seenIds`).
    const distinct = Boolean(event && event._id) && prior && prior.typed.has(type);
    if (prior && !distinct && Math.abs(at - prior.at) <= USER_DEDUPE_MS) {
      if (event && event._id) prior.typed.add(type);
      // The cut during-execution copy gives way to the full user_message text.
      if (prior.during && !during && text.length > prior.node.textContent.length) {
        prior.node.textContent = text;
        prior.during = false;
      }
      return;
    }
    // A new person-turn closes the agent's turn: the next action opens a fresh
    // one, with its own clock, count and receipt. A turn still working when the
    // person wrote reads "… before your message", never a live Stop or Steer.
    this._close(event, this.turn && this.turn.state === 'working' ? 'so_far'
      : (this.turn && this.turn.state));
    const turn = el('div', 'turn turn-you');
    const bubble = el('div', 'bubble', text);
    turn.appendChild(bubble);
    this.thread.appendChild(turn);
    this.userBubbles.set(key, { at, node: bubble, during,
      typed: new Set(event && event._id ? [type] : []) });
  }

  /** Close the open turn in *state*: its receipt drawn once more, without
   *  Stop or Steer, and the next event opens a new turn. */
  _close(event, state) {
    const turn = this.turn;
    if (!turn) return;
    // Only a turn still working runs up to the closing event; a turn already
    // done or stopped keeps the time it ended.
    if (event && (turn.state === 'working' || turn.state === 'waiting')) this._at(event);
    if (state) turn.state = state;
    this._receipt();
    this.turn = null;
    this.lastTurn = turn;
  }

  /** The ms an event happened at, and the open turn's `last` moved up to it. */
  _at(event) {
    const at = eventMs(event, this.now());
    if (this.turn && at > this.turn.last) this.turn.last = at;
    return at;
  }

  _ensureTurn(atMs) {
    if (this.turn) return this.turn;
    const at = Number.isFinite(atMs) ? atMs : this.now();
    const root = el('div', 'turn turn-rob');
    const said = el('div', 'said');
    const acts = document.createElement('div');
    acts.style.marginTop = 'var(--s4)';
    const receipt = el('p', 'receipt');
    receipt.hidden = true; // shown once the turn has something to receipt
    // DOM order: the steps, then the reply they led to, then the receipt.
    root.appendChild(acts);
    root.appendChild(said);
    root.appendChild(receipt);
    this.thread.appendChild(root);
    this.turn = {
      root, said, acts, receipt,
      actNodes: new Map(),
      bubbles: new Set(),
      count: 0,
      cost: null,
      start: at,
      last: at,
      state: 'working',
    };
    return this.turn;
  }

  _reply(data, ts, event) {
    const text = String(data.text || data.message || data.result || data.response || '').trim();
    if (!text) return;
    const key = `a:${ts}:${text}`;
    if (this.seen.has(key)) return;
    this.seen.add(key);
    const at = eventMs(event, this.now());
    const turn = this._ensureTurn(at);
    this._at(event);
    // R2 backstop: a byte-identical repeat bubble within a turn is suppressed
    // (the same rule cli/ui/rich_renderer.py keeps).
    const norm = text.toLowerCase();
    if (turn.bubbles.has(norm)) return;
    turn.bubbles.add(norm);
    turn.said.appendChild(el('p', null, text));
    this._receipt();
  }

  _tool(data, event) {
    if (isControllerVerb(data)) return;
    const at = eventMs(event, this.now());
    const turn = this._ensureTurn(at);
    this._at(event);
    const callId = data.call_id != null ? String(data.call_id) : `#${this.synthetic++}`;
    const success = data.success;
    const phase = success === true ? 'done' : success === false ? 'stopped' : 'running';
    // The offset from the turn's first event; a later event for the same call
    // moves the row to the LATEST time of that call (its end).
    const seconds = Math.max(0, (at - turn.start) / 1000);
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
      if (!(node.lastAt > at)) node.querySelector('.act-time').textContent = mmss(seconds);
    }
    node.lastAt = Math.max(node.lastAt || 0, at);
    node.className = phase === 'running' ? 'act is-running'
      : phase === 'stopped' ? 'act is-stopped'
        : 'act';
    this._receipt();
  }

  _cost(data, event) {
    const raw = data.cost_estimate != null ? data.cost_estimate
      : data.cost != null ? data.cost
        : data.cost_usd;
    const n = Number(raw);
    if (!Number.isFinite(n)) return;
    // The cost of a model call often lands just AFTER `done` closed the turn:
    // it belongs to that turn, never to a new empty one.
    if (!this.turn && this.lastTurn) {
      this.lastTurn.cost = (this.lastTurn.cost || 0) + n;
      this._receipt(this.lastTurn);
      return;
    }
    const turn = this._ensureTurn(eventMs(event, this.now()));
    this._at(event);
    turn.cost = (turn.cost || 0) + n;
    this._receipt();
  }

  _status(data, event) {
    const state = statusToState(data.status || data.state);
    if (!state) return;
    // Kept even with no open turn, so a status before the first step is not lost.
    this.lastStatus = state;
    const turn = this.turn;
    if (!turn) {
      // A run that failed before Rob drew anything says so, once. A `stopped`
      // with no open turn (a REPL /exit after finished turns) draws nothing.
      if (state === 'failed') this._failedLine();
      return;
    }
    this._at(event);
    if (state === 'failed') return this._close(event, 'failed');
    // A working status re-opens a waiting turn; it never re-opens an ended one,
    // and a later stop never rewrites a turn that already finished.
    if (state === 'working' && turn.state !== 'waiting') return;
    if (state === 'stopped' && turn.state === 'done') return;
    turn.state = state;
    this._receipt();
  }

  /** "This chat failed before Rob could answer." — once, and only while Rob
   *  has drawn no turn in this thread (otherwise Rob did answer). */
  _failedLine() {
    if (!this.thread || this.thread.querySelector('.turn-rob')) return;
    if (this.thread.querySelector('[data-read-notice="failed"]')) return;
    const line = el('p', 'note is-failed', this.copy.turn_failed || '');
    line.dataset.readNotice = 'failed';
    line.setAttribute('role', 'status');
    this.thread.appendChild(line);
  }

  /** Redraw the current turn's receipt from its own counters. */
  _receipt(turn = this.turn) {
    if (!turn) return;
    const tpl = turn.state === 'done' ? this.copy.receipt_done
      : turn.state === 'stopped' ? this.copy.receipt_stopped
        : turn.state === 'so_far' ? this.copy.receipt_so_far
          : turn.state === 'waiting' ? this.copy.receipt_waiting
            : turn.state === 'failed' ? this.copy.receipt_failed
              : turn.state === 'ended' ? this.copy.receipt_ended
                : this.copy.receipt_working;
    const open = turn.state === 'working' || turn.state === 'waiting';
    const end = (open && this.live) ? this.now() : turn.last;
    const values = {
      elapsed: elapsedWords(end - turn.start),
      steps: stepsWords(turn.count, this.copy),
      cost: costWords(turn.cost, this.copy),
    };
    turn.receipt.replaceChildren(fillReceipt(tpl, values));
    turn.receipt.hidden = false;
    // Stop and Steer live on the receipt, not in a toolbar — and only while the
    // turn is live and the console may touch it.
    if (open && turn === this.turn && !this.readOnly) {
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

  /** FE12: advance a live open turn's elapsed time in place (the page calls
   *  this every second). Only the `{elapsed}` value changes — the receipt is
   *  not rebuilt, so a focused Stop or Steer button keeps its focus. */
  tick() {
    const turn = this.turn;
    if (!turn || !this.live || !turn.receipt) return;
    if (turn.state !== 'working' && turn.state !== 'waiting') return;
    const slot = turn.receipt.querySelector('[data-slot="elapsed"]');
    if (slot) slot.textContent = elapsedWords(this.now() - turn.start);
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
      const note = this._commandNote(res && res.body);
      this._cardButtons(note, text, res && res.body);
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

  /**
   * Action cards: the taps a money quote (or `/cards`) answer offers, drawn as
   * buttons under its note. A button SENDS the tap token as if typed — the
   * console's verb plane runs it with every gate — and the buttons go away
   * once one is used, so a card is never tapped twice from here.
   */
  _cardButtons(note, sentText, body) {
    if (!note || !body) return null;
    const taps = cardTaps(sentText, body.command_reply);
    if (!taps.length) return null;
    const row = el('div', 'entry-actions');
    row.dataset.cardTaps = '1';
    for (const tap of taps) {
      const b = el('button', `btn${tap.act === 'ok' ? ' btn-primary' : ''}`,
        cardLabel(tap.act, this.copy));
      b.type = 'button';
      b.dataset.token = tap.token;
      b.addEventListener('click', async () => {
        row.querySelectorAll('button').forEach((x) => { x.disabled = true; });
        const res = await this.send(tap.token);
        if (res && res.ok) row.remove();
        else row.querySelectorAll('button').forEach((x) => { x.disabled = false; });
      });
      row.appendChild(b);
    }
    note.after(row);
    return row;
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

/** The live-notice words for a socket refusal, by its code (070 E.31). */
export function liveNotice(body, copy) {
  const code = body && body.code;
  if (code === 'rate_limited' && copy && copy.live_busy) return copy.live_busy;
  return (copy && copy.live_unavailable) || '';
}

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

/**
 * Bind the session socket's events to *transcript*. Exported for tests.
 *
 * FE1: a RE-connect after the backfill was drawn holds live events again,
 * rejoins, and backfills again (`refill`) before it flushes — a reply written
 * while the laptop slept, the Wi-Fi dropped or the server redeployed is drawn;
 * the `_id` dedupe keeps what is already on screen to one row. A connect while
 * a backfill is still loading only rejoins: that backfill covers the gap.
 *
 * FE18: the live notice clears only when the server ACKS a join it did not
 * refuse (the refusal arrives before the ack on the same socket) — never when
 * the join is merely sent.
 */
export function wireSession(socket, transcript, sessionId, opts = {}) {
  const refill = opts.refill || (() => Promise.resolve());
  const settle = opts.settle || (() => {});
  const later = opts.later || setTimeout;
  let refusals = 0;
  const join = () => {
    const before = refusals;
    socket.emit('join_session', { session_id: sessionId }, () => {
      if (refusals === before) transcript._readNotice('live', '');
    });
  };
  // Hold live events, backfill again, then flush — the `_id` dedupe keeps
  // what is already drawn to one row. A backfill already loading covers it.
  const resync = () => {
    if (transcript.buffer !== null) return false;
    transcript.startBuffer();
    Promise.resolve()
      .then(() => refill())
      .catch((err) => console.error('[transcript] re-backfill failed', err))
      .finally(() => transcript.flush());
    return true;
  };
  socket.on('connect', () => {
    if (transcript.buffer === null) {
      resync();
      join();
    } else {
      join();
    }
    settle();
  });
  // WS4: the server's per-room limit dropped `feed_update`s and says so ONCE
  // per gap. Wait out its window (bounded), then refill — one pending refill
  // at a time, however many gaps arrive meanwhile.
  let gapPending = false;
  socket.on('feed_gap', (body) => {
    const room = body && body.session_id ? String(body.session_id) : '';
    if (room && room !== sessionId && !sessionId.endsWith(room) && !room.endsWith(sessionId)) return;
    if (gapPending) return;
    gapPending = true;
    const asked = Number(body && body.retry_after);
    const wait = Number.isFinite(asked) && asked > 0 ? Math.min(asked, 300) : 60;
    later(() => { gapPending = false; resync(); }, wait * 1000);
  });
  socket.on('disconnect', () => transcript._readNotice('live', transcript.copy.live_unavailable));
  socket.on('connect_error', () => {
    transcript._readNotice('live', transcript.copy.live_unavailable);
    settle();
  });
  // 070 E.31: a refusal carries a code; the console chooses the words and
  // never shows the server's message.
  socket.on('error', (body) => {
    refusals += 1;
    transcript._readNotice('live', liveNotice(body, transcript.copy));
    // 070 W0.17: the socket stays on a rate limit; join again after the window.
    if (body && body.code === 'rate_limited') {
      const wait = Number(body.retry_after) > 0 ? Number(body.retry_after) : 60;
      later(join, wait * 1000);
    }
  });
  socket.on('feed_update', (item) => transcript.receive(item));
}

/** Join the session room. Resolves once the socket first connects (the room
 *  is joined on connect) or fails, or after 3 s — the backfill then runs, and
 *  every live event that arrives meanwhile is held by `transcript.buffer`. */
async function connect(transcript, sessionId) {
  let io;
  try {
    io = await loadSocketIo();
  } catch (err) {
    transcript._readNotice('live', transcript.copy.live_unavailable);
    console.error('[transcript] no live updates', err);
    return;
  }
  let settle;
  const first = new Promise((resolve) => { settle = resolve; });
  setTimeout(() => settle(), 3000);
  const socket = io({
    path: '/socket.io',
    transports: ['polling', 'websocket'],
    reconnection: true,
  });
  wireSession(socket, transcript, sessionId, {
    settle: () => settle(),
    refill: () => backfill(transcript, sessionId),
  });
  await first;
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
  const liveAttr = body?.dataset?.runLive;
  const runLive = liveAttr === 'true' ? true : liveAttr === 'false' ? false : null;
  const transcript = new Transcript(thread, copy, { sessionId, readOnly, input, runLive });
  if (body?.dataset?.runKind === 'chat') transcript.seedFirstLine(body.dataset.firstLine);

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

  // Join first, then backfill: nothing written while the page loads is lost,
  // and an event in both the backfill and the room draws once (`_id`).
  // FE12: "Working for 3s" must count while the turn runs.
  setInterval(() => transcript.tick(), 1000);
  transcript.startBuffer();
  connect(transcript, sessionId)
    .then(() => backfill(transcript, sessionId))
    .finally(() => transcript.flush());
  return transcript;
}
