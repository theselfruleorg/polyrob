"""The x pack's status lines (X evaluation 2026-09-26, build item 4).

``build() -> list[str]`` is meant for a ``PackSpec.status_sections`` row, so it
renders under the ``packs`` section of every status seat (`/status`, `polyrob
doctor`, the console). The core tier already names an open ``x_oauth2`` verdict
(``core.status_snapshot.x_oauth2_line``) but cannot decrypt the token record;
this module can, so it adds what only the pack sees: the expiry, whether a
refresh token is held, and the scope.

Secret-free by construction: it reads :func:`polyrob_x.x_oauth2.status`, which
returns presence, expiry and scope only — never a token value.
"""
from __future__ import annotations

from typing import Any, Dict, List


def _duration(seconds: float) -> str:
    try:
        from core.credential_verdicts import duration_text
        return duration_text(seconds)
    except Exception:  # noqa: BLE001 — a formatting helper, never fatal
        return f"{int(seconds)}s"


def lines_from(st: Dict[str, Any]) -> List[str]:
    """The owner-facing lines for one :func:`polyrob_x.x_oauth2.status` dict."""
    if st.get("relogin_needed"):
        since = st.get("relogin_needed_since") or "unknown"
        remedy = st.get("relogin_remedy") or "`/x login` or `polyrob x-account oauth-login`"
        return [f"X login (OAuth 2.0, DMs): re-login needed since {since} — {remedy}"]
    if st.get("stored"):
        left = st.get("expires_in_sec")
        refresh = bool(st.get("has_refresh_token"))
        if left is None:
            head = "X login (OAuth 2.0, DMs): stored, expiry unknown"
        elif int(left) <= 0:
            head = (f"X login (OAuth 2.0, DMs): access token expired {_duration(-int(left))} ago"
                    + ("; the next call refreshes it" if refresh else
                       " and no refresh token is held — re-login: `/x login`"))
        else:
            # 2026-10-03: "expires in 1h 59m" read as "log in again in 2 hours".
            # The 2-hour access token renews itself; only the login can die.
            head = ("X login (OAuth 2.0, DMs): valid, renews itself"
                    + (f" (access token: {_duration(int(left))} left)" if refresh else
                       f", access token expires in {_duration(int(left))}"))
        if st.get("stored") and not refresh and (left is None or int(left) > 0):
            head += " (no refresh token — it dies at expiry)"
        out = [head]
        if not st.get("client_id_set"):
            out.append("X login: TWITTER_OAUTH2_CLIENT_ID is not set — no refresh can run")
        if st.get("scope"):
            out.append(f"X login scope: {st['scope']}")
        return out
    if st.get("static_env"):
        return ["X login (OAuth 2.0, DMs): static env token only (TWITTER_OAUTH2_ACCESS_TOKEN, "
                "~2h life, never refreshed) — `/x login` stores a refreshable one"]
    return ["X login (OAuth 2.0, DMs): none — DM reads need it; `/x login` or "
            "`polyrob x-account oauth-login`"]


def build() -> List[str]:
    """Secret-free X OAuth 2.0 status lines. An unreadable store is said so,
    never rendered as "none"."""
    try:
        from polyrob_x.x_oauth2 import status
        st = status()
    except Exception as exc:  # noqa: BLE001 — rendered, never omitted
        return [f"X login (OAuth 2.0, DMs): unreadable ({type(exc).__name__}: {exc})"]
    return lines_from(st)


__all__ = ["build", "lines_from"]
