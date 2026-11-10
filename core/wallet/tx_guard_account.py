"""tx_guard rule 5: the pre-flight reads of a token-bound account call (``TxIntent.via_account``).

Moved out of ``core/wallet/tx_guard.py`` (its size row is frozen); ``tx_guard`` keeps the two
names (``_via_account_preflight``, ``_account_collection_refusal``) as thin wrappers, because
the guard steps and the tests reach them there. Helpers of ``tx_guard`` are reached as
``_tg.<name>`` at call time for the same reason (a test monkeypatches them there).

Two account kinds, chosen by the collection profile (``collection_registry.AccountVersion``):

* Tokenbound **AccountV3** — the registry and the implementation pinned by
  ``erc6551.CODE_SHA256``; the lock (``isLocked``) and the ERC-2771 forwarder
  (``isTrustedForwarder``) are read.
* **PolyrobAccount** (polyrob-desk SPEC §7.1) — the implementation pinned by the profile's
  ``transfer_safe.account_impl_runtime_sha256``; it has no lock, no forwarder and no fallback, so
  those reads would revert and fail closed on every call: they are skipped. Its ``JOURNAL()`` must
  be the profile's ``journal_log`` when the profile pins one.

Every read that fails refuses (fail closed). ⚠️ The signer re-runs this code in its own process:
no new module-level imports.
"""
from typing import Optional, Tuple


def account_collection_refusal(chain: str, account: str, rpc, *,
                               journal_to: Optional[str] = None,
                               implementation: Optional[str] = None
                               ) -> Tuple[Optional[str], object]:
    """C4: the account's own ``token()`` must name a PINNED collection on this chain whose live
    code still hashes to its pin, and the account must be that token's canonical account (one
    of the profile's account versions, pure CREATE2). Returns ``(reason or None, (profile,
    version))``; *version* is the profile's version of *implementation* (None when not given or
    not pinned). The signer re-runs this guard, so a clone of an unpinned or changed collection
    refuses there too. Fail closed."""
    from core.wallet import collection_registry, erc6551
    from core.wallet import tx_guard as _tg
    chain_id, collection, token_id = erc6551.read_token(rpc, account)
    want = collection_registry.chain_id_of(chain)
    if int(chain_id) != int(want):
        return (f"the token-bound account {account} is bound to a token on chain {chain_id}, "
                f"not {chain} ({want})"), (None, None)
    why = _tg._collection_runtime_refusal(chain, collection, rpc)
    if why:
        return f"the token-bound account {account} belongs to {collection} #{token_id}: {why}", \
            (None, None)
    profile = collection_registry.profile_for(want, collection)
    canonical = {erc6551.account_address(want, profile.address, int(token_id), salt=v.salt,
                                         implementation=v.implementation).lower()
                 for v in profile.accounts}
    if str(account).lower() not in canonical:
        return (f"{account} is not the pinned ERC-6551 account of {collection} #{token_id} — "
                f"an account outside the profile's account versions"), (profile, None)
    if journal_to is not None and str(journal_to).lower() != str(profile.journal_log or ""):
        return (f"the journal leg calls {journal_to}, not the JournalLog pinned for {collection} "
                f"({profile.journal_log or 'none'})"), (profile, None)
    version = profile.account_version(implementation) if implementation else None
    return None, (profile, version)


def via_account_preflight(intent, sender: str, rpc, *, inner_to: Optional[str] = None,
                          journal_to: Optional[str] = None) -> Optional[str]:
    """Rule 5: reads BEFORE simulation. Any failed read refuses (fail closed).

    069 v4 (the simple model): the SIGNER must be the NFT's current owner — the only way this
    instance acts through an account. There is no grant path: ``setPermissions`` is an
    account-admin selector the guard refuses, and a permissioned caller is never accepted."""
    from core.wallet import erc6551
    acct = intent.via_account
    try:
        impl = erc6551.read_implementation(rpc, acct)
        why, (profile, version) = account_collection_refusal(
            intent.chain, acct, rpc, journal_to=journal_to, implementation=impl)
        if why:
            return why
        if version is None:
            return (f"the token-bound account clones {impl}, not an account implementation "
                    f"pinned for {profile.address} (AccountV3 {erc6551.ACCOUNT_V3_IMPL}, or the "
                    f"collection's own PolyrobAccount)")
        erc6551.verify(rpc, version)
        polyrob = version.is_polyrob
        if not polyrob and erc6551.read_is_locked(rpc, acct):
            return "the token-bound account is LOCKED by its owner — nothing moves until the lock ends"
        owner = erc6551.read_owner(rpc, acct)
        if str(owner).lower() != str(sender).lower():
            return (f"this wallet ({sender}) does not own the NFT of the token-bound account "
                    f"{acct} (its owner is {owner}) — an agent acts through an account only "
                    f"as the NFT's current owner")
        if not polyrob and inner_to and erc6551.read_is_trusted_forwarder(rpc, acct, inner_to):
            return (f"the token-bound account trusts {inner_to} as an ERC-2771 forwarder — the "
                    f"inner call may not go through it")
        if polyrob and profile.journal_log:
            journal = erc6551.read_journal(rpc, acct)
            if journal != profile.journal_log:
                return (f"the PolyrobAccount {acct} names the JournalLog {journal}, not the one "
                        f"pinned for {profile.address} ({profile.journal_log})")
        state = erc6551.read_state(rpc, acct)
        if state != int(intent.via_account_state):
            return (f"the token-bound account state moved ({intent.via_account_state} → "
                    f"{state}) since the intent was built — re-read and rebuild")
    except Exception as exc:  # noqa: BLE001
        return f"Account pre-flight could not be verified ({exc}); failing closed"
    return None


__all__ = ["account_collection_refusal", "via_account_preflight"]
