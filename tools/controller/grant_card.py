"""Grant cards (030 WS-E1, finding C-5): ONE human-readable approval-request
renderer for every seat.

The owner-queue notification used to be a raw truncated ``json.dumps`` — for a
payment it showed a JSON blob instead of "amount, to whom, why", carried no
deadline, and never explained that a late approval still applies (the one-shot
grant). This module renders the card; per-seat formatting stays trivial because
the decide verbs (``/approve tap-<id>``) are shared by every chat seat.
"""
from typing import Any, Dict, Optional

_MONEY_KEYS = ("amount_usd", "amount", "size", "value_usd", "max_usd", "price")
_TARGET_KEYS = ("to", "to_email", "target", "recipient", "payer_contact",
                "address", "market", "url", "repo", "package")
_PURPOSE_KEYS = ("purpose", "reason", "description", "task", "subject", "text")
_SKIP_KEYS = set(_MONEY_KEYS) | set(_TARGET_KEYS) | set(_PURPOSE_KEYS)

_MAX_EXTRA_PARAMS = 4
_MAX_VALUE_LEN = 60


def _first(params: Dict[str, Any], keys) -> Optional[str]:
    for k in keys:
        v = params.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def render_grant_card(action_name: str, params: Optional[Dict[str, Any]],
                      display_id: str, *,
                      timeout_sec: Optional[float] = None,
                      grant_ttl_hours: Optional[float] = None) -> str:
    """A sectioned, human-first approval card. Never raises."""
    p = dict(params or {})
    lines = [f"🔐 Approval needed: {action_name}"]
    money = _first(p, _MONEY_KEYS)
    if money:
        lines.append(f"• Amount: {money}")
    target = _first(p, _TARGET_KEYS)
    if target:
        lines.append(f"• To: {target[:120]}")
    purpose = _first(p, _PURPOSE_KEYS)
    if purpose:
        lines.append(f"• For: {purpose[:160]}")
    extras = []
    for k, v in p.items():
        if k in _SKIP_KEYS or v in (None, ""):
            continue
        sv = str(v)
        if len(sv) > _MAX_VALUE_LEN:
            sv = sv[:_MAX_VALUE_LEN - 1] + "…"
        extras.append(f"{k}={sv}")
        if len(extras) >= _MAX_EXTRA_PARAMS:
            break
    if extras:
        lines.append("• " + ", ".join(extras))
    if timeout_sec:
        wait = int(timeout_sec)
        tail = ""
        if grant_ttl_hours:
            tail = (f" — approving later still applies to the next identical "
                    f"attempt for {grant_ttl_hours:g}h (one-shot grant)")
        lines.append(f"⏳ Waiting up to {wait}s{tail}.")
    lines.append(f"Approve: /approve {display_id} · Reject: /reject {display_id}")
    lines.append("(also: /pending, the webview Review page, or "
                 "polyrob owner promote/reject)")
    return "\n".join(lines)


def render_pending_preview(tool_name: str, params_summary: str,
                           *, max_len: int = 160) -> str:
    """The /pending list line: action first, then a compact params gist —
    never the old 'tool=… params={json} session=…' machine string."""
    gist = (params_summary or "").strip()
    if gist.startswith("{") and gist.endswith("}"):
        gist = gist[1:-1]
    text = f"{tool_name} — {gist}" if gist else str(tool_name)
    if len(text) > max_len:
        text = text[:max_len - 1] + "…"
    return text
