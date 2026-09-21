/**
 * file-attach.js — the chat composer's file picker (2026-09-21 audit, A24).
 *
 * ⚠️ The defect this closes: `webview/README.md` described a picker whose
 * `accept=` is rendered from the ONE upload allowlist, and `chat.html` carried
 * no picker at all. The legacy `static/js/file-attachments.js` that used to
 * drive one was loaded by nothing, bound to element ids no 043 template has,
 * and carried its OWN 10 MB cap — a second copy of a limit the server owns. So
 * the control was restored here, minimal, rather than that module revived.
 *
 * Four rules, the same ones every other 043 module keeps:
 *
 * 1. **One allowlist, and it is the server's.** The `accept=` attribute is
 *    rendered from `core.surfaces.inbound_attachments.upload_accept_attribute`
 *    (template global `upload_accept`), and the per-file cap and the MIME sniff
 *    are enforced at `/api/task/sessions/{id}/workspace/upload`. This file
 *    carries no list and no size of its own — a second copy is how the template
 *    came to offer extensions the endpoint refused.
 * 2. **It says what came back, verbatim.** A refusal renders the endpoint's own
 *    `detail`; a file that did not reach the server renders the copy layer's
 *    one sentence. A silent failure is exactly what a picker may not do.
 * 3. **It draws no verb it cannot act on.** The control exists only on a bound
 *    session on a writable console — the upload route is session-scoped, so on
 *    the cold open there is no folder to put a file in yet.
 * 4. **Every word comes from the copy layer**, on `#attach-copy`'s data
 *    attributes; no 043 template carries an inline script.
 */

/** The copy the server handed over, as a plain object. */
export function copyFrom(node) {
  return node ? { ...node.dataset } : {};
}

/** Fill every `{name}` from a vars object. Copy owns the words. */
export function format(template, vars) {
  return String(template || "").replace(/\{(\w+)\}/g, (m, key) =>
    vars && Object.prototype.hasOwnProperty.call(vars, key)
      ? String(vars[key]) : m);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/**
 * Send ONE file to this session's workspace. Returns `{ok, message}` and never
 * throws: a thrown error on a picker is a file that vanished with no answer.
 *
 * ⚠️ `Content-Type` is deliberately unset — the browser must write the
 * multipart boundary itself, and naming the type here produces a body the
 * server cannot parse. `credentials: 'include'` carries the console's
 * same-origin cookie, and the same-origin `Origin` header is what
 * `webgate.csrf_guard` actually checks (see http.js).
 */
export async function uploadOne(sessionId, file, copy, opts = {}) {
  const call = opts.fetcher || fetch;
  const form = new FormData();
  form.append("file", file);
  try {
    const resp = await call(
      `/api/task/sessions/${encodeURIComponent(sessionId)}/workspace/upload`,
      { method: "POST", body: form, credentials: "include" });
    let body = null;
    try { body = await resp.json(); } catch (err) { body = null; }
    if (!resp.ok) {
      const detail = body && (body.detail || body.message || body.error);
      return {
        ok: false,
        message: detail ? String(detail)
          : format(copy && copy.attach_failed, { name: file.name }),
      };
    }
    const where = (body && (body.path || body.filename)) || file.name;
    return { ok: true, message: format(copy && copy.attach_done, { name: where }) };
  } catch (err) {
    console.error("[attach] the file did not reach the console", err);
    return { ok: false, message: (copy && copy.attach_unreachable) || "" };
  }
}

// --------------------------------------------------------------------- wiring

export function mount(root) {
  const doc = root || document;
  const button = doc.getElementById("chat-attach");
  const input = doc.getElementById("chat-file");
  const list = doc.getElementById("chat-attached");
  if (!button || !input) return null;
  const copy = copyFrom(doc.getElementById("attach-copy"));
  const sessionId = copy.session_id || "";
  if (!sessionId) return null;

  button.addEventListener("click", () => input.click());
  input.addEventListener("change", async () => {
    const files = Array.from(input.files || []);
    input.value = ""; // so the same file can be picked again after a refusal
    for (const file of files) {
      const line = el("p", "entry-meta",
        format(copy.attach_sending, { name: file.name }));
      if (list) list.appendChild(line);
      const { ok, message } = await uploadOne(sessionId, file, copy);
      line.textContent = message;
      line.dataset.ok = ok ? "true" : "false";
    }
  });
  return true;
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => mount());
  } else {
    mount();
  }
}
