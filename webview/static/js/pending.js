/**
 * pending.js — the pending-review page (approve/reject the quarantine queue).
 *
 * De-inlined from pending.html (043 phase 5, R6): the console CSP drops
 * `'unsafe-inline'` from `script-src`, so this rides an external module loaded
 * with `src=`. Behaviour is preserved exactly; the read-only flag crosses on
 * `#pending-list`'s `data-read-only` attribute (the copy-layer pattern), not
 * an inline `{{ }}` expression.
 */
(function () {
  const list = document.getElementById("pending-list");
  if (!list) return;
  const READ_ONLY = list.dataset.readOnly === "true";

  async function act(item, verb, msgEl, rowEl) {
    msgEl.textContent = "…";
    msgEl.className = "pend-msg";
    const r = await fetch(
      "/api/webgate/pending/" +
        encodeURIComponent(item.kind) +
        "/" +
        encodeURIComponent(item.id) +
        "/" +
        verb,
      { method: "POST" }
    );
    const out = await r.json().catch(() => ({}));
    if (r.ok && out.ok) {
      msgEl.textContent = out.message || verb + "d";
      msgEl.className = "pend-msg ok";
      rowEl.style.opacity = "0.45";
      rowEl.querySelectorAll("button").forEach((b) => (b.disabled = true));
    } else {
      msgEl.textContent = out.message || out.detail || "error " + r.status;
      msgEl.className = "pend-msg err";
    }
  }

  async function showBody(item, holder, btn) {
    if (holder.dataset.loaded) {
      const expanding = holder.style.display === "none";
      holder.style.display = expanding ? "block" : "none";
      // track VISIBILITY separately from the fetch cache — the auto-refresh
      // guard keys on data-expanded, so collapsing a body re-enables refresh.
      if (expanding) holder.dataset.expanded = "1";
      else delete holder.dataset.expanded;
      return;
    }
    btn.disabled = true;
    const r = await fetch(
      "/api/webgate/pending/" +
        encodeURIComponent(item.kind) +
        "/" +
        encodeURIComponent(item.id)
    );
    const out = await r.json().catch(() => ({}));
    holder.textContent = out.ok ? out.body : out.body || "failed to load";
    holder.dataset.loaded = "1";
    holder.dataset.expanded = "1";
    holder.style.display = "block";
    btn.disabled = false;
  }

  function render(items) {
    list.innerHTML = "";
    if (!items.length) {
      list.innerHTML =
        '<div class="text-muted">No pending proposals — the queue is clear.</div>';
      return;
    }
    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "pend-row";

      const head = document.createElement("div");
      head.className = "pend-head";
      const kind = document.createElement("span");
      kind.className = "pend-kind";
      kind.textContent = item.kind;
      const id = document.createElement("span");
      id.className = "pend-id";
      id.textContent = item.id;
      const chars = document.createElement("span");
      chars.className = "pend-chars";
      chars.textContent = item.chars + " chars";
      head.appendChild(kind);
      head.appendChild(id);
      head.appendChild(chars);

      const preview = document.createElement("div");
      preview.className = "pend-preview";
      preview.textContent = item.preview || "";

      const body = document.createElement("div");
      body.className = "pend-body";
      body.style.display = "none";

      const actions = document.createElement("div");
      actions.className = "pend-actions";
      const msg = document.createElement("span");
      msg.className = "pend-msg";

      const showBtn = document.createElement("button");
      showBtn.className = "form-control";
      showBtn.textContent = "Show full";
      showBtn.addEventListener("click", () => showBody(item, body, showBtn));
      actions.appendChild(showBtn);

      if (!READ_ONLY) {
        const ok = document.createElement("button");
        ok.className = "form-control";
        ok.textContent = "Approve";
        ok.addEventListener("click", () => {
          if (window.confirm('Approve ' + item.kind + ' "' + item.id + '"?')) {
            act(item, "promote", msg, row);
          }
        });
        const no = document.createElement("button");
        no.className = "form-control";
        no.textContent = "Reject";
        no.addEventListener("click", () => {
          if (window.confirm('Reject (archive) ' + item.kind + ' "' + item.id + '"?')) {
            act(item, "reject", msg, row);
          }
        });
        actions.appendChild(ok);
        actions.appendChild(no);
      }
      actions.appendChild(msg);

      row.appendChild(head);
      row.appendChild(preview);
      row.appendChild(body);
      row.appendChild(actions);
      list.appendChild(row);
    });
  }

  // 019 P3: auto-refresh — a newly blocked approval shows without a manual
  // reload. Skipped while an approve/deny is in flight (row buttons disable
  // themselves) and while the tab is hidden.
  let actionInFlight = false;
  document.addEventListener(
    "click",
    (e) => {
      if (e.target && e.target.tagName === "BUTTON") actionInFlight = true;
      setTimeout(() => {
        actionInFlight = false;
      }, 4000);
    },
    true
  );

  function load() {
    fetch("/api/webgate/pending")
      .then((r) => r.json())
      .then((d) => render(d.items || []))
      .catch(() => {
        list.innerHTML =
          '<div class="text-muted">Failed to load pending proposals.</div>';
      });
  }

  load();
  setInterval(() => {
    if (document.visibilityState !== "visible" || actionInFlight) return;
    // don't clobber an expanded "Show full" body the owner is reading
    if (list.querySelector(".pend-body[data-expanded]")) return;
    load();
  }, 5000);
})();
