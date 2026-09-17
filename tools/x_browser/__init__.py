"""X browser rail — durable login, visible DMs, posting, and supervised signup.

Three pieces (2026-08-18 design):

- ``session_store``  — Fernet-encrypted per-tenant custody of the X login
  (storage_state + generated password + handle).
- ``tool``           — the optional ``x_browser`` tool: dedicated DM read/send,
  posting, login-check, and signup verbs; never raw
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
