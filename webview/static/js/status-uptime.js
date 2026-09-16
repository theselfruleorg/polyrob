/**
 * status-uptime.js — the public status page's uptime line.
 *
 * De-inlined from status.html (043 phase 5, R6): the console CSP drops
 * `'unsafe-inline'` from `script-src`. Behaviour is preserved exactly.
 */
function load() {
  const el = document.getElementById("uptime");
  if (!el) return;
  fetch("/api/status")
    .then((r) => r.json())
    .then((d) => {
      el.textContent = d.uptime_seconds + "s";
    })
    .catch(() => {});
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
}
