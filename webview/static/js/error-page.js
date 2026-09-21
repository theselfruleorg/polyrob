/**
 * error-page.js — the console error page's troubleshooting actions.
 *
 * De-inlined from error.html (043 phase 5, R6): the console CSP drops
 * `'unsafe-inline'` from `script-src`, so this behaviour rides an external
 * module loaded with `src=` instead of an inline `<script>`. Behaviour is
 * preserved byte-for-byte; the session id crosses on the button's
 * `data-session-id` attribute, the copy-layer pattern the shell uses.
 *
 * ⚠️ A30 (2026-09-21): the "Force Refresh All Sessions" button is GONE with
 * its route (`GET /api/refresh`). It walked ~1,790 session directories on the
 * event loop to rebuild a catalog no surviving screen reads, and a button that
 * calls a deleted route is worse than no button — it reports a failure the
 * operator then tries to troubleshoot.
 */
function bind() {
  const repairBtn = document.getElementById("repair-session");
  if (repairBtn) {
    repairBtn.addEventListener("click", function () {
      const sessionId = this.getAttribute("data-session-id");
      this.disabled = true;
      this.innerHTML = '<span class="action-prefix">></span> Repairing...';

      fetch(`/api/repair/${sessionId}`, { method: "POST" })
        .then((response) => response.json())
        .then((data) => {
          if (data.status === "ok") {
            this.innerHTML = '<span class="action-prefix">></span> Success! Redirecting...';
            setTimeout(() => {
              window.location.href = `/session/${sessionId}`;
            }, 1000);
          } else {
            this.innerHTML = '<span class="action-prefix">></span> Error: ' + data.message;
            this.disabled = false;
          }
        })
        .catch((error) => {
          console.error("Error repairing session:", error);
          this.innerHTML = '<span class="action-prefix">></span> Error Repairing';
          this.disabled = false;
        });
    });
  }
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
}
