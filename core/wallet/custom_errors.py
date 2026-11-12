"""Solidity custom errors — name a revert by its 4-byte selector, WITH its arguments.

A table maps a selector to ``(signature, text)``; the signature keeps the parameter names
(``"Refused(address target, bytes4 selector)"``) so a decoded revert reads
``Refused(target=0x…, selector=0x…)``. The selectors are LITERALS in each table (no keccak at
import, so a table imports without the crypto extra); a unit test computes every one from its
signature.

Which table applies is the caller's decision: a 4-byte selector is not unique across
contracts (``NotOwner()`` is a common error), so a revert is matched only against the tables
of the contracts the transaction is known to reach (``core.wallet.collection_errors``).
"""
from typing import Mapping, Optional, Tuple

#: selector (``0x`` + 8 lowercase hex) -> (signature with parameter names, what it means)
ErrorTable = Mapping[str, Tuple[str, str]]


def canonical(signature: str) -> str:
    """``"Refused(address target, bytes4 selector)"`` -> ``"Refused(address,bytes4)"``."""
    name, _, rest = str(signature).partition("(")
    params = [p.strip().split()[0] for p in rest.rstrip(")").split(",") if p.strip()]
    return f"{name.strip()}({','.join(params)})"


def _params(signature: str) -> Tuple[Tuple[str, str], ...]:
    _, _, rest = str(signature).partition("(")
    out = []
    for p in rest.rstrip(")").split(","):
        bits = p.strip().split()
        if bits:
            out.append((bits[0], bits[1] if len(bits) > 1 else f"arg{len(out)}"))
    return tuple(out)


def _value(kind: str, value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    if kind == "address":
        return str(value).lower()
    return str(value)


def revert_hex(revert_data) -> str:
    """Revert data as lowercase ``0x`` hex ('' when it is not hex)."""
    if isinstance(revert_data, (bytes, bytearray)):
        return "0x" + bytes(revert_data).hex()
    text = str(revert_data or "").strip().lower()
    return text if text.startswith("0x") else ""


def decode(revert_data, table: ErrorTable) -> Optional[str]:
    """``Name(arg=value, …) (text)`` when *revert_data* starts with a selector of *table*, else
    None. Arguments that cannot be decoded are named as unreadable, never guessed."""
    data = revert_hex(revert_data)
    row = table.get(data[:10]) if len(data) >= 10 else None
    if row is None:
        return None
    signature, text = row
    name = signature.partition("(")[0]
    params = _params(signature)
    if not params:
        return f"{name}() ({text})"
    try:
        from eth_abi import decode as _abi_decode
        values = _abi_decode([k for k, _ in params], bytes.fromhex(data[10:]))
        args = ", ".join(f"{n}={_value(k, v)}" for (k, n), v in zip(params, values))
    except Exception:  # noqa: BLE001 — no crypto extra, or short data: name it, say so
        args = "arguments unreadable"
    return f"{name}({args}) ({text})"


__all__ = ["ErrorTable", "canonical", "decode", "revert_hex"]
