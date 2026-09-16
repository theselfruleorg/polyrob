/**
 * http.js — the ONE way the 043 console asks the server to change something.
 *
 * ⚠️ There is no CSRF TOKEN in this console, and code that pretends otherwise
 * is worse than code that says nothing: it reads as a defence that is there.
 * `webgate.csrf_guard` is a **same-origin check** — on a mutating method it
 * compares the request's `Origin` (else `Referer`) host against its own `Host`,
 * and `Host` is a browser-forbidden header, so a cross-origin page cannot make
 * the two agree. Nothing mints a `polyrob_csrf` cookie and nothing reads an
 * `X-CSRF-Token` header.
 *
 * So what a mutation needs is exactly what a browser already does: a same-origin
 * `fetch` with a non-GET method, which the fetch spec makes carry `Origin`
 * automatically. This module exists so both callers do that identically, and so
 * the next one does not re-invent a token.
 *
 * ⚠️ The guard's "no header AND no cookie" branch is a deliberate PASS for
 * non-browser clients (curl, a script). It is not a hole this file can widen or
 * close — it is the server's rule, stated here so a reader of the client does
 * not conclude the client is what enforces it.
 *
 * ⚠️ `postJson` takes caller `headers` so a MULTITENANT seat can send its
 * `Authorization: Bearer …`. That header is an IDENTITY — the token the auth
 * layer issued to that tenant — and it is NOT a CSRF mechanism, of which this
 * console still has none. Reading it as one is how a reader concludes a
 * defence exists here that does not.
 */

// ⚠️ Import this as `./http.js`, never `/static/app/http.js`. A browser
// resolves both, but the dev rig's bundler reads a leading `/` as the REPO
// root and fails to resolve it — so the absolute form is a module that works
// in production and cannot be tested.

/** Headers for a JSON mutation. `Origin` is the browser's to send, not ours. */
export function jsonHeaders() {
  return { 'Content-Type': 'application/json' };
}

/**
 * POST JSON and return the parsed body, never throwing on a refusal.
 *
 * Returns `{ok, status, body}`: `ok` is the HTTP result, `body` is whatever the
 * endpoint answered (or `null` when it answered no JSON at all). A caller
 * decides what to SAY — this only carries what came back.
 *
 * `headers` is merged OVER `jsonHeaders()`, so a caller wins a collision. The
 * intended caller is a multitenant seat sending `Authorization` (see the header
 * note at the top of this file — no caller passes headers today).
 * `Content-Type` stays `application/json` unless a caller deliberately
 * restates it, and the body is always
 * `JSON.stringify`ed either way. The cookie rides on every call regardless —
 * `credentials: 'include'` is what the single-tenant console uses and this does
 * not replace it.
 */
export async function postJson(url, payload, options = {}) {
  return requestJson("POST", url, payload, options);
}

export async function patchJson(url, payload, options = {}) {
  return requestJson("PATCH", url, payload, options);
}

async function requestJson(method, url, payload, { fetcher, headers } = {}) {
  const call = fetcher || fetch;
  const init = {
    method,
    // A fresh object per call: one seat's token must never land on the next
    // seat's request.
    headers: { ...jsonHeaders(), ...(headers || {}) },
    credentials: 'include',
  };
  if (payload !== undefined) init.body = JSON.stringify(payload);
  const resp = await call(url, init);
  let body = null;
  try {
    body = await resp.json();
  } catch (err) {
    body = null;
  }
  return { ok: Boolean(resp.ok), status: resp.status, body };
}

/** Preserve the server refusal and its actionable detail. */
export function serverAnswer(body, fallback = '') {
  if (!body) return fallback;
  if (body.message || body.error) {
    const message = String(body.message || body.error);
    return typeof body.detail === 'string' && body.detail !== message ? `${message} ${body.detail}` : message;
  }
  if (typeof body.detail === 'string') return body.detail;
  return JSON.stringify(body);
}
