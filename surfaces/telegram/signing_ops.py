"""`/wallet signed [n]` — what the wallet signed, from the owner's seat (113 W6 G-5).

The signing journal (``tools/wallet_sign/store.py``, 090) was readable only by the
agent's own ``wallet_sign_status`` action: no seat showed the owner what his
wallet signed. This is a READ of that one journal — when, which site, what kind
and class, and whether an autonomous run signed it — plus the credentials a site
returned (fingerprints only). It signs nothing and changes no rule.

⚠️ A read never creates the store (its constructor writes the schema), and an
unreadable store is not an empty one.
"""
from __future__ import annotations

import os
import time
from typing import List

#: Rows one reply lists; the owner may ask for up to MAX_ROWS.
DEFAULT_ROWS = 10
MAX_ROWS = 50


def signing_history_reply(args: List[str]) -> str:
    """``/wallet signed [n]`` — the last *n* signatures, newest first."""
    try:
        limit = max(1, min(MAX_ROWS, int(args[0]))) if args else DEFAULT_ROWS
    except (TypeError, ValueError):
        return f"Usage: /wallet signed [n] (1-{MAX_ROWS}, default {DEFAULT_ROWS})"
    try:
        from tools.wallet_sign.store import WalletSignStore, store_path
        path = store_path()
        if not os.path.isfile(path):
            return "The wallet has signed nothing yet (there is no signing journal)."
        store = WalletSignStore(path)
        recent = store.recent(limit)
        creds = store.credentials()
    except Exception as exc:  # noqa: BLE001 — unreadable is not empty
        return (f"I could not read the signing journal ({type(exc).__name__}: "
                f"{str(exc)[:120]}). That is not 'nothing signed'.")
    lines = [f"Last {len(recent)} signature(s), newest first:" if recent
             else "The wallet has signed nothing yet."]
    for r in recent:
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(r["ts"])))
        lines.append(f"• {when}Z {r['origin']} — {r['kind']} ({r['scheme']}, class "
                     f"{r['cls']}{', by an autonomous run' if r['autonomous'] else ''})")
    if creds:
        lines.append(f"Credentials sites gave back ({len(creds)}, fingerprints only):")
        lines += [f"• {c['origin']} '{c['label']}' {c['fingerprint']}" for c in creds]
    return "\n".join(lines)


__all__ = ["DEFAULT_ROWS", "MAX_ROWS", "signing_history_reply"]
