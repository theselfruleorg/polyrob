/**
 * chats.js — the Chats overlay (043 C7).
 *
 * "Sessions" was a top-level destination in the old fifteen. It is not a
 * destination: it is a way back to a conversation you already had, which is
 * what a search button is for. So it is an overlay on the frame, reachable from
 * every screen, and it costs the nav nothing.
 *
 * Three rules it keeps:
 *
 * 1. **A row says WHO started it.** `creator` (043 A17) is the difference
 *    between a chat you had and a run the agent started by itself at 4am; a
 *    list that cannot tell them apart is a list you stop opening.
 * 2. **A row's state is one of five** (working, waiting for you, done, did
 *    not finish, stopped), derived from the session's own status, and an
 *    unrecognised status renders as UNKNOWN. A status nobody has seen before
 *    is not "done".
 * 3. **It says nothing it did not read.** A failed fetch renders the honest
 *    sentence, never an empty list — an empty list is a claim.
 *
 * Every word comes from the copy layer, handed over on `#chats-copy`'s data
 * attributes. There is no inline script on any 043 template
 * (tests/unit/webview/test_no_new_inline_script.py), so this is how a string
 * crosses from Python into JS.
 */

/**
 * Session status -> running | waiting | done | failed | stopped, or "unknown"
 * (070 W0.11). A suspended run waits for the owner; a failed run did not
 * finish, which is not the same as being stopped.
 */
export function statusOf(raw) {
  const status = String(raw || "").trim().toLowerCase();
  if (["running", "created", "initializing", "resumed"].includes(status)) return "running";
  if (["suspended", "waiting", "paused"].includes(status)) return "waiting";
  if (["completed", "done", "finished"].includes(status)) return "done";
  if (["failed", "error"].includes(status)) return "failed";
  if (["cancelled", "canceled", "stopped"].includes(status)) return "stopped";
  return "unknown";
}

/** The pill class for each state: done is neutral, failed is red, waiting amber. */
export function statusClass(state) {
  return {
    running: "is-running",
    waiting: "is-needs-you",
    done: "is-done",
    failed: "is-stopped",
    stopped: "is-stopped",
    unknown: "is-unknown",
  }[state] || "is-unknown";
}

/** The start time in the browser's own zone; the stored UTC text as a fallback. */
export function createdLabel(session) {
  const iso = session?.created_iso;
  if (iso) {
    const when = new Date(iso);
    if (!Number.isNaN(when.getTime())) {
      return when.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
    }
  }
  return session?.created || "";
}

/** Who started this session, in words. An unknown creator is never guessed. */
export function creatorLabel(session, copy) {
  const key = `creator_${String(session?.creator || "").trim().toLowerCase()}`;
  return (copy && copy[key]) || (copy && copy.creator_unknown) || "";
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** One row. Returns a DOM node, never a string — nothing here builds HTML. */
export function rowNode(session, copy) {
  const state = statusOf(session?.status);
  const row = el("a", "entry");
  row.href = `/c/${encodeURIComponent(session?.id || "")}`;
  row.dataset.state = state;
  row.appendChild(el("h3", "entry-title", session?.task || (copy && copy.untitled) || ""));

  const meta = el("p", "entry-meta");
  const pill = el("span", `pill ${statusClass(state)}`);
  pill.appendChild(el("span", "dot"));
  pill.appendChild(document.createTextNode((copy && copy[`status_${state}`]) || state));
  meta.appendChild(pill);
  meta.appendChild(document.createTextNode(" "));
  const who = el("span", "val", creatorLabel(session, copy));
  who.dataset.creator = String(session?.creator || "");
  meta.appendChild(who);
  const created = createdLabel(session);
  if (created) {
    meta.appendChild(document.createTextNode(" "));
    const when = el("span", "val", created);
    when.dataset.created = session?.created_iso || session?.created || "";
    meta.appendChild(when);
  }
  // 043 A32: a session whose live orchestrator runs in Rob's OWN process, not
  // this console. GET /api/webgate/chats — the ONE catalog since A12 deleted
  // the full-tree /api/sessions — annotates it `runtime:"agent"` (+owner_pid);
  // the chip says so, because a row that looks ordinary hides that the console
  // can watch it but not steer it. Any other runtime ("here"/"idle"/absent)
  // renders nothing — the chip is a fact, not a decoration.
  if (session?.runtime === "agent") {
    meta.appendChild(document.createTextNode(" "));
    const live = el("span", "val", (copy && copy.live_at_agent) || "");
    live.dataset.runtime = "agent";
    if (session.owner_pid != null && session.owner_pid !== "") {
      live.dataset.ownerPid = String(session.owner_pid);
    }
    meta.appendChild(live);
  }
  // A part of this row that would not read is ONE plain line; the raw reason
  // rides in the tooltip only, never in the row text (070 E.9).
  if (Object.keys(session?.unreadable || {}).length) {
    meta.appendChild(document.createTextNode(" "));
    const why = el("span", "unknown-why", (copy && copy.part_missing) || "");
    why.title = Object.values(session.unreadable).join("; ");
    meta.appendChild(why);
  }
  row.appendChild(meta);
  return row;
}

/**
 * Fill *list* from *sessions*. `sessions === null` means the read FAILED, which
 * is a different answer from an empty list and is rendered as one.
 */
export function render(list, state, sessions, copy) {
  list.replaceChildren();
  if (sessions === null) {
    state.textContent = (copy && copy.unreadable) || "";
    state.hidden = false;
    return "unreadable";
  }
  if (!sessions.length) {
    state.textContent = (copy && copy.empty) || "";
    state.hidden = false;
    return "empty";
  }
  state.hidden = true;
  sessions.forEach((session) => list.appendChild(rowNode(session, copy)));
  return "rows";
}

/** The copy the server handed over, as a plain object. */
export function copyFrom(node) {
  return node ? { ...node.dataset } : {};
}

const requests = new WeakMap();
export async function load(dialog, offset = 0, fetcher = fetch) {
  const request = {};
  requests.set(dialog, request);
  if (!offset) dialog.querySelector('[data-chats-more]')?.remove();
  const list = dialog.querySelector("#chats-list");
  const state = dialog.querySelector("#chats-state");
  const copy = copyFrom(document.getElementById("chats-copy"));
  state.textContent = copy.loading || "";
  state.hidden = false;
  try {
    const resp = await fetcher(`/api/webgate/chats?offset=${offset}&limit=50`, { credentials: "include" });
    if (!resp.ok) throw new Error(String(resp.status));
    const data = await resp.json();
    if (requests.get(dialog) !== request) return;
    if (!Array.isArray(data.sessions)) throw new Error('Invalid chat catalog');
    dialog.querySelector('[data-chats-more]')?.remove();
    if (!offset) render(list, state, Array.isArray(data.sessions) ? data.sessions : [], copy);
    else {
      const seen = new Set([...list.querySelectorAll('a.entry')].map(row => row.getAttribute('href')));
      data.sessions.forEach(session => {
        const row = rowNode(session, copy);
        if (!seen.has(row.getAttribute('href'))) { list.appendChild(row); seen.add(row.getAttribute('href')); }
      });
      state.hidden = true;
    }
    if (Object.keys(data.unreadable || {}).length) {
      state.hidden = false;
      state.textContent = copy.unreadable || '';
      console.debug('[chats] unreadable parts of the catalog', data.unreadable);
    }
    if (data.next_offset != null) {
      const more = el('button', 'btn', copy.more); more.type = 'button'; more.dataset.chatsMore = '1';
      more.addEventListener('click', () => { more.disabled = true; load(dialog, data.next_offset, fetcher); });
      list.after(more);
    }
  } catch (err) {
    if (requests.get(dialog) !== request) return;
    console.error("[chats] could not read the session list", err);
    if (!offset) render(list, state, null, copy);
    else { state.hidden = false; state.textContent = copy.unreadable || ""; }
    const more = dialog.querySelector("[data-chats-more]");
    if (more) more.disabled = false;
  }
}

function bind() {
  const dialog = document.getElementById("chats-dialog");
  const open = document.getElementById("chats-open");
  if (!dialog || !open) return;
  open.addEventListener("click", () => {
    // <dialog> brings the focus trap, the backdrop and Escape with it, so the
    // overlay uses the shared design tokens.
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    load(dialog);
  });
  const close = dialog.querySelector("#chats-close");
  if (close) close.addEventListener("click", () => dialog.close());
  // ⌘K / Ctrl-K is what the button's own hint promises.
  document.addEventListener("keydown", (event) => {
    if (event.key === "k" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      open.click();
    }
  });
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
}
