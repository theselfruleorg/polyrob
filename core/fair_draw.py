"""Deterministic compute with no code exec: hashes and the provably fair draw (112 W4).

The ONE home of the draw algorithm. The agent's ``compute`` action and any host script that
draws call :func:`fair_draw`, so a draw the agent re-checks is byte-for-byte the draw the
script wrote.

Stdlib only (keccak256 imports ``eth_utils`` lazily — the ``crypto`` extra); no I/O, no
network. A host script may load this file by path.
"""
import hashlib
from typing import List, Optional, Sequence

HASH_ALGOS = ("sha256", "sha3_256", "keccak256")
MAX_DRAW_IDS = 5000


class ComputeError(ValueError):
    """A refusal the caller shows as written (bad input, a missing extra)."""


def _bytes(data: str, encoding: str) -> bytes:
    if encoding == "text":
        return (data or "").encode("utf-8")
    if encoding == "hex":
        raw = (data or "").strip()
        if raw[:2].lower() == "0x":
            raw = raw[2:]
        try:
            return bytes.fromhex(raw)
        except ValueError:
            raise ComputeError("data is not valid hex") from None
    raise ComputeError("encoding must be 'text' or 'hex'")


def hash_text(algo: str, data: str, encoding: str = "text") -> str:
    """The lowercase hex digest of *data* under *algo*.

    ``sha3_256`` is NIST SHA-3; ``keccak256`` is Ethereum's (they differ). keccak256
    needs the ``crypto`` extra and refuses plainly when it is missing — never a silent
    ``sha3_256``.
    """
    algo = (algo or "").strip().lower()
    raw = _bytes(data, encoding)
    if algo == "sha256":
        return hashlib.sha256(raw).hexdigest()
    if algo == "sha3_256":
        return hashlib.sha3_256(raw).hexdigest()
    if algo == "keccak256":
        try:
            from eth_utils import keccak
        except Exception:
            raise ComputeError("keccak256 needs the crypto extra (eth-utils), "
                               "which is not installed here") from None
        return keccak(raw).hex().removeprefix("0x")
    raise ComputeError(f"algo must be one of {', '.join(HASH_ALGOS)}")


def fair_draw(seed: str, ids: Sequence[str], n: int, *,
              max_ids: Optional[int] = MAX_DRAW_IDS) -> List[str]:
    """Sort *ids* by ``sha256(seed + id)`` (text, UTF-8) and take the first *n*.

    *seed* is used verbatim — for a block hash, the ``0x``-prefixed text the RPC returns;
    stripping the prefix or decoding the hex changes the order. Ties keep the plain string
    order of the ids (a real sha256 tie does not happen). ``n <= 0`` → ``[]``. *max_ids* bounds
    an agent call (``compute``); the collector passes ``None`` — a round is never undrawable.
    """
    ids = [str(i) for i in ids]
    if max_ids is not None and len(ids) > max_ids:
        raise ComputeError(f"at most {max_ids} ids")
    if len(set(ids)) != len(ids):
        raise ComputeError("ids must be unique")
    if not isinstance(seed, str) or not seed:
        raise ComputeError("seed is required")
    n = int(n)
    if n <= 0:
        return []
    order = sorted(sorted(ids),
                   key=lambda uid: hashlib.sha256((seed + uid).encode()).hexdigest())
    return order[:n]
