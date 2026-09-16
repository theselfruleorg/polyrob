"""A minimal ABI encoder — enough for the shapes this tree actually sends (042).

Why not a library: the tree has no web3 dependency and adding one to the money
path to encode two known call shapes is a poor trade. Why not string
concatenation at the call site: the Pons launch tuple is a nested struct
containing a nested struct and five strings, and hand-splicing offsets is
exactly the kind of code that is wrong in a way nothing notices until a
transaction lands somewhere unintended.

So: a small, complete-for-its-scope encoder, with the acceptance test that
matters — ``tests/unit/core/wallet/test_abi.py`` re-encodes the arguments of a
LIVE mainnet transaction and asserts the result is byte-identical to the
calldata that transaction actually carried.

Supported: ``uintN``/``intN``, ``bool``, ``address``, ``bytesN``, ``bytes``,
``string``, ``T[]``, ``T[k]``, and ``tuple`` (nested to any depth). Anything
else raises rather than guessing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

WORD = 32


class AbiError(ValueError):
    """The value cannot be encoded as the declared type. Always says which."""


def selector(signature: str) -> str:
    """``keccak(signature)[:4]`` as 0x-hex. The signature must be canonical."""
    from eth_utils import keccak
    return "0x" + keccak(signature.encode()).hex()[:8]


def signature_of(name: str, inputs: Sequence[Dict[str, Any]]) -> str:
    """The canonical signature of a function from its ABI inputs."""
    return f"{name}({','.join(_canonical(i) for i in inputs)})"


def _canonical(field: Dict[str, Any]) -> str:
    kind = field["type"]
    if kind.startswith("tuple"):
        inner = ",".join(_canonical(c) for c in field.get("components") or ())
        return f"({inner}){kind[len('tuple'):]}"
    return kind


def encode(inputs: Sequence[Dict[str, Any]], values: Sequence[Any]) -> bytes:
    """ABI-encode *values* against the ABI *inputs* list."""
    if len(inputs) != len(values):
        raise AbiError(f"expected {len(inputs)} arguments, got {len(values)}")
    return _encode_tuple([i for i in inputs], list(values))


def encode_call(name: str, inputs: Sequence[Dict[str, Any]],
                values: Sequence[Any]) -> str:
    """``0x`` + selector + encoded arguments."""
    return selector(signature_of(name, inputs)) + encode(inputs, values).hex()


# -- internals -------------------------------------------------------------

def _is_dynamic(field: Dict[str, Any]) -> bool:
    kind = field["type"]
    if kind in ("string", "bytes"):
        return True
    if kind.endswith("[]"):
        return True
    if kind.endswith("]"):                       # fixed-size array T[k]
        inner, count = _array_parts(field)
        return count > 0 and _is_dynamic(inner)
    if kind.startswith("tuple"):
        return any(_is_dynamic(c) for c in field.get("components") or ())
    return False


def _array_parts(field: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    kind = field["type"]
    head, _, tail = kind.rpartition("[")
    count = -1 if tail == "]" else int(tail[:-1])
    return ({**field, "type": head}, count)


def _encode_tuple(fields: List[Dict[str, Any]], values: List[Any]) -> bytes:
    head: List[bytes] = []
    tail: List[bytes] = []
    # The head is one word per field (a value, or an offset). Offsets are
    # relative to the START of this tuple's head — getting that wrong is the
    # classic ABI bug, and it produces calldata a node accepts and a contract
    # reads as different arguments.
    head_size = 0
    for field in fields:
        if _is_dynamic(field):
            head_size += WORD
        else:
            head_size += len(_encode_static(field, None, size_only=True))

    offset = head_size
    for field, value in zip(fields, values):
        if _is_dynamic(field):
            blob = _encode_value(field, value)
            head.append(_uint(offset))
            tail.append(blob)
            offset += len(blob)
        else:
            head.append(_encode_value(field, value))
    return b"".join(head) + b"".join(tail)


def _encode_static(field: Dict[str, Any], value, *, size_only: bool = False) -> bytes:
    """Static encoding, or (size_only) a zero blob of the right length."""
    kind = field["type"]
    if kind.startswith("tuple"):
        components = list(field.get("components") or ())
        if size_only:
            return b"".join(_encode_static(c, None, size_only=True) for c in components)
        return _encode_tuple(components, list(value))
    if kind.endswith("]"):
        inner, count = _array_parts(field)
        if size_only:
            return _encode_static(inner, None, size_only=True) * max(count, 0)
        if len(value) != count:
            raise AbiError(f"{kind} needs exactly {count} items, got {len(value)}")
        return b"".join(_encode_value(inner, v) for v in value)
    return bytes(WORD) if size_only else _encode_value(field, value)


def _encode_value(field: Dict[str, Any], value) -> bytes:
    kind = field["type"]
    name = field.get("name") or kind

    if kind.startswith("tuple"):
        if kind.endswith("]"):
            return _encode_array(field, value)
        return _encode_tuple(list(field.get("components") or ()), list(value))

    if kind.endswith("]"):
        return _encode_array(field, value)

    if kind == "address":
        return _address(value, name)
    if kind == "bool":
        if not isinstance(value, bool):
            raise AbiError(f"{name}: bool expected, got {type(value).__name__}")
        return _uint(1 if value else 0)
    if kind == "string":
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        return _uint(len(raw)) + _pad_right(raw)
    if kind == "bytes":
        raw = _hex_bytes(value, name)
        return _uint(len(raw)) + _pad_right(raw)
    if kind.startswith("bytes"):
        width = int(kind[len("bytes"):])
        raw = _hex_bytes(value, name)
        if len(raw) != width:
            raise AbiError(f"{name}: {kind} needs {width} bytes, got {len(raw)}")
        return _pad_right(raw)
    if kind.startswith("uint"):
        width = int(kind[len("uint"):] or 256)
        return _uint(_whole(value, name), width=width, name=name)
    if kind.startswith("int"):
        width = int(kind[len("int"):] or 256)
        return _int(_whole(value, name), width=width, name=name)
    raise AbiError(f"{name}: unsupported ABI type {kind!r}")


def _encode_array(field: Dict[str, Any], value) -> bytes:
    inner, count = _array_parts(field)
    items = list(value)
    if count >= 0:
        if len(items) != count:
            raise AbiError(f"{field['type']} needs exactly {count} items, got {len(items)}")
        return _encode_tuple([inner] * count, items)
    return _uint(len(items)) + _encode_tuple([inner] * len(items), items)


def _whole(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AbiError(f"{name}: a whole number is required, got {value!r}")
    return value


def _uint(value: int, *, width: int = 256, name: str = "uint") -> bytes:
    if value < 0:
        raise AbiError(f"{name}: unsigned value cannot be negative ({value})")
    if value >= 1 << width:
        raise AbiError(f"{name}: {value} does not fit in uint{width}")
    return value.to_bytes(WORD, "big")


def _int(value: int, *, width: int = 256, name: str = "int") -> bytes:
    bound = 1 << (width - 1)
    if not -bound <= value < bound:
        raise AbiError(f"{name}: {value} does not fit in int{width}")
    return (value & ((1 << 256) - 1)).to_bytes(WORD, "big")


def _address(value, name: str) -> bytes:
    raw = _hex_bytes(value, name)
    if len(raw) != 20:
        raise AbiError(f"{name}: an address is 20 bytes, got {len(raw)}")
    return bytes(12) + raw


def _hex_bytes(value, name: str) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if not isinstance(value, str):
        raise AbiError(f"{name}: expected hex or bytes, got {type(value).__name__}")
    text = value[2:] if value.startswith("0x") else value
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise AbiError(f"{name}: not valid hex ({exc})")


def _pad_right(raw: bytes) -> bytes:
    if not raw:
        return b""
    pad = (-len(raw)) % WORD
    return raw + bytes(pad)


# ==========================================================================
# Decoding — needed for the READ path (a contract view returns a tuple), and
# it is what makes the encoder's acceptance test a true round trip.
# ==========================================================================

def decode(outputs: Sequence[Dict[str, Any]], data) -> Tuple[Any, ...]:
    """Decode ABI-encoded *data* against the ABI *outputs* list."""
    raw = _hex_bytes(data, "data")
    values, _ = _decode_tuple(list(outputs), raw, 0)
    return tuple(values)


def _decode_tuple(fields: List[Dict[str, Any]], raw: bytes, base: int):
    cursor = base
    values: List[Any] = []
    for field in fields:
        if _is_dynamic(field):
            offset = int.from_bytes(_slice(raw, cursor, WORD), "big")
            values.append(_decode_value(field, raw, base + offset))
            cursor += WORD
        else:
            values.append(_decode_value(field, raw, cursor))
            cursor += len(_encode_static(field, None, size_only=True))
    return values, cursor


def _decode_value(field: Dict[str, Any], raw: bytes, at: int):
    kind = field["type"]

    if kind.endswith("]"):
        inner, count = _array_parts(field)
        if count < 0:
            count = int.from_bytes(_slice(raw, at, WORD), "big")
            at += WORD
        items, _ = _decode_tuple([inner] * count, raw, at)
        return items

    if kind.startswith("tuple"):
        values, _ = _decode_tuple(list(field.get("components") or ()), raw, at)
        return tuple(values)

    if kind in ("string", "bytes"):
        length = int.from_bytes(_slice(raw, at, WORD), "big")
        body = _slice(raw, at + WORD, length)
        return body.decode("utf-8", "replace") if kind == "string" else "0x" + body.hex()

    word = _slice(raw, at, WORD)
    if kind == "address":
        from eth_utils import to_checksum_address
        return to_checksum_address(word[12:])
    if kind == "bool":
        return word[-1] != 0
    if kind.startswith("bytes"):
        return "0x" + word[:int(kind[len("bytes"):])].hex()
    if kind.startswith("uint"):
        return int.from_bytes(word, "big")
    if kind.startswith("int"):
        width = int(kind[len("int"):] or 256)
        value = int.from_bytes(word, "big")
        if value >= 1 << 255:
            value -= 1 << 256
        bound = 1 << (width - 1)
        if not -bound <= value < bound:
            # A well-formed intN is SIGN-EXTENDED to 32 bytes. A word that is
            # not decodes to a plausible-looking number outside the declared
            # range — e.g. an int24 of -200 sent unextended reads as 65336 — and
            # returning it would be the same class as treating a truncated
            # response as zero. Refuse instead.
            raise AbiError(
                f"the encoded word is {value}, outside the range of {kind}. A "
                f"well-formed value is sign-extended to 32 bytes; this one is "
                f"not, so it cannot be decoded honestly")
        return value
    raise AbiError(f"unsupported ABI type {kind!r}")


def _slice(raw: bytes, at: int, length: int) -> bytes:
    """A read past the end is a TRUNCATED response, never zeros.

    Returning zeros here would turn a short/garbled RPC reply into a confident
    number — the same class as a failed balance read reported as $0.00.
    """
    if at < 0 or at + length > len(raw):
        raise AbiError(
            f"the encoded data ends at byte {len(raw)} but a field needs bytes "
            f"{at}..{at + length} — the response is truncated, not zero")
    return raw[at:at + length]
