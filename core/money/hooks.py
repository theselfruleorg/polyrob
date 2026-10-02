"""What a money rail registers with the kernel (067 P1b, P5a).

The kernel is mechanism without rail data. Five facts come from the rail that
owns them, through ONE registration each:

* :func:`register_cap_resolver` — a factory ``(env=None, *, user_id=None,
  home_dir=None) -> () -> (max_per_tx_usd, daily_cap_usd)``; the wallet builds
  its :class:`core.money.ledger.SpendLedger` with what it returns;
* :func:`register_spend_ledger` — a zero-arg getter for the process-wide
  ledger (the caps block of ``modules/credits/unified_ledger`` reads it);
* :func:`register_treasury_balance` — an async zero-arg probe for the
  treasury's USD balance (``modules/credits/balances``). No registration =
  ``None`` = "unknown", which every ledger renderer already shows as unknown,
  never ``$0.00``;
* :func:`register_submission_journal` (P5a) — a zero-arg getter for the
  broadcast interlock: an object with ``unresolved() -> list`` and
  ``mark_booked(ref, *, amount_usd=None, venue=None)``. ``SpendLedger.check``
  REFUSES every spend while it is absent or raises (fail closed, the same
  sentence as the old failed import); ``SpendLedger.record`` raises;
* :func:`register_position_book` (P5a) — ``apply_deltas(user_id, deltas)``,
  the open-position book ``SpendLedger.record`` writes after a booked swap.
  Absent = the book write is skipped (fail open, as before).

Registration is last-writer-wins, so a test (or a later pack) can replace a
provider. There is NO default module any more (P5a deleted
``_DEFAULT_MODULES``): the wallet registers every provider when the
``core.wallet`` package is imported (``core/wallet/__init__.py``), and the
pack loader's phase 1 (``core.packs.loader.register_policies``, run at every
process entry) imports that package, so the CLI, the API server, the console
and the gateway all have the providers before the first money call. The
signer imports ``core.wallet.tx_guard`` and so registers the same way. P5b
replaces that import with the wallet pack's own registration.

Tier-0: stdlib only.
"""
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_CAP_RESOLVER = "cap_resolver"
_SPEND_LEDGER = "spend_ledger"
_TREASURY_BALANCE = "treasury_balance"
_SUBMISSION_JOURNAL = "submission_journal"
_POSITION_BOOK = "position_book"

_PROVIDERS: Dict[str, Callable[..., Any]] = {}


def _register(kind: str, fn: Callable[..., Any]) -> None:
    if not callable(fn):
        raise TypeError(f"money hook {kind}: {fn!r} is not callable")
    _PROVIDERS[kind] = fn


def _provider(kind: str) -> Optional[Callable[..., Any]]:
    return _PROVIDERS.get(kind)


def register_cap_resolver(factory: Callable[..., Callable[[], tuple]]) -> None:
    _register(_CAP_RESOLVER, factory)


def register_spend_ledger(getter: Callable[[], Any]) -> None:
    _register(_SPEND_LEDGER, getter)


def register_treasury_balance(probe: Callable[[], Awaitable[Optional[float]]]) -> None:
    _register(_TREASURY_BALANCE, probe)


def register_submission_journal(getter: Callable[[], Any]) -> None:
    _register(_SUBMISSION_JOURNAL, getter)


def register_position_book(apply_deltas: Callable[[str, list], Any]) -> None:
    _register(_POSITION_BOOK, apply_deltas)


def registered() -> tuple:
    """The kinds a rail has registered (for status and tests)."""
    return tuple(sorted(_PROVIDERS))


def cap_resolver(env=None, *, user_id=None, home_dir=None) -> Optional[Callable[[], tuple]]:
    """The registered resolver for these inputs, or None (= frozen caps).

    A provider whose module cannot be imported (a base install without the
    chain dependencies) also reads as None."""
    factory = _provider(_CAP_RESOLVER)
    if factory is None:
        return None
    try:
        return factory(env, user_id=user_id, home_dir=home_dir)
    except ImportError:
        return None


def get_spend_ledger():
    """The process-wide spend ledger, or None when no rail registered one.

    Raises whatever the getter raises (``ImportError`` when the wallet's
    chain dependencies are not installed) — the caller decides how loud that is.
    """
    getter = _provider(_SPEND_LEDGER)
    return getter() if getter is not None else None


def submission_journal():
    """The broadcast interlock, or None when no rail registered one.

    Raises whatever the getter raises; ``SpendLedger`` treats both a raise and
    ``None`` as "journal unavailable" and refuses (fail closed)."""
    getter = _provider(_SUBMISSION_JOURNAL)
    return getter() if getter is not None else None


def position_book() -> Optional[Callable[[str, list], Any]]:
    """``apply_deltas(user_id, deltas)``, or None (the book write is skipped)."""
    return _provider(_POSITION_BOOK)


async def treasury_balance_usd() -> Optional[float]:
    """The treasury's USD balance, or None = unknown. Never raises.

    The caller does the owner check (CR-M07) BEFORE asking: the treasury is
    the operator's fact, not a tenant's.
    """
    try:
        probe = _provider(_TREASURY_BALANCE)
        if probe is None:
            return None
        return await probe()
    except Exception:
        logger.debug("treasury balance hook failed (fail-open -> None)", exc_info=True)
        return None
