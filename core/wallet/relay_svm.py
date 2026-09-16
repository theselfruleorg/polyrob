"""Build a signable Solana transaction from Relay's instruction payload (037).

Relay's Solana-origin order arrives as a list of raw instructions
(`{programId, keys[], data}`) plus a set of address-lookup tables, not as a
ready-made transaction the way Jupiter's does. This module is the one place that
turns that payload into a `VersionedTransaction`.

**The lookup tables are deliberately NOT used.** An ALT only COMPRESSES a
message — it maps account addresses to one-byte indices so a large transaction
fits in a packet. Relay hands us every account as a full pubkey inside
`keys[]`, so compiling with an empty ALT list produces an equivalent
transaction that simply carries the addresses inline. Measured on the live
SOL -> Robinhood order: one instruction, five accounts, 213 bytes — an order of
magnitude under the 1232-byte limit.

That choice removes the single largest failure mode in this path. Using the
tables would mean fetching each ALT account over RPC and hand-decoding its
binary layout (a 56-byte header, then packed 32-byte addresses); a
mis-parse there does not fail loudly, it silently resolves an account index to
the WRONG ACCOUNT, and the transaction that results is signed by us. We take
the larger packet and keep the addresses we were given.

If a future route ever exceeds the packet limit, the fix is to REFUSE it here
with a clear message — never to start decoding tables in the path that moves
funds.
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: A Solana transaction must fit one UDP packet. Anything larger cannot land, so
#: it is refused at build time rather than at broadcast time.
MAX_TX_BYTES = 1232


class RelaySvmBuildError(RuntimeError):
    """The payload could not be turned into a signable transaction."""


def _decode_data(raw: Any) -> bytes:
    """Relay sends instruction data as hex. Base64 is accepted as a fallback, but
    an UNDECODABLE payload raises — never silently becomes empty bytes, which
    would build a transaction that calls a program with no arguments."""
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    s = str(raw or "").strip()
    if not s:
        raise RelaySvmBuildError("instruction carries no data")
    if s.startswith("0x"):
        s = s[2:]
    try:
        return bytes.fromhex(s)
    except ValueError:
        pass
    try:
        return base64.b64decode(s, validate=True)
    except Exception as exc:
        raise RelaySvmBuildError(
            f"instruction data is neither hex nor base64: {s[:40]!r}") from exc


def build_transaction(*, instructions: List[Dict[str, Any]], payer: str,
                      recent_blockhash: str):
    """An UNSIGNED `VersionedTransaction` for *instructions*.

    The payer must be the wallet's own address: it is the account that pays the
    fee and, in Relay's deposit instruction, the account the funds leave.
    """
    try:
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.signature import Signature
        from solders.transaction import VersionedTransaction
    except ImportError as exc:                       # pragma: no cover - env guard
        raise RelaySvmBuildError(
            "solders is not installed — the Solana bridge leg cannot be built") from exc

    if not instructions:
        raise RelaySvmBuildError("no instructions to build")

    payer_key = Pubkey.from_string(str(payer))
    compiled = []
    for i, ix in enumerate(instructions):
        program_id = str((ix or {}).get("programId") or "").strip()
        if not program_id:
            raise RelaySvmBuildError(f"instruction #{i} has no programId")
        metas = []
        for j, k in enumerate((ix or {}).get("keys") or []):
            pubkey = str((k or {}).get("pubkey") or "").strip()
            if not pubkey:
                raise RelaySvmBuildError(
                    f"instruction #{i} account #{j} has no pubkey")
            metas.append(AccountMeta(
                Pubkey.from_string(pubkey),
                bool(k.get("isSigner")), bool(k.get("isWritable"))))
        if not metas:
            raise RelaySvmBuildError(f"instruction #{i} names no accounts")
        compiled.append(Instruction(
            Pubkey.from_string(program_id), _decode_data(ix.get("data")), metas))

    # Empty ALT list on purpose — see the module docstring.
    message = MessageV0.try_compile(
        payer_key, compiled, [], Hash.from_string(str(recent_blockhash)))

    # ⚠️ PLACEHOLDER signatures, one per required signer — not an empty list.
    # A transaction is only well-formed when `len(signatures) ==
    # header.num_required_signatures`. Built with `[]`, the message says it needs
    # one signature and carries none, and the RPC rejects the whole thing with
    # "Transaction failed to sanitize accounts offsets correctly" — an error that
    # names offsets and has nothing to do with them. Found on the first prod dry
    # run that got this far (2026-09-11).
    #
    # These are zeroed placeholders, NOT signatures: the signer replaces them
    # wholesale (`SolanaSigner.sign_transaction` rebuilds from `tx.message`), and
    # a zero signature can never validate on-chain. They exist so the bytes are
    # shaped correctly for `simulateTransaction`, which runs with sigVerify off.
    placeholders = [Signature.default()] * message.header.num_required_signatures
    tx = VersionedTransaction.populate(message, placeholders)

    size = len(bytes(tx))
    if size > MAX_TX_BYTES:
        raise RelaySvmBuildError(
            f"REFUSED: the compiled transaction is {size} bytes, over Solana's "
            f"{MAX_TX_BYTES}-byte limit. This route needs address-lookup-table "
            f"compression, which this path deliberately does not do — decoding a "
            f"table wrong resolves an account to the WRONG address in a "
            f"transaction we then sign. Refuse the route instead.")
    return tx


def signer_accounts(instructions: List[Dict[str, Any]]) -> set:
    """Every account the payload marks `isSigner`.

    The caller asserts this is exactly {our address}: an instruction that
    requires a SECOND signer is not an order we can fulfil, and one that
    requires a DIFFERENT signer is not our order at all.
    """
    out = set()
    for ix in instructions or []:
        for k in (ix or {}).get("keys") or []:
            if (k or {}).get("isSigner"):
                pk = str((k or {}).get("pubkey") or "").strip()
                if pk:
                    out.add(pk)
    return out
