"""The signer wire format (066 §5.1): length-prefixed JSON, one request per frame.

A frame is a 4-byte big-endian length followed by that many bytes of UTF-8
JSON. A request is ``{"v": 1, "op": "<op>", "body": {...}}``; a response is
``{"v": 1, "ok": true, "result": {...}}`` or
``{"v": 1, "ok": false, "code": "<code>", "reason": "<text>"}``.

Strict on purpose. The codecs below refuse an unknown field instead of
ignoring it: a field the signer does not read is a field the agent could use to
say one thing to its own guard and another to the signer. An unknown field, an
unknown op or a wrong version is the refusal code ``unknown_shape``.
"""
import dataclasses
import hashlib
import json
import struct
from typing import Any, Dict, Optional

SCHEMA_VERSION = 1
#: A request that needs more than this is not a transaction. Init code is
#: bounded by EIP-3860 (48 KiB, 96 KiB as hex) — far below this.
MAX_FRAME = 1 << 20
_HEADER = struct.Struct(">I")

#: Every op the signer knows. Anything else is refused as ``unknown_shape``.
OPS = frozenset({
    "ping",               # health + caps summary
    "identity",           # public addresses
    "evm.verdict",        # shadow: would the signer sign this? never signs
    "evm.send",           # remote: guard, sign, broadcast, journal
    "x402.authorize",     # EIP-3009 TransferWithAuthorization
    "venue.sign",         # 066 P3: typed venue payloads (Hyperliquid L1 orders)
    "eip8004.feedback_auth",  # 066 P3: the EIP-712 feedback authorization
    "journal.sign",       # C1: EIP-191 over the account journal template, nothing else
    "deposit.address",    # the deterministic per-user deposit address
    "deposit.sweep",      # sweep a deposit to the PINNED treasury destination
    "approvals.list",     # pending above-hard-cap requests
    "approvals.decide",   # ROOT ONLY: grant / deny one request
    "pause.set",          # ROOT ONLY: the signer's own spend pause
})
#: Ops only uid 0 (the owner on the box) may call. D5: approvals come from the
#: owner CLI on the box; a Telegram approval runs inside the agent process and
#: can therefore never unlock anything above the hard cap.
ROOT_ONLY_OPS = frozenset({"approvals.decide", "pause.set"})

# Refusal codes (stable strings; the agent maps them to plain text).
UNKNOWN_SHAPE = "unknown_shape"
PEER_REFUSED = "peer_refused"
NOT_ROOT = "not_root"
GUARD_REFUSED = "guard_refused"
APPROVAL_REQUIRED = "approval_required"
NONCE_REUSE = "nonce_reuse"
NONCE_GAP = "nonce_gap"
PAUSED = "paused"
CHAIN_NOT_ALLOWED = "chain_not_allowed"
FEE_CEILING = "fee_ceiling"
BROADCAST_FAILED = "broadcast_failed"
INTERNAL = "internal"
NOT_CONFIGURED = "not_configured"


class ProtocolError(ValueError):
    """A frame or a body that does not match the schema (``unknown_shape``)."""


# -- framing -----------------------------------------------------------------

def encode_frame(obj: Dict[str, Any]) -> bytes:
    _finite_numbers(obj)
    payload = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(payload) > MAX_FRAME:
        raise ProtocolError(f"frame of {len(payload)} bytes exceeds {MAX_FRAME}")
    return _HEADER.pack(len(payload)) + payload


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed the connection mid-frame")
        buf.extend(chunk)
    return bytes(buf)


def send_frame(sock, obj: Dict[str, Any]) -> None:
    sock.sendall(encode_frame(obj))


def recv_frame(sock) -> Dict[str, Any]:
    (length,) = _HEADER.unpack(_recv_exact(sock, _HEADER.size))
    if length > MAX_FRAME:
        raise ProtocolError(f"frame of {length} bytes exceeds {MAX_FRAME}")
    try:
        obj = json.loads(_recv_exact(sock, length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"frame is not UTF-8 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("frame is not a JSON object")
    return obj


def request(op: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"v": SCHEMA_VERSION, "op": op, "body": dict(body or {})}


def ok(result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"v": SCHEMA_VERSION, "ok": True, "result": dict(result or {})}


def refusal(code: str, reason: str, **extra) -> Dict[str, Any]:
    out = {"v": SCHEMA_VERSION, "ok": False, "code": str(code), "reason": str(reason)}
    out.update(extra)
    return out


def parse_request(obj: Dict[str, Any]):
    """``(op, body)`` or :class:`ProtocolError`."""
    _finite_numbers(obj)
    if set(obj) - {"v", "op", "body"}:
        raise ProtocolError(f"unknown request keys {sorted(set(obj) - {'v', 'op', 'body'})}")
    if obj.get("v") != SCHEMA_VERSION:
        raise ProtocolError(f"schema version {obj.get('v')!r} is not {SCHEMA_VERSION}")
    op = obj.get("op")
    if op not in OPS:
        raise ProtocolError(f"unknown op {op!r}")
    body = obj.get("body", {})
    if not isinstance(body, dict):
        raise ProtocolError("body is not an object")
    return op, body


def check_keys(body: Dict[str, Any], *, required=(), optional=()) -> None:
    """Refuse a body with a missing required key or ANY unknown key."""
    allowed = set(required) | set(optional)
    extra = set(body) - allowed
    if extra:
        raise ProtocolError(f"unknown field(s) {sorted(extra)}")
    missing = [k for k in required if k not in body]
    if missing:
        raise ProtocolError(f"missing field(s) {missing}")


# -- TxIntent codec ----------------------------------------------------------

def _finite_numbers(value):
    import math
    if isinstance(value, float) and not math.isfinite(value):
        raise ProtocolError("non-finite number in signing request")
    if isinstance(value, dict):
        for item in value.values():
            _finite_numbers(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _finite_numbers(item)

def _intent_cls():
    from core.wallet.tx_guard import TxIntent
    return TxIntent


def _jsonable(value):
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _tupled(value):
    if isinstance(value, list):
        return tuple(_tupled(v) for v in value)
    return value


def intent_to_wire(intent) -> Dict[str, Any]:
    """Every field of a ``TxIntent`` as JSON-able data (tuples become lists)."""
    return {f.name: _jsonable(getattr(intent, f.name)) for f in dataclasses.fields(intent)}


def intent_from_wire(data: Dict[str, Any]):
    """Rebuild a ``TxIntent``. An unknown field refuses; a missing optional
    field takes the dataclass default, exactly as a caller omitting it would."""
    _finite_numbers(data)
    if not isinstance(data, dict):
        raise ProtocolError("intent is not an object")
    cls = _intent_cls()
    fields = {f.name: f for f in dataclasses.fields(cls)}
    extra = set(data) - set(fields)
    if extra:
        raise ProtocolError(f"unknown intent field(s) {sorted(extra)}")
    kwargs = {k: _tupled(v) for k, v in data.items()}
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ProtocolError(f"intent does not match TxIntent: {exc}") from exc


# -- transaction codec -------------------------------------------------------

#: The EIP-1559 fields ``EvmRail`` builds. Nothing else is signed: an
#: ``accessList`` or a ``from`` the guard never saw is an unknown shape.
TX_FIELDS = ("to", "value", "data", "nonce", "gas", "maxFeePerGas",
             "maxPriorityFeePerGas", "chainId", "type")
_TX_INT_FIELDS = ("value", "nonce", "gas", "maxFeePerGas", "maxPriorityFeePerGas",
                  "chainId", "type")


def tx_to_wire(tx: Dict[str, Any]) -> Dict[str, Any]:
    extra = set(tx) - set(TX_FIELDS)
    if extra:
        raise ProtocolError(f"unknown transaction field(s) {sorted(extra)}")
    return {k: tx[k] for k in TX_FIELDS if k in tx}


def tx_from_wire(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ProtocolError("transaction is not an object")
    extra = set(data) - set(TX_FIELDS)
    if extra:
        raise ProtocolError(f"unknown transaction field(s) {sorted(extra)}")
    for k in ("value", "data", "nonce", "gas", "maxFeePerGas", "chainId"):
        if k not in data:
            raise ProtocolError(f"transaction is missing {k!r}")
    out = dict(data)
    for k in _TX_INT_FIELDS:
        if k in out:
            v = out[k]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ProtocolError(f"transaction field {k!r} must be a non-negative integer")
    if not isinstance(out.get("data"), str) or not out["data"].startswith("0x"):
        raise ProtocolError("transaction data must be a 0x-prefixed hex string")
    to = out.get("to")
    if to is not None and (not isinstance(to, str) or not to.startswith("0x") or len(to) != 42):
        raise ProtocolError("transaction 'to' must be a 20-byte 0x address or null")
    return out


def tx_digest(tx: Dict[str, Any]) -> str:
    """What was AUTHORIZED, independent of gas sizing.

    ``EvmRail.size_gas`` rewrites ``gas`` after ``tx_guard`` authorized the
    transaction, so the digest covers the fields that decide what the
    transaction DOES: chain, destination, value, calldata and nonce.
    """
    core = {
        "chainId": int(tx.get("chainId") or 0),
        "to": (str(tx.get("to")).lower() if tx.get("to") else None),
        "value": int(tx.get("value") or 0),
        "data": str(tx.get("data") or "0x").lower(),
        "nonce": int(tx.get("nonce") if tx.get("nonce") is not None else -1),
    }
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()


def request_digest(intent_wire: Dict[str, Any], tx: Dict[str, Any]) -> str:
    """The digest an owner approval is bound to: the intent plus what the
    transaction does, WITHOUT the nonce or gas — a retry after the owner's grant
    reads a fresh nonce and must still match."""
    core = {
        "intent": {k: v for k, v in sorted(intent_wire.items()) if k != "idempotency_key"},
        "chainId": int(tx.get("chainId") or 0),
        "to": (str(tx.get("to")).lower() if tx.get("to") else None),
        "value": int(tx.get("value") or 0),
        "data": str(tx.get("data") or "0x").lower(),
    }
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()
