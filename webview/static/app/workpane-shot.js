/**
 * workpane-shot.js — the chat's last browser screenshot in the Work pane (113 W6, C-11).
 *
 * Reads `GET /api/session/{id}/screenshot` (the newest `screenshots/*.png` of this chat)
 * and draws it from `GET /api/session/{id}/screenshot/file?ts=…`. The reader's `page_url`
 * is not shown: it is rebuilt from a file name that lost the URL's dots and slashes, so it
 * would name a page that does not exist. No screenshot is one line, not an empty box; a read that
 * failed says so. It re-reads on the console's live beat (`live.js::onLiveChange`).
 * Words come from `#workpane-shot-copy`.
 */
import { onLiveChange } from "./live.js";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

/** Draw one answer of the screenshot reader. Returns the state drawn. */
export function renderShot(root, body, copy, sessionId) {
  root.replaceChildren();
  if (!body || body.status !== "ok") {
    root.appendChild(el("p", "state-body", copy.unreadable || ""));
    return "unreadable";
  }
  if (body.is_placeholder || !body.url) {
    root.appendChild(el("p", "state-body", copy.none || ""));
    return "none";
  }
  const expected = `/api/session/${encodeURIComponent(sessionId)}/screenshot/file`;
  if (!String(body.url).startsWith(expected)) {   // only this chat's own file is drawn
    root.appendChild(el("p", "state-body", copy.unreadable || ""));
    return "unreadable";
  }
  const link = el("a", "shot-link");
  link.href = body.url;
  link.target = "_blank";
  link.rel = "noopener";
  const img = el("img", "shot");
  img.src = body.url;
  img.alt = copy.alt || "";
  img.loading = "lazy";
  link.appendChild(img);
  root.appendChild(link);
  return "shot";
}

export async function loadShot(root, copy, sessionId, fetcher = fetch) {
  let body = null;
  try {
    const resp = await fetcher(`/api/session/${encodeURIComponent(sessionId)}/screenshot`, { credentials: "include" });
    body = await resp.json().catch(() => null);
    if (!resp.ok) body = null;
  } catch (err) {
    console.error("[workpane-shot] the screenshot read did not reach the console", err);
    body = null;
  }
  return renderShot(root, body, copy, sessionId);
}

export function mount(doc = document) {
  const root = doc.getElementById("workpane-shot");
  const sessionId = doc.body?.dataset?.sessionId || "";
  if (!root || !sessionId || sessionId === "new") return null;
  const copy = { ...(doc.getElementById("workpane-shot-copy")?.dataset || {}) };
  const read = () => loadShot(root, copy, sessionId);
  onLiveChange(read, { doc });
  read();
  return root;
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => mount());
  else mount();
}
