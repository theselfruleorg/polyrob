/**
 * chat-column.js — the chat column on the Chat destination (113 W6, W2 item 6, IA-1).
 *
 * At 1024 px and wider the Chat destination (`/` and `/c/<id>`) shows YOUR
 * console chats in a left column: the main chat first, then the separate chats
 * by last activity (`chat.active_iso`, else when they began). A row is the
 * chat's name and its state pill; the open one carries `aria-current="page"`.
 * Below 1024 px nothing is read and nothing is drawn: the Chats overlay (⌘K)
 * stays the way in. The rows come from `GET /api/webgate/chats` (the rows that
 * carry `chat`, from `chats_routes.annotate`) and the words from `#chats-copy`,
 * the same node and the same state words as the overlay (`chats.js`).
 *
 * It says nothing it did not read: a failed read is the overlay's "could not
 * load" line, never an empty column.
 */
import { copyFrom, statusClass, statusOf } from "./chats.js";
import { onLiveChange } from "./live.js";

export const WIDE = "(min-width: 1024px)";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** Your console chats, main first, then by last activity (newest first). */
export function columnChats(sessions) {
  const mine = (Array.isArray(sessions) ? sessions : []).filter((s) => s && s.chat);
  const when = (s) => Date.parse(s.chat.active_iso || s.created_iso || "") || 0;
  return mine.sort((a, b) => (b.chat.main === true) - (a.chat.main === true) || when(b) - when(a));
}

/** One row: the chat's name and its state pill; the open chat is aria-current. */
export function columnRow(session, copy, currentId) {
  const state = statusOf(session.status);
  const row = el("a", "chat-column-row");
  row.href = `/c/${encodeURIComponent(session.id || "")}`;
  row.dataset.state = state;
  if (currentId && String(session.id) === String(currentId)) row.setAttribute("aria-current", "page");
  const name = session.chat.label || (session.chat.main ? copy.main : "") || session.task || copy.untitled || "";
  row.appendChild(el("span", "chat-column-name", name));
  const pill = el("span", `pill ${statusClass(state)}`);
  pill.appendChild(el("span", "dot"));
  pill.appendChild(document.createTextNode(copy[`status_${state}`] || state));
  row.appendChild(pill);
  return row;
}

/** Fill the column's list; `sessions === null` = the read failed. */
export function renderColumn(list, state, sessions, copy, currentId) {
  list.replaceChildren();
  if (sessions === null) {
    state.textContent = copy.unreadable || "";
    state.hidden = false;
    return "unreadable";
  }
  const rows = columnChats(sessions);
  if (!rows.length) {
    state.textContent = copy.empty || "";
    state.hidden = false;
    return "empty";
  }
  state.hidden = true;
  rows.forEach((s) => {
    const item = el("li");
    item.appendChild(columnRow(s, copy, currentId));
    list.appendChild(item);
  });
  return "rows";
}

export async function loadColumn(root, { fetcher = fetch, doc = document } = {}) {
  const list = root.querySelector("#chat-column-list");
  const state = root.querySelector("#chat-column-state");
  const copy = copyFrom(doc.getElementById("chats-copy"));
  const current = doc.body?.dataset?.sessionId || "";
  try {
    const resp = await fetcher("/api/webgate/chats?offset=0&limit=50", { credentials: "include" });
    if (!resp.ok) throw new Error(String(resp.status));
    const data = await resp.json();
    if (!Array.isArray(data.sessions)) throw new Error("Invalid chat catalog");
    return renderColumn(list, state, data.sessions, copy, current);
  } catch (err) {
    console.error("[chat-column] could not read your chats", err);
    return renderColumn(list, state, null, copy, current);
  }
}

export function mount({ doc = document, win = globalThis.window } = {}) {
  const root = doc.getElementById("chat-column");
  if (!root || !win || typeof win.matchMedia !== "function") return null;
  const wide = win.matchMedia(WIDE);
  let read = false;
  const refresh = () => { if (wide.matches) { read = true; loadColumn(root, { doc }); } };
  if (typeof wide.addEventListener === "function") {
    wide.addEventListener("change", () => { if (wide.matches && !read) refresh(); });
  }
  onLiveChange(() => { if (read) refresh(); }, { doc });
  refresh();
  return root;
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => mount());
  else mount();
}
