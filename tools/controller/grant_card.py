"""Grant cards (030 WS-E1, finding C-5): ONE human-readable approval-request
renderer for every seat.

The owner-queue notification used to be a raw truncated ``json.dumps`` — for a
payment it showed a JSON blob instead of "amount, to whom, why", carried no
deadline, and never explained that a late approval still applies (the one-shot
grant). This module renders the card; per-seat formatting stays trivial because
the decide verbs (``/approve tap-<id>``) are shared by every chat seat.
"""
import hashlib
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Every money-relevant field of every money verb (CR-M01). Each present key
#: renders on its OWN line, in full, never counted against the extras cap: the
#: grant hashes the full params, so a field the card drops is a field the owner
#: approves blind. ``tests/unit/tools/controller/test_grant_card_cr_m01.py``
#: pins that no money-verb field is dropped.
_MONEY_KEYS = ("amount_usd", "amount", "amount_in", "amount_a", "amount_b",
               "size_usd", "max_usd", "size", "value_usd", "max_spend_usd", "price",
               "value", "value_wei", "supply", "spend_max_raw",
               "receive_min_raw", "allow_max_raw", "usd", "max_amount_usd")
#: What the money moves INTO / OUT OF, or who may pull it. Also rendered in
#: full on their own lines.
_ASSET_KEYS = ("chain", "from_chain", "to_chain", "token_in", "token_out",
               "token_a", "token_b", "spend_token", "receive_token",
               "allow_spender", "token_id", "fee", "range", "slippage_bps",
               "dry_run", "expected_pay_to", "allow_plaintext")
_TARGET_KEYS = ("to", "to_email", "target", "recipient", "payer_contact",
                "address", "market", "url", "repo", "package")
_PURPOSE_KEYS = ("purpose", "reason", "description", "task")
#: 096 W4 (B1/B2): the words the action SENDS or RUNS — a tweet, a DM, an email
#: body, a shell command. Each present key is shown WHOLE in its own fenced block
#: (up to ``OUTBOUND_INLINE_MAX_CHARS``; above that a head + the hidden count +
#: the tap that opens the rest). It used to share the 160-char "For:" slot or
#: fall into the 60-char extras: the owner approved speech it could not read.
_OUTBOUND_KEYS = ("text", "body", "html", "message", "content", "command")
#: Rendered on its own line, never in the body's slot (an email's subject used
#: to take the "For:" slot and push the body into a 60-char extra).
_SUBJECT_KEY = "subject"
#: 096 D1 (proposed default): inline up to 3000 chars on a pushed card.
OUTBOUND_INLINE_MAX_CHARS = 3000
_OUTBOUND_HEAD_CHARS = 1500
_TARGET_MAX_CHARS = 300
_PURPOSE_MAX_CHARS = 600
_SUBJECT_MAX_CHARS = 300
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


def _cut(text: str, cap: int) -> str:
    """*text* whole, or its first *cap* chars + how many are hidden (R3)."""
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + f"… (+{len(text) - cap} chars)"


def _html_as_text(raw: str) -> str:
    import html as _html
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p\s*>|</div\s*>|</li\s*>", "\n", text)
    # A link shows its target next to its text: the owner approves where the
    # link goes, not only what it says (an <a> whose text is one address and
    # whose href is another is the phishing shape).
    text = re.sub(
        r"""(?is)<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))[^>]*>(.*?)</a\s*>""",
        lambda m: f"{m.group(4)} [link: {m.group(1) or m.group(2) or m.group(3) or ''}]",
        text)
    text = re.sub(
        r"""(?is)<img\b[^>]*?\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))[^>]*>""",
        lambda m: f"[image: {m.group(1) or m.group(2) or m.group(3) or ''}]", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _outbound_lines(key: str, raw: Any, *, full: bool, more_tap: str) -> List[str]:
    """The fenced, whole outbound value for *key* (096 W4)."""
    text = str(raw)
    label = key
    if key == "html":
        text = _html_as_text(text)
        label = "html, shown as text"
    head = f"• {label} ({len(text)} chars):"
    shown = text
    tail = None
    if not full and len(text) > OUTBOUND_INLINE_MAX_CHARS:
        shown = text[:_OUTBOUND_HEAD_CHARS].rstrip()
        hidden = len(text) - len(shown)
        tail = (f"(+{hidden} chars not shown here — the whole text: {more_tap})"
                if more_tap else f"(+{hidden} chars not shown here)")
    # A value that itself holds ``` would close the fence early.
    lines = [head, "```", shown.replace("```", "ʼʼʼ"), "```"]
    if tail:
        lines.append(tail)
    return lines


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


#: Per-action card context: facts a verb's params do not carry but the owner
#: must see before consent (e.g. what an agent NFT's account holds before it is
#: given away). ``action_name -> fn(params) -> list[str]``; read ONCE when the
#: ask is created and stored on it, so every seat shows the same lines.
_CARD_CONTEXT: Dict[str, Callable[[Dict[str, Any]], List[str]]] = {}
_MAX_CONTEXT_LINES = 40


def register_card_context(action_name: str, fn: Callable[[Dict[str, Any]], List[str]]) -> None:
    if not callable(fn):
        raise TypeError(f"card context for {action_name}: {fn!r} is not callable")
    _CARD_CONTEXT[str(action_name)] = fn


def card_context_lines(action_name: str, params: Optional[Dict[str, Any]]) -> List[str]:
    """The registered context lines for *action_name*, or ``[]``. Never raises:
    a provider that fails says so ON the card (an unreadable fact is not an
    absent one)."""
    fn = _CARD_CONTEXT.get(str(action_name))
    if fn is None:
        return []
    try:
        lines = [str(x) for x in (fn(dict(params or {})) or [])]
    except Exception as exc:  # noqa: BLE001
        return [f"⚠️ Context for this approval could not be read ({type(exc).__name__}) — "
                f"you would approve without it."]
    if len(lines) > _MAX_CONTEXT_LINES:
        lines = lines[:_MAX_CONTEXT_LINES] + [f"… +{len(lines) - _MAX_CONTEXT_LINES} more line(s)"]
    return lines


def render_grant_card(action_name: str, params: Optional[Dict[str, Any]],
                      display_id: str, *,
                      timeout_sec: Optional[float] = None,
                      grant_ttl_hours: Optional[float] = None,
                      reply_lines: bool = True,
                      context_lines: Optional[List[str]] = None,
                      full_content: bool = False) -> str:
    """A sectioned, human-first approval card. Never raises.

    ``full_content=True`` shows every outbound text whole at any length (the
    tapped full view, ``/pending <id>``, and the web Inbox); the pushed card
    shows up to ``OUTBOUND_INLINE_MAX_CHARS`` and names the tap to the rest.

    ``reply_lines=False`` drops the trailing ``/approve``/``/reject`` lines for
    a seat that draws its own buttons (the web Inbox); every field above them
    is the same card. ``context_lines`` (``card_context_lines``) render after
    the params, before the deadline.
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
        lines.append(f"• To: {_cut(target, _TARGET_MAX_CHARS)}")
    if _present(p.get(_SUBJECT_KEY)):
        shown.add(_SUBJECT_KEY)
        lines.append(f"• Subject: {_cut(str(p[_SUBJECT_KEY]), _SUBJECT_MAX_CHARS)}")
    purpose_key, purpose = _first(p, _PURPOSE_KEYS)
    if purpose:
        shown.add(purpose_key)
        lines.append(f"• For: {_cut(purpose, _PURPOSE_MAX_CHARS)}")
    more_tap = f"/pending_{str(display_id).replace('-', '_')}" if display_id else ""
    for k in _OUTBOUND_KEYS:
        if k in shown or not _present(p.get(k)) or isinstance(p.get(k), (dict, list)):
            continue
        shown.add(k)
        lines.extend(_outbound_lines(k, p[k], full=full_content, more_tap=more_tap))
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
    for line in (context_lines or [])[:_MAX_CONTEXT_LINES + 1]:
        lines.append(str(line))
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
