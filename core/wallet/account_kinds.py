"""The ONE account-kind table: each ERC-6551 account kind this build verifies, as a row of facts.

A kind is CODE (a reviewed row), never config: a collection profile may only NAME a kind this
build knows (``accounts[i].kind`` in ``core/wallet/collection_registry.py``), and ``/nft trust``
proves it by ERC-165 before it pins one. Every branch that differs by kind — the guard's
pre-flight reads, the code pin, the revert names, the views, the tool text — reads a field
of the row; nothing compares a kind name.

Two rows, keyed by the STANDARD, not by a product:

* ``tokenbound_v3`` — the canonical Tokenbound ``AccountV3`` (one implementation, pinned by
  ``core.wallet.erc6551.CODE_SHA256``). It has a lock and an ERC-2771 forwarder, may hold
  standing approvals, and accepts its owner's signature as its own (ERC-1271).
* ``erc6551p`` — an account of the ERC-6551P draft extension (first deployment: the
  polyrob-desk ``contracts/src/account``). Each collection pins its own implementation by
  runtime code hash; the collection answers ``IERC6551PCollection`` and the implementation
  ``ITransferSafeAccount`` by ERC-165. No lock, no forwarder, no ERC-1271 signer, and no
  standing grant at rest; its custom errors are :data:`ERC6551P_ERRORS`.

A third kind is a new row, a review and a fork test — never a profile edit.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from core.wallet.custom_errors import ErrorTable

#: The custom errors of an ERC-6551P account (polyrob-desk
#: ``contracts/src/account/PolyrobAccount.sol:118-131``), by selector, with their arguments.
ERC6551P_ERRORS: Dict[str, Tuple[str, str]] = {
    "0x30cd7471": ("NotOwner()", "only the NFT's current owner can make the account call"),
    "0xb5dfd9e5": ("Reentered()", "the account is already inside a batch"),
    "0x5363d184": ("SelfCall()", "the account may not call itself"),
    "0x29a270f5": ("OperationNotSupported()", "CALL only: no DELEGATECALL, no CREATE"),
    "0xc17da94a": ("ReadyNotLast()", "after readyToSell only JournalLog.log legs with no value "
                                     "may follow"),
    "0xe4073933": ("Refused(address target, bytes4 selector)",
                   "the account refuses this selector or target at any depth"),
    "0xb04d6d52": ("BadEncoding()", "a grant call or a wrapper is not canonically ABI-encoded"),
    "0x2645cabb": ("TooManyGrants()", "more grant tuples than the account reads"),
    "0x1bc29056": ("ReadGas()", "too little gas left for a grant read"),
    "0x6379a90a": ("ReadFailed(address target)", "a grant read of a target failed"),
    "0xdab3582e": ("GasReserve()", "the account holds gas back after each leg; the gas limit "
                                   "is too low"),
    "0xa210d63b": ("OwnerChanged()", "the NFT's owner changed inside the batch"),
    "0xd88c09e3": ("StandingGrant(uint256 leg, address target, address who)",
                   "an approval would outlive the batch: an approve must be spent and reset to "
                   "zero in the same batch"),
    "0xb79e3f3f": ("OwnershipCycle()", "an NFT may not go into its own account, nor into an "
                                       "account of the same collection"),
}


@dataclass(frozen=True, eq=False)
class AccountKind:
    """One account kind. Identity equality: there is one object per row."""
    #: The standard: ``"tokenbound_v3"`` | ``"erc6551p"`` (the profile's ``accounts[i].kind``).
    name: str
    #: What a person reads: ``"Tokenbound AccountV3"`` | ``"ERC-6551P account"``.
    label: str
    #: The one implementation of the kind, or None (the profile pins its own by code hash).
    builtin_implementation: Optional[str]
    #: sha256 of that implementation's runtime, or None (the profile's ``runtime_sha256``).
    builtin_code_sha256: Optional[str]
    #: ERC-165 id the collection must answer at pin time, or None.
    collection_iface: Optional[str]
    #: ERC-165 id the implementation must answer at pin time, or None.
    account_iface: Optional[str]
    #: The guard reads ``isLocked`` (an account without a lock reverts the read).
    has_lock: bool
    #: The guard reads ``isTrustedForwarder`` for each inner target.
    has_forwarder: bool
    #: The view that names the account's JournalLog (must equal the profile's ``journal_log``).
    journal_getter: Optional[str]
    #: ERC-1271 accepts the NFT owner's signature as the account's.
    signs_as_owner: bool
    #: ``"may_hold"`` | ``"refused"`` (a found row is then a residual R-1 symptom).
    standing_approvals: str
    #: The account's custom errors (decoded only for a call through this kind).
    errors: ErrorTable = field(default_factory=dict)
    #: The collection read of sale readiness (a collection-standard view), or None.
    sale_ready_view: Optional[str] = None
    #: ONE sentence the tool descriptions, the views and the skill share.
    limits_text: str = ""
    #: The view lines of the kind's approval guarantee (None = the kind guarantees none).
    approvals_text: Optional[str] = None
    residuals_text: Optional[str] = None
    found_approval_text: Optional[str] = None
    #: What the account signs.
    signs_text: str = ""

    def __repr__(self) -> str:
        return f"AccountKind({self.name!r})"


TOKENBOUND_V3 = AccountKind(
    name="tokenbound_v3",
    label="Tokenbound AccountV3",
    builtin_implementation="0x41C8f39463A868d3A88af00cd0fe7102F30E44eC",
    builtin_code_sha256="0b890bf41c26142829c2ec32796e03fae54b1ee4c1a36477c63264277c61c160",
    collection_iface=None,
    account_iface=None,
    has_lock=True,
    has_forwarder=True,
    journal_getter=None,
    signs_as_owner=True,
    standing_approvals="may_hold",
    errors={},
    sale_ready_view=None,
    limits_text=("Tokenbound AccountV3: the NFT's owner drives it with CALL only (no "
                 "DELEGATECALL, no admin call); it has a lock and an ERC-2771 forwarder, may "
                 "hold standing approvals, and accepts the owner's signature as its own "
                 "(ERC-1271)."),
    signs_text="as its owner: ERC-1271 accepts the NFT owner's signature as the account's",
)

ERC6551P = AccountKind(
    name="erc6551p",
    label="ERC-6551P account",
    builtin_implementation=None,
    builtin_code_sha256=None,
    collection_iface="0x6b7481e3",   # IERC6551PCollection (XOR of its nine selectors)
    account_iface="0x92ad233c",      # ITransferSafeAccount = inBatch()
    has_lock=False,
    has_forwarder=False,
    journal_getter="JOURNAL()",
    signs_as_owner=False,
    standing_approvals="refused",
    errors=ERC6551P_ERRORS,
    sale_ready_view="saleReadyOf(uint256)",
    limits_text=("ERC-6551P account: the NFT's owner drives it with CALL only (no "
                 "DELEGATECALL, no admin call); it has no lock and no forwarder, signs "
                 "nothing, refuses a standing approval (an ERC-20 spend approves, spends and "
                 "resets in one batch), and its NFT cannot be sold while an ERC-6551 account "
                 "owns it."),
    approvals_text="the ERC-6551P account refuses standing approvals",
    residuals_text=("not covered by the account: grants it cannot decode (R-1), built-in "
                    "grants of a protocol (R-5), configuration rights (R-6)"),
    found_approval_text=("the ERC-6551P account refuses standing approvals, so a row found "
                         "here is a residual R-1 symptom (a grant the account could not "
                         "decode)"),
    signs_text=("nothing: ERC-1271 always fails and no address is a valid signer, so no "
                "off-chain order can be signed for the account"),
)

#: Every kind this build verifies, by name. The order is the order the text lists them.
KINDS: Dict[str, AccountKind] = {k.name: k for k in (TOKENBOUND_V3, ERC6551P)}


def kind_named(name: str) -> Optional[AccountKind]:
    """The kind called *name*, or None (an unknown kind refuses the profile that names it)."""
    return KINDS.get(str(name or ""))


def limits_text() -> str:
    """Every kind's one sentence, in table order — the text the tools and the skill share."""
    return " ".join(k.limits_text for k in KINDS.values())


__all__ = ["AccountKind", "ERC6551P", "ERC6551P_ERRORS", "KINDS", "TOKENBOUND_V3",
           "kind_named", "limits_text"]
