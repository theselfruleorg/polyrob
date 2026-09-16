"""SPL instruction builders — the tree's first from-scratch Solana authors (042b).

``relay_svm.py`` DECODES instructions a provider handed us. This module AUTHORS
them, which is a different risk: there is no counterparty to blame for a wrong
byte, and a mis-encoded instruction is signed and broadcast exactly as written.

So every layout here is hand-encoded from the SPL Token program's own
``TokenInstruction::pack``, and pinned two ways:
``tests/unit/core/wallet/test_spl_token.py`` asserts the exact bytes AND
cross-checks them against ``spl.token.instructions`` as an independent oracle
(import-skipped, never a runtime dependency — see below).

⚠️ **solders only.** ``solana-py``/``spl`` are an extras-only dependency this
tree pins for the x402 SDK's benefit (``pyproject.toml``: *"Our OWN Solana code
needs only solders"*). Taking a hard import on ``spl`` would put the money path
inside a version range held for somebody else's reason — and it cannot build
``InitializeMint2`` anyway (its instruction enum stops at 17). Imports are lazy
and guarded, matching every other solders call site in the tree.

⚠️ ``COption<Pubkey>`` here is SPL's FIXED-WIDTH form: ``0x01 ++ pubkey`` or
``0x00 ++ 32 zero bytes`` — always 33 bytes, never 1. Borsh's variable-length
Option is a different encoding, and using it produces a length mismatch the
program reports as an opaque ``InvalidInstructionData``.
"""
from __future__ import annotations

import struct
from typing import Any, List, Optional

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
#: Token-2022. The mint is created HERE, not under the classic program, for one
#: reason: it is the only way an SPL token carries its own name and symbol
#: on-chain. Classic SPL has no metadata at all — a token minted there shows as
#: "Unknown" in every explorer and wallet until a separate Metaplex account
#: exists, which is a second program, a second rent payment and a second thing
#: to get wrong. Token-2022's `MetadataPointer` + `TokenMetadata` extensions put
#: it IN the mint. It is also what pump.fun uses, verified by decoding a live
#: launch, so it is the mainstream path rather than a clever one.
#:
#: ⚠️ The trade-off, stated: a few older AMMs do not support Token-2022. Jupiter
#: (our own Solana route) and the major venues do.
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

#: Token-2022 extension instruction: `TokenInstruction::MetadataPointerExtension`.
_METADATA_POINTER_IX = 39
#: A mint with the MetadataPointer extension: the 82-byte base padded to 165,
#: one account-type byte, then a 4-byte TLV header and 64 bytes of pointer
#: state. The METADATA itself is appended by a realloc during
#: `TokenMetadata::Initialize`, which is why the account must be funded for both
#: up front — see `metadata_space`.
MINT_WITH_POINTER_LEN = 165 + 1 + 4 + 64

#: Account sizes, from the SPL Token program's own `Mint::LEN` / `Account::LEN`.
MINT_LEN = 82
TOKEN_ACCOUNT_LEN = 165

#: Rent-exemption is `(128 + size) * lamports_per_byte_year * exemption_threshold`.
#:
#: ⚠️ These are MEASURED, not recalled. The first live simulation against
#: mainnet (2026-09-13) returned 1,066,800 lamports for an 82-byte mint where
#: the widely-quoted 3480/byte-year gives 1,461,600 — a 37% overshoot. The
#: overhead is still 128 bytes and the threshold is still 2.0; the per-byte-year
#: figure is 2540. Derived, not guessed: `getMinimumBalanceForRentExemption(1) -
#: (0)` is exactly 5080, and 5080/2 = 2540.
#:
#: ⚠️ The RPC is AUTHORITATIVE and is read at call time. These exist only for
#: the path where it cannot be reached, and that path adds a deliberate margin
#: (:data:`_FALLBACK_MARGIN`) because the failure directions are not symmetric:
#: over-funding leaves recoverable lamports in an account we own, while
#: under-funding fails two instructions later at `InitializeMint2` with an
#: `InvalidAccountData` a long way from its cause.
_LAMPORTS_PER_BYTE_YEAR = 2540
_EXEMPTION_THRESHOLD = 2
_ACCOUNT_OVERHEAD = 128
#: Applied ONLY when the RPC could not be reached. 25% covers a parameter rise
#: without the caller noticing a thing.
_FALLBACK_MARGIN = 1.25

#: `AuthorityType` in the SPL Token program.
AUTHORITY_MINT_TOKENS = 0
AUTHORITY_FREEZE_ACCOUNT = 1
AUTHORITY_ACCOUNT_OWNER = 2
AUTHORITY_CLOSE_ACCOUNT = 3

#: Solana's hard transaction-size limit. Same constant `relay_svm` refuses on.
MAX_TX_BYTES = 1232


class SplBuildError(RuntimeError):
    """The instruction or transaction could not be built. Always says why."""


def rent_exempt_lamports(space: int) -> int:
    """The rent-exempt minimum for an account of *space* bytes, computed."""
    return (_ACCOUNT_OVERHEAD + int(space)) * _LAMPORTS_PER_BYTE_YEAR * _EXEMPTION_THRESHOLD


def rent_exempt_from_rpc(space: int, rpc) -> int:
    """The RPC's answer, or the formula PLUS a margin when it cannot be reached.

    Fail-OPEN rather than refusing: an unreachable RPC should not stop a
    deployment whose every other bound still holds. But the fallback is
    deliberately generous, because the failure directions are not symmetric —
    see :data:`_FALLBACK_MARGIN`. The measured constants were 37% high once
    already; assuming they cannot drift again is how that happens twice.
    """
    try:
        answer = rpc("getMinimumBalanceForRentExemption", [int(space)])
        if isinstance(answer, int) and answer > 0:
            return answer
    except Exception:
        pass
    return int(rent_exempt_lamports(space) * _FALLBACK_MARGIN)


# ==========================================================================
# Encoding primitives
# ==========================================================================

def _pubkey_bytes(value) -> bytes:
    from solders.pubkey import Pubkey
    if isinstance(value, (bytes, bytearray)):
        if len(value) != 32:
            raise SplBuildError(f"a pubkey is 32 bytes, got {len(value)}")
        return bytes(value)
    if isinstance(value, str):
        return bytes(Pubkey.from_string(value))
    return bytes(value)                       # already a solders Pubkey


def _coption_pubkey(value) -> bytes:
    """SPL's FIXED-WIDTH ``COption<Pubkey>``: 33 bytes either way."""
    if value is None:
        return b"\x00" + bytes(32)
    return b"\x01" + _pubkey_bytes(value)


def _meta(pubkey, *, signer: bool, writable: bool):
    from solders.instruction import AccountMeta
    from solders.pubkey import Pubkey
    key = pubkey if not isinstance(pubkey, str) else Pubkey.from_string(pubkey)
    if isinstance(key, (bytes, bytearray)):
        key = Pubkey.from_bytes(bytes(key))
    return AccountMeta(key, bool(signer), bool(writable))


def _instruction(program_id: str, data: bytes, metas):
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey
    return Instruction(Pubkey.from_string(program_id), data, list(metas))


# ==========================================================================
# Instructions
# ==========================================================================

def ix_create_account(*, payer, new_account, lamports: int, space: int,
                      owner: str):
    """``SystemInstruction::CreateAccount``.

    ⚠️ The discriminator is a **u32** little-endian 0, not a u8 — unlike every
    SPL Token instruction below, whose discriminator is one byte. Getting this
    wrong shifts every subsequent field by three bytes.
    """
    if int(lamports) < 0 or int(space) < 0:
        raise SplBuildError("lamports and space must not be negative")
    data = (struct.pack("<I", 0)
            + struct.pack("<Q", int(lamports))
            + struct.pack("<Q", int(space))
            + _pubkey_bytes(owner))
    return _instruction(SYSTEM_PROGRAM, data, [
        _meta(payer, signer=True, writable=True),
        _meta(new_account, signer=True, writable=True),
    ])


def ix_initialize_mint2(*, mint, decimals: int, mint_authority,
                        freeze_authority=None,
                        token_program: str = TOKEN_2022_PROGRAM):
    """``TokenInstruction::InitializeMint2`` (index 20).

    ``InitializeMint2`` rather than ``InitializeMint`` (index 0) for one
    reason: it drops the rent Sysvar account. Identical 67-byte data layout.
    """
    if not 0 <= int(decimals) <= 9:
        # 9 is Solana's convention (SOL itself) and the most any DEX UI assumes.
        raise SplBuildError(
            f"decimals must be 0..9 on Solana, got {decimals}")
    data = (bytes([20, int(decimals)])
            + _pubkey_bytes(mint_authority)
            + _coption_pubkey(freeze_authority))
    return _instruction(token_program, data, [
        _meta(mint, signer=False, writable=True),
    ])


def ix_create_ata_idempotent(*, payer, owner, mint,
                             token_program: str = TOKEN_2022_PROGRAM):
    """The ATA program's ``CreateIdempotent`` (data ``0x01``), 6 accounts.

    Idempotent on purpose: an ATA that already exists becomes a no-op instead of
    failing the whole transaction. The legacy empty-data form takes SEVEN
    accounts (it carries the rent Sysvar) — mixing the data byte with the wrong
    account count is the trap here.
    """
    from solders.pubkey import Pubkey
    ata, _bump = Pubkey.find_program_address(
        [_pubkey_bytes(owner), _pubkey_bytes(token_program), _pubkey_bytes(mint)],
        Pubkey.from_string(ATA_PROGRAM))
    return _instruction(ATA_PROGRAM, b"\x01", [
        _meta(payer, signer=True, writable=True),
        _meta(ata, signer=False, writable=True),
        _meta(owner, signer=False, writable=False),
        _meta(mint, signer=False, writable=False),
        _meta(SYSTEM_PROGRAM, signer=False, writable=False),
        _meta(token_program, signer=False, writable=False),
    ]), str(ata)


def ix_mint_to(*, mint, destination, authority, amount: int,
               token_program: str = TOKEN_2022_PROGRAM):
    """``TokenInstruction::MintTo`` (index 7). *amount* is RAW base units."""
    if int(amount) <= 0:
        raise SplBuildError("mint amount must be greater than zero")
    if int(amount) >= 2 ** 64:
        raise SplBuildError(
            f"{amount} does not fit in a u64 — Solana supplies are u64, so "
            f"supply x 10**decimals must stay under 18,446,744,073,709,551,616")
    data = bytes([7]) + struct.pack("<Q", int(amount))
    return _instruction(token_program, data, [
        _meta(mint, signer=False, writable=True),
        _meta(destination, signer=False, writable=True),
        _meta(authority, signer=True, writable=False),
    ])


def ix_set_authority(*, account, current_authority, authority_type: int,
                     new_authority=None,
                     token_program: str = TOKEN_2022_PROGRAM):
    """``TokenInstruction::SetAuthority`` (index 6).

    ``new_authority=None`` REVOKES — which for ``AUTHORITY_MINT_TOKENS`` is the
    single most important instruction in a fixed-supply launch: a live mint
    authority means the supply is whatever its holder later decides.
    """
    if authority_type not in (AUTHORITY_MINT_TOKENS, AUTHORITY_FREEZE_ACCOUNT,
                              AUTHORITY_ACCOUNT_OWNER, AUTHORITY_CLOSE_ACCOUNT):
        raise SplBuildError(f"unknown authority type {authority_type}")
    data = bytes([6, int(authority_type)]) + _coption_pubkey(new_authority)
    return _instruction(token_program, data, [
        _meta(account, signer=False, writable=True),
        _meta(current_authority, signer=True, writable=False),
    ])


def _spl_discriminate(namespace: str) -> bytes:
    """``sha256(namespace)[:8]`` — the SPL interface discriminator convention.

    Derived, never pasted. Verified against mainnet by simulation: a transaction
    built with these bytes produces a mint whose `jsonParsed` extensions read
    back the name, symbol and uri that went in.
    """
    import hashlib
    return hashlib.sha256(namespace.encode()).digest()[:8]


def _borsh_string(text: str) -> bytes:
    raw = str(text).encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def metadata_space(*, name: str, symbol: str, uri: str) -> int:
    """Extra bytes the TokenMetadata TLV entry will need.

    The account must be funded for this at CREATION, because
    `TokenMetadata::Initialize` reallocs and a short account fails there rather
    than at the instruction that under-funded it.
    """
    return (4 + 4                       # TLV type + length
            + 32 + 32                   # update_authority + mint
            + len(_borsh_string(name)) + len(_borsh_string(symbol))
            + len(_borsh_string(uri))
            + 4)                        # additional_metadata vec length


def ix_initialize_metadata_pointer(*, mint, authority=None, metadata_address=None):
    """Point the mint at its own metadata (Token-2022 extension 39, sub 0).

    ``metadata_address`` defaults to the MINT ITSELF, which is what makes the
    name and symbol live in the same account rather than a separate one.
    ⚠️ Must come BEFORE `InitializeMint2`: Token-2022 refuses to add an
    extension to an already-initialized mint.
    """
    target = metadata_address if metadata_address is not None else mint
    data = (bytes([_METADATA_POINTER_IX, 0])
            + (bytes(32) if authority is None else _pubkey_bytes(authority))
            + _pubkey_bytes(target))
    return _instruction(TOKEN_2022_PROGRAM, data, [
        _meta(mint, signer=False, writable=True),
    ])


def ix_initialize_token_metadata(*, mint, update_authority, mint_authority,
                                 name: str, symbol: str, uri: str):
    """Write the name, symbol and uri INTO the mint."""
    data = (_spl_discriminate("spl_token_metadata_interface:initialize_account")
            + _borsh_string(name) + _borsh_string(symbol) + _borsh_string(uri))
    return _instruction(TOKEN_2022_PROGRAM, data, [
        _meta(mint, signer=False, writable=True),          # the metadata account
        _meta(update_authority, signer=False, writable=False),
        _meta(mint, signer=False, writable=False),
        _meta(mint_authority, signer=True, writable=False),
    ])


def ix_revoke_metadata_authority(*, mint, current_authority):
    """Set the metadata UPDATE authority to none.

    Without this the name, symbol and uri stay editable forever by whoever holds
    it. A token whose supply is fixed but whose NAME can be swapped later is
    only half immutable, and the half that moves is the half a buyer reads.
    """
    data = (_spl_discriminate("spl_token_metadata_interface:update_the_authority")
            + bytes(32))
    return _instruction(TOKEN_2022_PROGRAM, data, [
        _meta(mint, signer=False, writable=True),
        _meta(current_authority, signer=True, writable=False),
    ])


# ==========================================================================
# The transaction
# ==========================================================================

def build_fixed_supply_mint(*, payer: str, decimals: int, supply_raw: int,
                            recent_blockhash: str, mint_rent: int,
                            name: str, symbol: str, uri: str = "",
                            mint_keypair=None):
    """``(tx, mint_keypair, mint_address, ata_address)`` for a fixed-supply token.

    Eight instructions, and the SHAPE is the safety argument. Everything that
    could later be changed is closed in the SAME transaction that opens it:

    1. create the mint under **Token-2022**, funded for the metadata it is about
       to grow;
    2. point it at ITSELF for metadata (must precede initialization — Token-2022
       refuses to add an extension to an initialized mint);
    3. initialize with **no freeze authority at all** — one that exists even for
       five instructions is a window that does not need to exist, and the delta
       parser's own docstring calls freeze the Solana shape of a honeypot;
    4. write the name, symbol and uri INTO the mint;
    5. **revoke the metadata update authority** — a token whose supply is fixed
       but whose NAME can be swapped later is only half immutable, and the half
       that moves is the half a buyer reads;
    6. create our associated token account;
    7. mint the WHOLE supply to it;
    8. **revoke the mint authority.**

    After (8) nothing about this token can change, and every part of that is
    observable on-chain rather than promised.

    ⚠️ TWO signers: the payer and the brand-new mint, which must sign its own
    creation. ``SolanaSigner.sign_transaction`` hands solders ONE keypair and
    raises ``SignerError: not enough signers`` — use ``sign_transaction_with``.
    """
    try:
        from solders.hash import Hash
        from solders.keypair import Keypair
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.signature import Signature
        from solders.transaction import VersionedTransaction
    except ImportError as exc:                     # pragma: no cover - env guard
        raise SplBuildError(
            "solders is not installed — pip install 'polyrob[solana]'") from exc

    name = str(name or "").strip()
    symbol = str(symbol or "").strip()
    if not name or not symbol:
        raise SplBuildError(
            "a token needs a name and a symbol — they are written on-chain and "
            "are what every explorer and wallet shows")

    mint_kp = mint_keypair or Keypair()
    mint = str(mint_kp.pubkey())
    payer_key = Pubkey.from_string(str(payer))

    ata_ix, ata = ix_create_ata_idempotent(payer=payer, owner=payer, mint=mint)
    instructions = [
        ix_create_account(payer=payer, new_account=mint, lamports=int(mint_rent),
                          space=MINT_WITH_POINTER_LEN, owner=TOKEN_2022_PROGRAM),
        ix_initialize_metadata_pointer(mint=mint),
        ix_initialize_mint2(mint=mint, decimals=decimals,
                            mint_authority=payer, freeze_authority=None),
        ix_initialize_token_metadata(mint=mint, update_authority=payer,
                                     mint_authority=payer, name=name,
                                     symbol=symbol, uri=uri or ""),
        ix_revoke_metadata_authority(mint=mint, current_authority=payer),
        ata_ix,
        ix_mint_to(mint=mint, destination=ata, authority=payer,
                   amount=int(supply_raw)),
        ix_set_authority(account=mint, current_authority=payer,
                         authority_type=AUTHORITY_MINT_TOKENS,
                         new_authority=None),
    ]

    # Empty ALT list, for the reason relay_svm records: a mis-decoded lookup
    # table silently resolves an account index to the WRONG account in a
    # transaction we then sign. This one measures ~734 bytes and needs none.
    message = MessageV0.try_compile(
        payer_key, instructions, [], Hash.from_string(str(recent_blockhash)))
    if str(message.account_keys[0]) != str(payer):
        raise SplBuildError(
            f"the compiled fee payer is {message.account_keys[0]}, not {payer}")

    placeholders = [Signature.default()] * message.header.num_required_signatures
    tx = VersionedTransaction.populate(message, placeholders)
    size = len(bytes(tx))
    if size > MAX_TX_BYTES:
        raise SplBuildError(
            f"REFUSED: the compiled transaction is {size} bytes, over Solana's "
            f"{MAX_TX_BYTES}-byte limit — the name, symbol or uri is too long. "
            f"This path deliberately does not compress with address-lookup "
            f"tables.")
    return tx, mint_kp, mint, ata


def decode_mint_account(info: Any) -> Optional[dict]:
    """``{decimals, supply, mint_authority, freeze_authority}`` from a
    ``jsonParsed`` mint account, or None when it is not one.

    This is how the revocation is PROVEN after confirmation: the simulation's
    ``authority_grants`` taxonomy reads token-ACCOUNT fields and is structurally
    blind to ``SetAuthority`` on a MINT, so "the mint authority is gone" has to
    be observed directly rather than inferred from the deltas.
    """
    try:
        parsed = ((info or {}).get("value") or {}).get("data") or {}
        if not isinstance(parsed, dict):
            return None
        if str(parsed.get("program") or "") not in ("spl-token", "spl-token-2022"):
            return None
        fields = (parsed.get("parsed") or {}).get("info") or {}
        if not fields:
            return None
        out = {
            "decimals": fields.get("decimals"),
            "supply": int(fields.get("supply") or 0),
            "mint_authority": fields.get("mintAuthority"),
            "freeze_authority": fields.get("freezeAuthority"),
            "initialized": bool(fields.get("isInitialized")),
            # Token-2022 native metadata. Absent on a classic mint, which is
            # exactly the "Unknown token" case this path exists to avoid.
            "name": None, "symbol": None, "uri": None,
            "update_authority": None, "has_metadata": False,
        }
        for entry in (fields.get("extensions") or []):
            if str(entry.get("extension") or "") != "tokenMetadata":
                continue
            state = entry.get("state") or {}
            out.update({
                "name": state.get("name"), "symbol": state.get("symbol"),
                "uri": state.get("uri"),
                "update_authority": state.get("updateAuthority"),
                "has_metadata": True,
            })
        return out
    except Exception:
        return None


def signer_pubkeys(tx) -> List[str]:
    """Every pubkey the compiled message REQUIRES a signature from."""
    header = tx.message.header
    keys = list(tx.message.account_keys)
    return [str(k) for k in keys[:header.num_required_signatures]]
