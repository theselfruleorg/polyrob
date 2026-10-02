"""``/x`` — the owner's X account verb, contributed to the ONE verb table.

Registered through the loader's ``owner.verbs`` hook (``core.verbs.register_verbs``,
source ``pack:x``) with a handler per seat: Telegram runs :func:`telegram_x`
after its owner gate (the console runs the same handler through the
dispatcher), the REPL runs :func:`repl_x`. Owner-only, refused in a room.

* ``/x login``  — mint a one-tap X OAuth 2.0 login (``x_login_flow``) and answer
  with the authorize link. X redirects to the console callback, which renews
  the token and sends "X login renewed".
* ``/x status`` (and bare ``/x``) — the secret-free state of the OAuth 2.0 token
  and the browser session.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from core.verbs import Verb

logger = logging.getLogger(__name__)

X_VERB = Verb("/x", "set up", "My X account: renew its login, or see its state")

USAGE = ("Usage: /x login — a one-tap link that renews my X login\n"
         "       /x status — the state of my X login (no secrets)")

#: The hook value (``PackSpec.hooks["owner.verbs"]``).
OWNER_VERBS = {
    "rows": (X_VERB,),
    "handlers": {"telegram": {"/x": "polyrob_x.owner_verbs:telegram_x"},
                 "repl": {"/x": "polyrob_x.owner_verbs:repl_x"}},
}


def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    return f"{seconds // 60} min"


def status_snapshot() -> dict:
    """Secret-free X login state: the OAuth 2.0 token, its standing verdict,
    the browser session and whether ``/x login`` can run. Each part that cannot
    be read says so (``unavailable``), never reads as absent."""
    out: dict = {}
    try:
        from polyrob_x import x_oauth2
        out["oauth2"] = x_oauth2.status()
    except Exception as exc:  # noqa: BLE001
        out["oauth2"] = {"unavailable": type(exc).__name__}
    try:
        from core import credential_verdicts
        found = credential_verdicts.active("x_oauth2")
        v = found[0] if found else None
        out["verdict"] = ({"since": credential_verdicts.since_text(v),
                           "code": str(v.code or "")}
                          if v is not None else None)
    except Exception as exc:  # noqa: BLE001
        out["verdict"] = {"unavailable": type(exc).__name__}
    try:
        from core.identity import resolve_identity
        from polyrob_x.x_browser.session_store import XSessionStore
        rec = XSessionStore().load(resolve_identity())
        out["browser"] = ({"stored": True, "handle": rec.get("handle") or "",
                           "captured": rec.get("created_at") or ""}
                          if rec else {"stored": False})
    except Exception as exc:  # noqa: BLE001
        out["browser"] = {"unavailable": type(exc).__name__}
    try:
        from polyrob_x.x_login_flow import configured_redirect_uri
        out["login_link"] = {"ready": bool(configured_redirect_uri())}
    except Exception as exc:  # noqa: BLE001
        out["login_link"] = {"ready": False, "remedy": str(exc)}
    return out


def status_lines(snap: Optional[dict] = None) -> List[str]:
    snap = status_snapshot() if snap is None else snap
    lines = ["X account"]
    st = snap.get("oauth2") or {}
    if "unavailable" in st:
        lines.append(f"• API login (OAuth 2.0) for DMs: unavailable ({st['unavailable']})")
    elif st.get("stored"):
        left = st.get("expires_in_sec", 0)
        # The 2-hour access token renews itself from the refresh token; say so,
        # or "valid for 1h 59m" reads as "log in again in 2 hours" (2026-10-03).
        if st.get("expired"):
            state = "access token expired, the next call renews it" \
                if st.get("has_refresh_token") else "EXPIRED"
        elif st.get("has_refresh_token"):
            state = f"logged in, renews itself (access token: {_duration(left)} left)"
        else:
            state = f"valid for {_duration(left)}, then dead (no refresh token)"
        lines.append(f"• API login (OAuth 2.0) for DMs: {state} · refresh token "
                     f"{'yes' if st.get('has_refresh_token') else 'NO'} · scope "
                     f"[{st.get('scope') or '?'}]")
    else:
        lines.append("• API login (OAuth 2.0) for DMs: none stored"
                     + (" (a static env token is set; nothing refreshes it)"
                        if st.get("static_env") else ""))
    if st and "unavailable" not in st and not st.get("client_id_set"):
        lines.append("  the app's client id is missing (TWITTER_OAUTH2_CLIENT_ID)")
    verdict = snap.get("verdict")
    if isinstance(verdict, dict) and "unavailable" not in verdict:
        lines.append(f"• X refused the stored login {verdict.get('since') or ''}".rstrip()
                     + " — send /x login")
    br = snap.get("browser") or {}
    if "unavailable" in br:
        lines.append(f"• Browser session (posts, outreach): unavailable ({br['unavailable']})")
    elif br.get("stored"):
        handle = f"@{br['handle']}" if br.get("handle") else "(unknown handle)"
        lines.append(f"• Browser session (posts, outreach): stored for {handle}"
                     + (f" (captured {br['captured']})" if br.get("captured") else ""))
    else:
        # 2026-10-02: the owner renewed the API login expecting it to unblock
        # outreach; outreach needs THIS credential, restored only on the box.
        lines.append("• Browser session (posts, outreach): none stored — restore it "
                     "on the box with `polyrob x-account capture-session`; "
                     "/x login does not cover it")
    link = snap.get("login_link") or {}
    if link.get("ready"):
        lines.append("Renew the API login: /x login")
    else:
        lines.append("/x login is not ready: " + str(link.get("remedy") or "unknown"))
    return lines


def x_reply(owner_user_id: str, args: List[str]) -> str:
    """The seat-neutral answer to ``/x …``."""
    sub = (args[0].lower() if args else "status")
    if sub == "status":
        return "\n".join(status_lines())
    if sub != "login":
        return USAGE
    from polyrob_x.x_login_flow import (PENDING_TTL_SEC, LoginFlowError, begin_login,
                                        configured_redirect_uri)
    try:
        url = begin_login(owner_user_id, configured_redirect_uri())
    except LoginFlowError as exc:
        return f"X login cannot start: {exc}"
    except Exception as exc:  # noqa: BLE001 — an unwritable store is an answer too
        logger.warning("x login: begin failed: %s", exc)
        return f"X login cannot start: the X token store could not be written ({type(exc).__name__})."
    return ("Open this link and approve on x.com — it renews my X login:\n"
            f"{url}\n\n"
            f"The link works once and expires in {PENDING_TTL_SEC // 60} minutes. "
            "I will tell you here when the login is renewed.")


async def telegram_x(*, user_id, data_dir=None, args=None, task_agent=None, result=None,
                     board=None) -> str:
    """Telegram (and console) seat — runs after the owner gate."""
    return x_reply(str(user_id), list(args or []))


async def repl_x(ctx) -> None:
    """REPL seat."""
    owner = (getattr(ctx, "user_id", "") or "").strip() or "local"
    ctx.emit(x_reply(owner, list(getattr(ctx, "args", None) or [])), title="x")


__all__ = ["OWNER_VERBS", "USAGE", "X_VERB", "repl_x", "status_lines", "status_snapshot",
           "telegram_x", "x_reply"]
