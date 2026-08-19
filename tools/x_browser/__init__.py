"""X (x.com) browser rail — durable login + gated posting + supervised signup.

Three pieces (2026-08-18 plan, spec
docs/superpowers/specs/2026-08-18-agent-mail-and-x-account-design.md):

- ``session_store``  — Fernet-encrypted per-tenant custody of the X login
  (storage_state + generated password + handle).
- ``tool``           — the optional ``x_browser`` tool: dedicated, approval-
  gated verbs (``x_post`` / ``x_login_check`` / ``x_signup_start``); never raw
  browser clicks, so the approval gate is enforceable by action name.
- ``signup``         — deterministic signup state machine; a CAPTCHA / phone
  check / unknown page is an OBSTACLE that escalates to the owner (ask/notice
  rails) — never auto-solved. One account per tenant; the profile carries an
  automation disclosure.

Gate: ``X_BROWSER_ENABLED`` (default OFF). Capabilities: high_impact +
delegate_blocked. Never in the default tool_ids.
"""

__all__ = ["register_x_browser_tool", "x_browser_enabled"]


def __getattr__(name):
    # Lazy re-export so importing the package (e.g. the session store) never
    # pulls the Playwright-heavy tool module.
    if name in __all__:
        from tools.x_browser import registration
        return getattr(registration, name)
    raise AttributeError(name)
