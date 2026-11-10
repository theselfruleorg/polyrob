/**
 * state.js — THE empty state a module draws (113 W5, ST-2).
 *
 * The same markup as `templates/_state.html::state` (the server-rendered twin):
 * an optional glyph, a title, body lines, the one action, and — only when a
 * read named a reason — that reason behind a disclosure. Every word arrives
 * already resolved through the copy layer (a module's `data-` attributes); this
 * file holds none.
 */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/**
 * `{glyph, title, body: string|string[], actions: [{href, label, primary}],
 *   why, reason}` → one `.state` node. Empty parts are left out, so a state
 * with no title is a body line, never an empty heading.
 */
export function stateBlock(spec = {}) {
  const state = el("div", "state");
  if (spec.glyph) {
    const glyph = el("div", "state-glyph", spec.glyph);
    glyph.setAttribute("aria-hidden", "true");
    state.appendChild(glyph);
  }
  if (spec.title) state.appendChild(el("h2", "state-title", spec.title));
  const body = Array.isArray(spec.body) ? spec.body : [spec.body];
  body.filter(Boolean).forEach((line) => state.appendChild(el("p", "state-body", line)));
  const actions = (spec.actions || []).filter((a) => a && a.href && a.label);
  if (actions.length) {
    const row = el("div", "state-actions");
    actions.forEach((a) => {
      const link = el("a", a.primary ? "btn btn-primary" : "btn", a.label);
      link.href = a.href;
      row.appendChild(link);
    });
    state.appendChild(row);
  }
  if (spec.reason) {
    const details = el("details");
    details.appendChild(el("summary", null, spec.why || ""));
    details.appendChild(el("p", "entry-meta", spec.reason));
    state.appendChild(details);
  }
  return state;
}
