/**
 * agent-model.js — Agent › Settings › the model for new chats (113 W6, G-4).
 *
 * Reads `GET /api/webgate/model` (the owner's choice for new chats, its source,
 * names to pick from) and writes `POST /api/webgate/model` — the server runs the
 * `/model <name> default` pipeline every seat uses, with no chat named, so a chat
 * already open keeps its model. The pipeline's own answer is shown verbatim. No
 * control is drawn when the server says it cannot set it (read-only, or not the
 * owner console). A choice that would not read is its own entry, never the
 * operator default shown as yours. Words come from `#agent-model-copy`.
 */
import { postJson } from "./http.js";
import { stateBlock } from "./state.js";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function fill(template, vars) {
  return String(template || "").replace(/\{(\w+)\}/g, (m, k) => (vars && vars[k] != null ? String(vars[k]) : m));
}

/** Draw the read into `root`; `save(body)` posts one change. Returns the state. */
export function renderModel(root, body, copy, save) {
  root.replaceChildren();
  const head = el("div", "section-head");
  head.appendChild(el("h2", "section-title", copy.title || ""));
  head.appendChild(el("span", "section-aside", copy.aside || ""));
  root.appendChild(head);
  if (!body || body.readable !== true) {
    root.appendChild(stateBlock({ body: copy.unreadable, why: copy.why, reason: (body && body.error) || "" }));
    return "unreadable";
  }
  const entry = el("div", "entry");
  entry.appendChild(el("p", "entry-body", fill(copy.now, { model: body.model || copy.provider_default || "",
    provider: body.provider || "" })));
  if (body.source) entry.appendChild(el("p", "entry-meta", fill(copy.source, { source: body.source })));
  root.appendChild(entry);
  const tiers = renderTiers(body, copy, save);
  if (body.can_set !== true || typeof save !== "function") { root.appendChild(tiers); return "read"; }

  const form = el("form", "token-trust-form model-form");
  const label = el("label", "token-field", copy.label || "");
  const input = el("input");
  input.type = "text"; input.name = "model"; input.autocomplete = "off";
  input.setAttribute("list", "agent-model-names");
  label.appendChild(input);
  const names = el("datalist");
  names.id = "agent-model-names";
  (body.suggestions || []).forEach((n) => { const o = el("option"); o.value = n; names.appendChild(o); });
  const use = el("button", "btn", copy.save || ""); use.type = "submit";
  const reset = el("button", "btn btn-quiet", copy.reset || ""); reset.type = "button";
  const status = el("p", "why"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
  form.append(label, names, use);
  if (body.mine) form.appendChild(reset);
  root.append(form, status, tiers);
  const run = async (payload) => {
    use.disabled = reset.disabled = true;
    const out = await save(payload);
    use.disabled = reset.disabled = false;
    status.textContent = out.message || copy.unreachable || "";
    if (out.ok && out.now) {
      entry.firstChild.textContent = fill(copy.now, { model: out.now.model || copy.provider_default || "",
        provider: out.now.provider || "" });
      if (entry.children[1] && out.now.source) entry.children[1].textContent = fill(copy.source, { source: out.now.source });
      input.value = "";
    }
    return out;
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const name = input.value.trim();
    if (name) run({ name });
  });
  reset.addEventListener("click", () => run({ reset: true }));
  return "form";
}

/**
 * 121: the heavy and light models and what the agent may choose — the lines
 * the bare `/model` shows on every seat (`body.settings`), and, where the owner
 * can set them, the `/model heavy|light <name>`, `… reset` and
 * `/model choice free|listed` controls. The pipeline's own answer is shown.
 */
export function renderTiers(body, copy, save) {
  const sec = el("div", "entry model-tiers");
  sec.appendChild(el("p", "entry-title", copy.tiers_title || ""));
  const lines = el("pre", "section-content");
  lines.textContent = (Array.isArray(body && body.settings) ? body.settings : []).join("\n");
  sec.appendChild(lines);
  if (!body || body.can_set !== true || typeof save !== "function") return sec;
  const form = el("form", "token-trust-form model-form");
  const label = el("label", "token-field", copy.tier_label || "");
  const input = el("input");
  input.type = "text"; input.name = "tier_model"; input.autocomplete = "off";
  input.setAttribute("list", "agent-model-names");
  label.appendChild(input);
  const status = el("p", "why"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
  const buttons = [];
  const run = async (payload) => {
    buttons.forEach((b) => { b.disabled = true; });
    const out = await save(payload);
    buttons.forEach((b) => { b.disabled = false; });
    status.textContent = out.message || copy.unreachable || "";
    if (out.ok && Array.isArray(out.settings)) lines.textContent = out.settings.join("\n");
    if (out.ok) input.value = "";
  };
  const button = (text, payload, needsName) => {
    const b = el("button", needsName ? "btn" : "btn btn-quiet", text || "");
    b.type = "button";
    b.addEventListener("click", () => {
      const name = input.value.trim();
      if (needsName && !name) { input.focus(); return; }
      run(needsName ? { ...payload, name } : payload);
    });
    buttons.push(b);
    return b;
  };
  form.append(label,
    button(copy.tier_heavy, { tier: "heavy" }, true),
    button(copy.tier_light, { tier: "light" }, true),
    button(copy.tier_reset_heavy, { tier: "heavy", reset: true }, false),
    button(copy.tier_reset_light, { tier: "light", reset: true }, false),
    button(copy.choice_free, { choice: "free" }, false),
    button(copy.choice_listed, { choice: "listed" }, false));
  form.addEventListener("submit", (event) => event.preventDefault());
  sec.append(form, status);
  return sec;
}

export async function save(payload, fetcher, copy) {
  try {
    const res = await postJson("/api/webgate/model", payload, fetcher ? { fetcher } : {});
    const body = res.body || {};
    // A malformed name is a code from the server; the page says it in words.
    const words = body.error === "bad_name" && copy && copy.bad_name ? copy.bad_name : "";
    return { ok: res.ok && body.ok === true,
             message: words || body.message || body.detail || body.error || "",
             now: body.now, settings: body.settings };
  } catch (err) {
    console.error("[agent-model] the change did not reach the console", err);
    return { ok: false, message: "" };
  }
}

export async function loadModel(root, copy, fetcher = fetch) {
  let body = null;
  try {
    const resp = await fetcher("/api/webgate/model", { credentials: "include" });
    body = await resp.json().catch(() => null);
    if (!resp.ok) body = { readable: false, error: (body && (body.detail || body.error)) || String(resp.status) };
  } catch (err) {
    console.error("[agent-model] the model read did not reach the console", err);
    body = { readable: false, error: String((err && err.message) || err) };
  }
  return renderModel(root, body, copy, (p) => save(p, fetcher === fetch ? undefined : fetcher, copy));
}

export function mount(doc = document) {
  const root = doc.getElementById("agent-model");
  const pane = root && root.closest("[data-pane]");
  if (!root || !pane) return null;
  const copy = { ...(doc.getElementById("agent-model-copy")?.dataset || {}) };
  let read = false;
  const go = () => {
    if (read || pane.hidden) return;
    read = true;
    root.replaceChildren(el("p", "state-body", copy.loading || ""));
    loadModel(root, copy);
  };
  if (typeof MutationObserver === "function") {
    new MutationObserver(go).observe(pane, { attributes: true, attributeFilter: ["hidden"] });
  }
  go();
  return root;
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => mount());
  else mount();
}
