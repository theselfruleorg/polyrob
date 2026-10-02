"""``WALLET_SIGNER=remote`` — the agent process holds no key (066 §5.1).

:class:`RemoteWallet` is an :class:`~core.wallet.agent_wallet.AgentWallet` that
knows every address (from the signer's ``identity``, checked against the
published ``public_identity.json``) and signs NOTHING itself:

* an EVM send leaves through :meth:`RemoteEvmSigner.send_transaction` →
  ``evm.send`` (``EvmRail.sign_and_send`` routes there);
* x402 (EIP-3009) goes through :class:`RemoteAccount.sign_typed_data`, which
  the x402 SDK's ``EthAccountSigner`` calls exactly as it calls a LocalAccount;
* Hyperliquid order signing (066 P3) goes through
  :meth:`RemoteAccount.sign_hl_l1_action`;
* everything else a LocalAccount could do — sign an arbitrary message, typed
  data, a raw transaction, hand out the private key — raises
  :class:`~core.wallet.agent_wallet.WalletSigningUnavailable`, naming why.

Solana signing is not served by the signer yet: every Solana signing attempt
refuses by name (fail closed) while the address stays readable.

``remote_verified()`` is the latch ``host_execution_refusal`` reads (066 §5.6):
True only once this process (a) holds no custody secret and (b) got the
signer's identity and it matched the wallet of record.
"""
import logging
import threading
from typing import Any, Dict, Optional

from core.wallet.agent_wallet import (VENUES, PublicOnlySigner, PublicOnlyWallet,
                                      WalletSigningUnavailable)

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_verified = False
_verify_detail = "remote signer not built in this process"


def remote_verified() -> bool:
    with _lock:
        return _verified


def remote_state() -> Dict[str, Any]:
    with _lock:
        return {"verified": _verified, "detail": _verify_detail}


def _set_verified(value: bool, detail: str) -> None:
    global _verified, _verify_detail
    with _lock:
        _verified, _verify_detail = bool(value), str(detail)


def _reset_for_tests() -> None:
    _set_verified(False, "remote signer not built in this process")


def _refusal(what: str) -> str:
    return (f"cannot {what} here — WALLET_SIGNER=remote: the key lives in polyrob-signer, "
            f"which signs only known intents (EVM sends after tx_guard, x402, Hyperliquid "
            f"orders). There is no generic signing (066 §5.2).")


class _Signed:
    """The one attribute ``EthAccountSigner`` reads off a signed message."""

    def __init__(self, signature: bytes):
        self.signature = signature


def _hexify(value):
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    if isinstance(value, dict):
        return {k: _hexify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_hexify(v) for v in value]
    return value


class RemoteAccount:
    """A LocalAccount-shaped object whose only abilities are the signer's schemas."""

    def __init__(self, client, address: str, venue: str):
        self._client = client
        self._address = address
        self._venue = venue

    @property
    def address(self) -> str:
        return self._address

    @property
    def key(self):
        raise WalletSigningUnavailable(_refusal("hand out a private key"))

    def sign_message(self, *_a, **_k):
        raise WalletSigningUnavailable(_refusal("sign an arbitrary message"))

    def sign_transaction(self, *_a, **_k):
        raise WalletSigningUnavailable(_refusal("sign a raw transaction"))

    def unsafe_sign_hash(self, *_a, **_k):
        raise WalletSigningUnavailable(_refusal("sign a raw hash"))

    def sign_typed_data(self, domain_data=None, message_types=None, message_data=None,
                        full_message=None):
        """x402 only: an EIP-3009 TransferWithAuthorization. The signer checks
        the USDC domain, the window, the amount and its own caps."""
        if full_message is not None:
            domain_data = full_message.get("domain")
            message_types = full_message.get("types")
            message_data = full_message.get("message")
        types = {k: v for k, v in (message_types or {}).items() if k != "EIP712Domain"}
        if list(types) != ["TransferWithAuthorization"]:
            raise WalletSigningUnavailable(_refusal(f"sign typed data {sorted(types)}"))
        result = self._client.call("x402.authorize", {
            "domain": _hexify(dict(domain_data or {})), "types": _hexify(message_types or {}),
            "primary_type": "TransferWithAuthorization",
            "message": _hexify(dict(message_data or {}))})
        return _Signed(bytes.fromhex(str(result["signature"])[2:]))

    def sign_hl_l1_action(self, action, active_pool, nonce, expires_after, is_mainnet):
        """066 P3: a Hyperliquid L1 ORDER action; the signer re-hashes it."""
        if self._venue != "hyperliquid":
            raise WalletSigningUnavailable(_refusal("sign a Hyperliquid action with a non-HL key"))
        body = {"venue": "hyperliquid", "action": _hexify(action), "nonce": int(nonce),
                "is_mainnet": bool(is_mainnet)}
        if expires_after is not None:
            body["expires_after"] = int(expires_after)
        if active_pool:
            body["vault_address"] = str(active_pool)
        return self._client.call("venue.sign", body)["signature"]

    def __repr__(self) -> str:
        return f"<RemoteAccount {self._venue} {self._address} (key in polyrob-signer)>"


class RemoteEvmSigner:
    """The ``Signer`` interface the rails use, with the key in another process."""

    #: ``EvmRail.sign_and_send`` reads this and sends through the signer.
    remote_send = True

    def __init__(self, client, address: str, venue: str, *, verified_fn=remote_verified):
        self._client = client
        self._address = address
        self._venue = venue
        self._verified_fn = verified_fn

    @property
    def address(self) -> str:
        return self._address

    @property
    def account(self) -> RemoteAccount:
        return RemoteAccount(self._client, self._address, self._venue)

    def sign_message(self, data):
        """ONLY the account journal template (C1): ``journal.sign`` in the signer, which re-checks
        the template. The signer has no generic message signing (066 §5): every other message
        refuses here."""
        from core.wallet.account_journal import is_journal_template
        if not is_journal_template(bytes(data) if isinstance(data, (bytes, bytearray)) else b""):
            raise WalletSigningUnavailable(_refusal("sign a message"))
        if not self._verified_fn():
            raise WalletSigningUnavailable(
                "the remote signer is not VERIFIED in this process — refusing to sign")
        result = self._client.call("journal.sign", {"message": bytes(data).decode("utf-8")})
        if str(result.get("address") or "").lower() != self._address.lower():
            raise WalletSigningUnavailable(
                f"the signer signed the journal with {result.get('address')}, not {self._address}")
        return result["signature"]

    def sign_typed_data(self, domain, types, message):
        raise WalletSigningUnavailable(_refusal("sign typed data"))

    def sign_transaction(self, tx):
        raise WalletSigningUnavailable(_refusal("sign a transaction without sending it"))

    def sign_transaction_with(self, tx, extra_keypairs):
        raise WalletSigningUnavailable(_refusal("sign a transaction"))

    def send_transaction(self, tx: Dict[str, Any], *, chain: str, intent) -> Dict[str, Any]:
        from core.signer import protocol
        if not self._verified_fn():
            raise WalletSigningUnavailable(
                "the remote signer is not VERIFIED in this process (unreachable at start, "
                "or its wallet differs from the wallet of record) — refusing to send; "
                "see the custody status section")
        return self._client.call("evm.send", {"intent": protocol.intent_to_wire(intent),
                                              "tx": protocol.tx_to_wire(tx)})

    def __repr__(self) -> str:
        return f"<RemoteEvmSigner {self._venue} {self._address} (key in polyrob-signer)>"


class RemoteSolanaSigner(PublicOnlySigner):
    def _refusal(self, what: str) -> str:
        return (f"cannot {what} for svm — WALLET_SIGNER=remote and the signer does not "
                f"serve Solana signing yet (066 P2 residual). The address {self.address} "
                f"is correct; set WALLET_SIGNER=local to trade on Solana.")


class RemoteWallet(PublicOnlyWallet):
    """Every address, no key; signatures come from ``polyrob-signer``."""

    def __init__(self, config, identity, client, audit_sink=None, on_record=None):
        self._client = client
        super().__init__(config, identity, audit_sink=audit_sink, on_record=on_record)

    @property
    def signing_available(self) -> bool:
        return True

    @property
    def signer_client(self):
        return self._client

    def signer_for(self, venue: str) -> RemoteEvmSigner:
        if venue not in VENUES:
            raise ValueError(f"unknown venue '{venue}' (expected one of {sorted(VENUES)})")
        address = self._addresses.get(venue)
        if not address:
            raise WalletSigningUnavailable(
                f"the signer's identity names no address for venue '{venue}'")
        return RemoteEvmSigner(self._client, address, venue)

    def account_for(self, venue: str) -> RemoteAccount:
        """A LocalAccount-shaped handle whose only abilities are the signer's
        schemas (x402, Hyperliquid orders). Never a private key."""
        return self.signer_for(venue).account

    def solana_signer(self, account: int = 0):
        if int(account) != 0 or not self._identity.get("solana"):
            raise WalletSigningUnavailable(
                "WALLET_SIGNER=remote: only the agent's own Solana address (account 0) is known here")
        return RemoteSolanaSigner(str(self._identity["solana"]), venue="account0", family="svm")


def _record_matches(identity: Dict[str, Any], record: Optional[Dict[str, Any]]) -> Optional[str]:
    """None when *identity* reproduces every address in *record*, else why not."""
    if not record:
        return None
    theirs = dict(record.get("evm") or {})
    ours = dict(identity.get("evm") or {})
    for venue, addr in theirs.items():
        if str(ours.get(venue, "")).lower() != str(addr).lower():
            return f"{venue}: signer {ours.get(venue)} vs record {addr}"
    if record.get("solana") and identity.get("solana") and record["solana"] != identity["solana"]:
        return f"solana: signer {identity['solana']} vs record {record['solana']}"
    return None


def build_remote_wallet(cfg, sink, *, on_record=None, client=None) -> RemoteWallet:
    """The agent's wallet in ``remote`` mode. Drops any key material this
    process still holds FIRST — remote means the agent holds none."""
    import dataclasses
    from core.security.custody_env import discard_custody_secrets
    held = discard_custody_secrets()
    if getattr(cfg, "master_seed", None):
        held = sorted(set(held) | {"AGENT_WALLET_MASTER_SEED (config)"})
    # The config object carries the seed too (load_wallet_config read it before
    # the discard): the wallet is built from a copy WITHOUT it.
    cfg = dataclasses.replace(cfg, master_seed=None)
    if held:
        logger.critical("WALLET_SIGNER=remote but this process was given %s — DISCARDED; "
                        "remove wallet.env from the agent unit (066 cut-over)", ", ".join(held))
    if client is None:
        from core.signer.client import SignerClient
        client = SignerClient()
    record = None
    try:
        from core.wallet import public_identity
        record = public_identity.read_public_identity()
    except Exception:
        record = None
    identity = None
    try:
        identity = client.call("identity", timeout=10.0)
    except Exception as exc:
        _set_verified(False, f"signer unreachable at start: {exc}")
        logger.error("WALLET_SIGNER=remote: signer identity unavailable (%s); reads use the "
                     "published identity and every send refuses until restart", exc)
    if identity is not None:
        mismatch = _record_matches(identity, record)
        if mismatch:
            _set_verified(False, f"signer wallet differs from the wallet of record ({mismatch})")
            logger.critical("WALLET_SIGNER=remote: the signer's wallet is NOT the wallet of "
                            "record (%s); every send refuses", mismatch)
        else:
            _set_verified(True, "signer identity matches the wallet of record")
    source = identity if identity is not None else record
    if not source:
        raise WalletSigningUnavailable(
            "WALLET_SIGNER=remote: the signer is unreachable and no public identity is published")
    return RemoteWallet(cfg, source, client, audit_sink=sink, on_record=on_record)
