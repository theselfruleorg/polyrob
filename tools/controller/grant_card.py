"""Grant cards (030 WS-E1, finding C-5): ONE human-readable approval-request
renderer for every seat.

The owner-queue notification used to be a raw truncated ``json.dumps`` — for a
payment it showed a JSON blob instead of "amount, to whom, why", carried no
deadline, and never explained that a late approval still applies (the one-shot
grant). This module renders the card; per-seat formatting stays trivial because
the decide verbs (``/approve tap-<id>``) are shared by every chat seat.
"""
import hashlib
from typing import Any, Dict, List, Optional, Tuple

#: Every money-relevant field of every money verb (CR-M01). Each present key
#: renders on its OWN line, in full, never counted against the extras cap: the
#: grant hashes the full params, so a field the card drops is a field the owner
#: approves blind. ``tests/unit/tools/controller/test_grant_card_cr_m01.py``
#: pins that no money-verb field is dropped.
_MONEY_KEYS = ("amount_usd", "amount", "amount_in", "amount_a", "amount_b",
               "size", "value_usd", "max_usd", "max_spend_usd", "price",
               "value", "value_wei", "supply", "spend_max_raw",
               "receive_min_raw", "allow_max_raw", "usd")
#: What the money moves INTO / OUT OF, or who may pull it. Also rendered in
#: full on their own lines.
_ASSET_KEYS = ("chain", "from_chain", "to_chain", "token_in", "token_out",
               "token_a", "token_b", "spend_token", "receive_token",
               "allow_spender", "token_id", "fee", "range", "slippage_bps",
               "dry_run")
_TARGET_KEYS = ("to", "to_email", "target", "recipient", "payer_contact",
                "address", "market", "url", "repo", "package")
_PURPOSE_KEYS = ("purpose", "reason", "description", "task", "subject", "text")
#: Contract-call detail (the dapp wallet's owner ask). Rendered IN FULL on
#: their own lines: an approval for calldata the owner cannot see is a blind
#: signature, and a 60-character extra would hide every argument.
_CALL_KEYS = ("selector", "args", "calldata_sha256")
#: Raw hex blobs the owner cannot read as one string: rendered as byte length +
#: sha256 of the FULL blob + its decoded 32-byte words (CR-M01).
_HEX_BLOB_KEYS = ("calldata", "data", "constructor_args", "bytecode",
                  "init_code")
_MAX_ARG_WORDS = 24

_MAX_EXTRA_PARAMS = 8
_MAX_VALUE_LEN = 60


def _present(v: Any) -> bool:
    return v is not None and v != ""


def _first(params: Dict[str, Any], keys) -> Tuple[Optional[str], Optional[str]]:
    for k in keys:
        v = params.get(k)
        if _present(v):
            return k, str(v)
    return None, None


def _hex_blob_lines(key: str, raw: Any) -> List[str]:
    """Length + sha256 of the full blob + decoded 32-byte words. Never raises."""
    text = str(raw).strip()
    body = text[2:] if text.lower().startswith("0x") else text
    try:
        blob = bytes.fromhex(body)
    except ValueError:
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        return [f"• {key}: {len(text)} chars (not hex), sha256(text)={digest}"]
    digest = hashlib.sha256(blob).hexdigest()
    lines = [f"• {key}: {len(blob)} bytes, sha256={digest}"]
    if key == "bytecode" or key == "init_code":
        # Creation code is not ABI words; the hash is what the owner compares.
        return lines
    words_hex = body
    if key in ("calldata", "data") and len(blob) >= 4:
        lines.append(f"    selector 0x{body[:8]}")
        words_hex = body[8:]
    words = [words_hex[i:i + 64] for i in range(0, len(words_hex), 64)]
    for i, w in enumerate(words[:_MAX_ARG_WORDS]):
        lines.append(f"    [{i}] {w}")
    if len(words) > _MAX_ARG_WORDS:
        lines.append(f"    … +{len(words) - _MAX_ARG_WORDS} more word(s); "
                     f"the sha256 above covers them all")
    return lines


def render_grant_card(action_name: str, params: Optional[Dict[str, Any]],
                      display_id: str, *,
                      timeout_sec: Optional[float] = None,
                      grant_ttl_hours: Optional[float] = None,
                      reply_lines: bool = True) -> str:
    """A sectioned, human-first approval card. Never raises.

    ``reply_lines=False`` drops the trailing ``/approve``/``/reject`` lines for
    a seat that draws its own buttons (the web Inbox); every field above them
    is the same card.
    """
    p = dict(params or {})
    shown = set()
    lines = [f"🔐 Approval needed: {action_name}"]
    money_key, money = _first(p, _MONEY_KEYS)
    if money:
        shown.add(money_key)
        lines.append(f"• Amount: {money}"
                     + ("" if money_key in ("amount", "amount_usd")
                        else f" ({money_key})"))
    target_key, target = _first(p, _TARGET_KEYS)
    if target:
        shown.add(target_key)
        lines.append(f"• To: {target[:120]}"
                     + ("…" if len(target) > 120 else ""))
    purpose_key, purpose = _first(p, _PURPOSE_KEYS)
    if purpose:
        shown.add(purpose_key)
        lines.append(f"• For: {purpose[:160]}"
                     + ("…" if len(purpose) > 160 else ""))
    # Every OTHER money field and every asset/grant field, in full (CR-M01).
    for k in _MONEY_KEYS + _ASSET_KEYS:
        if k in shown or not _present(p.get(k)):
            continue
        shown.add(k)
        lines.append(f"• {k}: {p[k]}")
    if p.get("selector") or p.get("args"):
        lines.append(f"• Call: {p.get('selector') or '0x'}"
                     f" value={p.get('value_wei') or 0} wei")
        words = p.get("args") if isinstance(p.get("args"), list) else []
        for i, w in enumerate(words[:_MAX_ARG_WORDS]):
            lines.append(f"    [{i}] {w}")
        if len(words) > _MAX_ARG_WORDS:
            lines.append(f"    … +{len(words) - _MAX_ARG_WORDS} more word(s); "
                         f"sha256 below covers them all")
    if p.get("calldata_sha256"):
        lines.append(f"• Calldata sha256: {p['calldata_sha256']}")
    shown.update(_CALL_KEYS)
    for k in _HEX_BLOB_KEYS:
        if k in shown or not _present(p.get(k)):
            continue
        shown.add(k)
        lines.extend(_hex_blob_lines(k, p[k]))
    extras = []
    hidden = 0
    for k, v in p.items():
        if k in shown or not _present(v):
            continue
        if len(extras) >= _MAX_EXTRA_PARAMS:
            hidden += 1
            continue
        sv = str(v)
        if len(sv) > _MAX_VALUE_LEN:
            sv = (sv[:_MAX_VALUE_LEN - 1]
                  + f"… (+{len(sv) - _MAX_VALUE_LEN + 1} chars)")
        extras.append(f"{k}={sv}")
    if extras:
        lines.append("• " + ", ".join(extras))
    if hidden:
        # The grant hashes EVERY param; say that some are not on this card.
        lines.append(f"• +{hidden} more param(s) not shown — the grant covers every param")
    if timeout_sec:
        wait = int(timeout_sec)
        tail = ""
        if grant_ttl_hours:
            tail = (f" — approving later still applies to the next identical "
                    f"attempt for {grant_ttl_hours:g}h (one-shot grant)")
        lines.append(f"⏳ Waiting up to {wait}s{tail}.")
    # ⚠️ ONE TOKEN, not verb + argument. Telegram auto-links a `/word` of
    # [A-Za-z0-9_] and sends the whole token on tap; it does NOT link a trailing
    # argument. `/approve tap-abc` therefore rendered only `/approve` as
    # tappable — the half that does nothing — and the owner had to copy the id by
    # hand off a phone ("the whole command should be highlighted so i could tap
    # on it", 2026-09-12). The underscore form is a single tap with no typing.
    # The spaced form stays below for every other seat (CLI, webview, console),
    # where it is the natural one.
    if not reply_lines:
        return "\n".join(lines)
    one_tap = str(display_id).replace("-", "_")
    lines.append(f"✅ Approve: /approve_{one_tap}")
    lines.append(f"🚫 Reject:  /reject_{one_tap}")
    # BOTH spaced verbs, not just approve: the CLI, webview and console seats use
    # this line, and a reject path that is only reachable by guessing the shape
    # is not a reject path.
    lines.append(f"(or /approve {display_id} · /reject {display_id} · /pending "
                 f"· the console Inbox)")
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
