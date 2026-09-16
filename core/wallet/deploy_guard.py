"""Deploy-specific measurement for ``tx_guard`` (042).

A CREATE transaction is the one shape the guard's delta model did not cover. It
has **no destination** and **no counterparty**; the thing it produces does not
exist until it exists. So the questions the other verbs answer ("where is the
money going, and does the simulation agree") have to be replaced with different
ones, which this module answers:

* Is it actually a create, or a call wearing a deploy's name?
* Is the init code bounded (EIP-3860) and the runtime it produces bounded
  (EIP-170)?
* **Does it produce code at all?** ``eth_simulateV1`` returns the deployed
  runtime bytecode as the create call's return data. An empty return is a
  constructor that reverted quietly or self-destructed — never a deployment.
* **Is the code the code that was declared?** For a pinned template the runtime
  is compared BYTE FOR BYTE against the checked-in artifact, excluding only the
  immutable slots the compiler itself recorded. This is what lets the agent say
  "this token has no mint function" and be held to it.
* **Does the constructor move anything it was not declared to?** A constructor
  is arbitrary code running as the wallet. It can transfer tokens and it can
  grant allowances. Any token outflow or any allowance grant refuses outright.

⚠️ This module decides NOTHING. It measures, and returns a reason. Every policy
question — the pause, the turn origin, the pinned RPC, the caps, the approval
lane — stays in ``tx_guard.authorize``, which is THE choke point and must stay
the only one. A second authorizer is exactly the two-gates-that-do-not-know-
about-each-other failure that made the owner tap twice for one bridge (039).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

#: EIP-170: the maximum RUNTIME code size a contract may have. A deployment
#: producing more is rejected by consensus, so signing it burns the fee for
#: nothing.
MAX_RUNTIME_BYTES = 24_576
#: EIP-3860: the maximum INIT code size a create transaction may carry.
MAX_INIT_BYTES = 49_152

#: Arachnid's deterministic deployment proxy — the canonical CREATE2 factory,
#: deployed at the SAME address on every EVM chain from one presigned
#: transaction. Verified live 2026-09-13 on base, robinhood, ethereum, arbitrum
#: and polygon: 69 bytes of code on each, all five hashing to
#: :data:`CREATE2_FACTORY_CODE_HASH`.
#:
#: Its calldata is ``salt(32) ++ init_code`` and it returns the 20-byte address.
#: What that buys: the SAME address on every chain for the same bytes, and
#: vanity addresses by salt search.
CREATE2_FACTORY = "0x4e59b44847b379578588920cA78FbF26c0B4956C"
CREATE2_FACTORY_CODE_HASH = (
    "0x2fa86add0aed31f33a762c9d88e807c475bd51d0f52bd0955754b2608f7e4989")


@dataclass(frozen=True)
class DeployFacts:
    """What the simulation proved about the contract that is about to exist."""
    predicted_address: str
    runtime_size: int
    runtime_hash: str
    #: True when the produced runtime matched a pinned template exactly (modulo
    #: its declared immutable slots). False when the caller supplied raw
    #: bytecode and nothing could be compared against.
    template_matched: bool


def _hex_bytes(value: Optional[str]) -> Optional[bytes]:
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    try:
        return bytes.fromhex(value[2:])
    except ValueError:
        return None


def _rlp_encode_create(sender: bytes, nonce: int) -> bytes:
    """RLP of ``[sender, nonce]`` — the only RLP shape this tree needs.

    Hand-written rather than pulled from a library because it is nine lines and
    one shape, and because a wrong answer here is silent: it would name an
    address that is not the one the contract lands at, which the caller would
    then publish.
    """
    def _str_item(raw: bytes) -> bytes:
        if len(raw) == 1 and raw[0] < 0x80:
            return raw
        if len(raw) <= 55:
            return bytes([0x80 + len(raw)]) + raw
        length = len(raw).to_bytes((len(raw).bit_length() + 7) // 8, "big")
        return bytes([0xb7 + len(length)]) + length + raw

    if nonce < 0:
        raise ValueError("nonce cannot be negative")
    nonce_bytes = b"" if nonce == 0 else nonce.to_bytes(
        (nonce.bit_length() + 7) // 8, "big")
    payload = _str_item(sender) + _str_item(nonce_bytes)
    if len(payload) <= 55:
        return bytes([0xc0 + len(payload)]) + payload
    length = len(payload).to_bytes((len(payload).bit_length() + 7) // 8, "big")
    return bytes([0xf7 + len(length)]) + length + payload


def predict_create_address(sender: str, nonce: int) -> str:
    """``keccak(rlp([sender, nonce]))[12:]`` — where a CREATE lands.

    Stated BEFORE broadcast so the caller can name the address it is about to
    create, rather than discovering it from a receipt it may never get.
    """
    from eth_utils import keccak, to_checksum_address
    raw = _hex_bytes(sender if sender.startswith("0x") else "0x" + sender)
    if raw is None or len(raw) != 20:
        raise ValueError(f"sender is not a 20-byte address: {sender!r}")
    digest = keccak(_rlp_encode_create(raw, int(nonce)))
    return to_checksum_address(digest[-20:])


def predict_create2_address(salt, init_code, *,
                            factory: str = CREATE2_FACTORY) -> str:
    """``keccak(0xff ++ factory ++ salt ++ keccak(init_code))[12:]``.

    ⚠️ This is a CRYPTOGRAPHIC COMMITMENT, and that is why the CREATE2 path can
    prove what it deploys without reading the runtime back. The address is a
    hash OF the init code: if the factory returns the address we computed from
    OUR bytes, the contract at it was created from our bytes and no others.
    Stronger than the byte comparison the direct-CREATE path does, and free.

    Verified against the live factory's own answer on base and robinhood
    (2026-09-13): identical address on both, which is the other half of what
    this buys — one address for a token on every chain.
    """
    from eth_utils import keccak, to_checksum_address
    raw_salt = _hex_bytes(salt if isinstance(salt, str) else "0x" + bytes(salt).hex())
    if raw_salt is None or len(raw_salt) != 32:
        raise ValueError(f"salt must be 32 bytes, got {salt!r}")
    code = _hex_bytes(init_code)
    if not code:
        raise ValueError("init code is empty or not valid hex")
    addr = _hex_bytes(factory)
    if addr is None or len(addr) != 20:
        raise ValueError(f"factory is not a 20-byte address: {factory!r}")
    return to_checksum_address(
        keccak(b"\xff" + addr + raw_salt + keccak(code))[-20:])


def create2_calldata(salt, init_code: str) -> str:
    """The factory's whole ABI: ``salt(32) ++ init_code``. No selector."""
    raw_salt = _hex_bytes(salt if isinstance(salt, str) else "0x" + bytes(salt).hex())
    if raw_salt is None or len(raw_salt) != 32:
        raise ValueError(f"salt must be 32 bytes, got {salt!r}")
    code = _hex_bytes(init_code)
    if not code:
        raise ValueError("init code is empty or not valid hex")
    return "0x" + raw_salt.hex() + code.hex()


def mine_vanity_salt(init_code: str, *, prefix: str = "", suffix: str = "",
                     max_attempts: int = 5_000_000,
                     factory: str = CREATE2_FACTORY):
    """``(salt, address, attempts)`` for an address matching *prefix*/*suffix*.

    Pure and bounded. A four-character prefix is ~65k attempts (about a second);
    each extra character is 16x, so the ceiling is what stops a caller asking
    for eight characters and waiting a week. Returns ``(None, None, attempts)``
    on exhaustion rather than raising — not finding one is an answer.

    ⚠️ Case-INSENSITIVE on purpose. EIP-55 checksum case is a function of the
    address, so demanding a specific case would multiply the work for a
    property nothing reads.
    """
    import os
    from eth_utils import keccak, to_checksum_address

    want_pre = (prefix or "").lower().removeprefix("0x")
    want_suf = (suffix or "").lower()
    for text, label in ((want_pre, "prefix"), (want_suf, "suffix")):
        if text and any(c not in "0123456789abcdef" for c in text):
            raise ValueError(f"{label} {text!r} is not hex — an address has only 0-9a-f")
    if len(want_pre) + len(want_suf) > 8:
        raise ValueError(
            "a pattern longer than 8 hex characters needs ~4 billion attempts; "
            "ask for fewer")

    addr = _hex_bytes(factory)
    code_hash = keccak(_hex_bytes(init_code))
    head = b"\xff" + addr
    for attempt in range(1, int(max_attempts) + 1):
        salt = os.urandom(32)
        digest = keccak(head + salt + code_hash)[-20:].hex()
        if want_pre and not digest.startswith(want_pre):
            continue
        if want_suf and not digest.endswith(want_suf):
            continue
        return "0x" + salt.hex(), to_checksum_address("0x" + digest), attempt
    return None, None, int(max_attempts)


def runtime_matches_template(produced: bytes, template: bytes,
                             immutable_slots: Sequence[Tuple[int, int]]) -> Optional[str]:
    """None when *produced* IS *template*; otherwise why it is not.

    ``immutable_slots`` are ``(offset, length)`` byte ranges solc fills at
    construction time (the artifact's ``immutableReferences``). They are the
    ONLY bytes allowed to differ, and they are excluded by position, never by
    "ignore any difference" — a template comparison that tolerates unknown
    differences is not a comparison.
    """
    if len(produced) != len(template):
        return (f"the deployment produces {len(produced)} bytes of runtime code "
                f"but the pinned template is {len(template)} bytes — this is not "
                f"the contract that was declared")
    mutable = bytearray(len(template))
    for (offset, length) in immutable_slots:
        if offset < 0 or length < 0 or offset + length > len(template):
            return (f"declared immutable slot ({offset}, {length}) falls outside "
                    f"the {len(template)}-byte template — refusing to compare "
                    f"against a template this caller cannot describe")
        for i in range(offset, offset + length):
            mutable[i] = 1
    for i, (a, b) in enumerate(zip(produced, template)):
        if a != b and not mutable[i]:
            return (f"the produced runtime differs from the pinned template at "
                    f"byte {i} (0x{a:02x} vs 0x{b:02x}), outside every declared "
                    f"immutable slot — this is not the contract that was declared")
    return None


def _assert_create2(intent, deltas, salt):
    """``(facts, refusal)`` for the DETERMINISTIC path.

    The proof is the ADDRESS. ``keccak(0xff ++ factory ++ salt ++
    keccak(init_code))`` is a commitment to the init code, so a factory that
    returns the address we computed from OUR bytes has created the contract from
    our bytes and no others. That is a stronger statement than the direct path's
    byte comparison of the produced runtime, and it costs one hash.
    """
    from eth_utils import keccak, to_checksum_address

    returned = _hex_bytes(deltas.return_data)
    if not returned:
        return None, ("refused: the CREATE2 factory returned nothing. It reverts "
                      "when the address is already occupied — pick another salt")
    if len(returned) < 20:
        return None, (f"refused: the CREATE2 factory returned {len(returned)} "
                      f"bytes, not an address")
    landed = to_checksum_address("0x" + returned[-20:].hex())
    if int(landed, 16) == 0:
        return None, ("refused: the CREATE2 factory returned the zero address, "
                      "which is how it reports a failed deployment")
    try:
        expected = predict_create2_address(salt, intent.init_code)
    except Exception as exc:
        return None, f"refused: could not predict the CREATE2 address ({exc})"
    if landed.lower() != expected.lower():
        return None, (
            f"refused: the factory would deploy to {landed}, but this init code "
            f"and salt commit to {expected}. The address is a hash OF the code, "
            f"so a mismatch means the bytes being deployed are not the bytes that "
            f"were declared")

    code = _hex_bytes(intent.init_code) or b""
    return DeployFacts(
        predicted_address=landed,
        runtime_size=0,
        runtime_hash="0x" + keccak(code).hex(),
        template_matched=bool(intent.expected_runtime)), None


def assert_deploy(intent, deltas, *, holder: str, nonce: Optional[int]):
    """``(facts, refusal)`` — exactly one is None.

    *intent* is a ``tx_guard.TxIntent`` with ``is_deploy=True``; *deltas* is the
    ``simulation.Deltas`` for the already-simulated create.
    """
    init = _hex_bytes(intent.init_code)
    if not init:
        return None, "refused: init code is empty or not valid hex — there is nothing to deploy"
    if len(init) > MAX_INIT_BYTES:
        return None, (f"refused: {len(init)} bytes of init code exceeds the "
                      f"{MAX_INIT_BYTES}-byte EIP-3860 limit — consensus would "
                      f"reject it and the fee would be burned for nothing")

    salt = getattr(intent, "create2_salt", None)
    if salt:
        facts, why = _assert_create2(intent, deltas, salt)
        if why:
            return None, why
    else:
        facts = None

    runtime = _hex_bytes(deltas.return_data)
    if facts is None and runtime is None:
        return None, ("refused: the simulation reported no return data for the "
                      "create, so the runtime code cannot be inspected — an "
                      "un-inspected deployment is not an authorized one")
    if facts is None:
        if not runtime:
            return None, ("refused: the constructor produces EMPTY runtime code. "
                          "The deployment would burn the fee and leave an address "
                          "with nothing at it")
        if len(runtime) > MAX_RUNTIME_BYTES:
            return None, (f"refused: the constructor produces {len(runtime)} bytes "
                          f"of runtime code, above the {MAX_RUNTIME_BYTES}-byte "
                          f"EIP-170 limit — the deployment cannot succeed")

    # A constructor runs as the wallet. Anything it moves that is not the
    # declared native value is undeclared, and undeclared is refused — there is
    # no legitimate reason for a deployment to spend a token or grant a claim.
    for token, moved in (deltas.token_deltas or {}).items():
        if moved < 0:
            return None, (f"refused: the constructor moves {-moved} of token "
                          f"{token} out of the wallet — a deployment must not "
                          f"spend anything but its declared value and its fee")
    for (l_token, l_to, l_amount) in (deltas.holder_transfers or ()):
        if l_amount > 0:
            return None, (f"refused: the constructor emits a Transfer of "
                          f"{l_amount} on {l_token} from the wallet to {l_to} — "
                          f"a deployment that moves funds is not a deployment")
    for (l_token, l_spender, l_amount) in (deltas.holder_approvals or ()):
        if l_amount > 0:
            return None, (f"refused: the constructor grants an allowance of "
                          f"{l_amount} on {l_token} to {l_spender} — a standing "
                          f"claim on the wallet is not bounded by any cap here, "
                          f"because the drain happens in a later transaction")
    for (token, spender), change in (deltas.allowance_deltas or {}).items():
        if change > 0:
            return None, (f"refused: the constructor grants an allowance of "
                          f"{change} on {token} to {spender}")

    if facts is not None:
        # The CREATE2 path proved itself by address. Nothing below applies: the
        # return data is 20 bytes of address, not runtime, and the template
        # comparison is subsumed by the commitment.
        return facts, None

    template_matched = False
    if intent.expected_runtime:
        template = _hex_bytes(intent.expected_runtime)
        if template is None:
            return None, ("refused: the declared template runtime is not valid "
                          "hex, so nothing could be compared against it")
        why = runtime_matches_template(runtime, template, intent.immutable_slots)
        if why:
            return None, f"refused: {why}"
        template_matched = True

    if nonce is None:
        return None, ("refused: the transaction carries no nonce, so the address "
                      "this contract would land at cannot be predicted — "
                      "deploying to an address we cannot name is not authorized")
    try:
        address = predict_create_address(holder, int(nonce))
    except Exception as exc:
        return None, f"refused: could not predict the deployment address ({exc})"

    from eth_utils import keccak
    return DeployFacts(predicted_address=address,
                       runtime_size=len(runtime),
                       runtime_hash="0x" + keccak(runtime).hex(),
                       template_matched=template_matched), None
