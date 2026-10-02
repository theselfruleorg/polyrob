"""Optional API contributions (067 P5a) — the money rail's routes, middleware,
auth method and payment option, behind one seam.

The API server (``api/app.py``) names the SLOTS and their mount positions; a
contribution fills a slot. Today the in-tree money contributions register from
``api/money_contributions.py`` (imported on first use, :data:`_IN_TREE`); 067
P5c moves that file's registrations into the wallet pack, and a server without
it simply has no ``/api/payments``, ``/api/x402/*``, ``/eip8004/*``, no x402
middleware, no x402 auth path and no x402 option in a 402 body.

Every reference is ``"module:attr"``, resolved when the slot is used — an
``ImportError`` there is the "rail not installed" case and is logged, never
raised into the app factory (the behaviour of the old inline ``try`` blocks).

Unlike a pack's own routers (``api/pack_routes.py``, mounted under
``/api/packs/<id>``), a contribution here keeps its PUBLIC path: payers,
wallets and ERC-8004 resolvers already call those URLs.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Router slots in ``api/app.py`` mount order.
ROUTER_SLOTS: Tuple[str, ...] = ("payments", "x402", "eip8004")
#: Middleware slots (installed where ``api/app.py`` names them).
MIDDLEWARE_SLOTS: Tuple[str, ...] = ("payment",)

_IN_TREE: Tuple[str, ...] = ("api.money_contributions",)


@dataclass(frozen=True)
class RouterContribution:
    slot: str
    ref: str
    prefix: str
    tags: Tuple[str, ...]
    source: str
    announce: Optional[str] = None          # "module:attr" -> (logger) -> None
    missing_level: int = logging.WARNING


_ROUTERS: List[RouterContribution] = []
_MIDDLEWARE: Dict[str, Tuple[str, str]] = {}          # slot -> (installer ref, source)
_AUTH: List[Tuple[str, str, str]] = []                # (name, ref, source), in order
_PAYMENT_OPTIONS: List[Tuple[str, str, str]] = []     # (name, ref, source)
_CARD_OPTIONS: List[Tuple[str, str, str]] = []        # (name, ref, source) — a2a card
_loaded = False


def _check_ref(ref: str, what: str) -> str:
    mod, _, attr = str(ref).partition(":")
    if not mod or not attr:
        raise ValueError(f"{what}: {ref!r} is not 'module:attr'")
    return str(ref)


def resolve(ref: str) -> Any:
    mod, _, attr = ref.partition(":")
    obj = importlib.import_module(mod)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def register_router(slot: str, ref: str, *, prefix: str = "", tags=(), source: str,
                    announce: Optional[str] = None,
                    missing_level: int = logging.WARNING) -> None:
    """Contribute a router to ``slot`` (mounted at that slot's position, with
    ``prefix``/``tags`` exactly as given). Idempotent per (slot, ref)."""
    if slot not in ROUTER_SLOTS:
        raise ValueError(f"api router from {source}: {slot!r} is not a slot {ROUTER_SLOTS}")
    ref = _check_ref(ref, f"api router from {source}")
    if any(c.slot == slot and c.ref == ref for c in _ROUTERS):
        return
    _ROUTERS.append(RouterContribution(slot, ref, prefix, tuple(tags), source,
                                       announce, missing_level))


def register_middleware(slot: str, installer: str, *, source: str) -> None:
    """``installer(app, logger)`` adds the middleware (it decides whether it is
    enabled). One installer per slot; last writer wins."""
    if slot not in MIDDLEWARE_SLOTS:
        raise ValueError(f"api middleware from {source}: {slot!r} is not a slot "
                         f"{MIDDLEWARE_SLOTS}")
    _MIDDLEWARE[slot] = (_check_ref(installer, f"api middleware from {source}"), source)


def register_auth_method(name: str, ref: str, *, source: str) -> None:
    """A PERMISSIVE auth path (``api.dependencies.get_user_permissive``):
    ``fn(request) -> user_id | None``, tried in registration order BEFORE the
    core paths (JWT, API key)."""
    ref = _check_ref(ref, f"auth method {name} from {source}")
    if not any(n == name for n, _r, _s in _AUTH):
        _AUTH.append((name, ref, source))


def register_payment_option(name: str, ref: str, *, source: str) -> None:
    """An extra ``payment_options[name]`` block in a 402 body
    (``api.payment_verification.payment_required_response``):
    ``fn(request, cost_credits) -> dict``."""
    ref = _check_ref(ref, f"payment option {name} from {source}")
    if not any(n == name for n, _r, _s in _PAYMENT_OPTIONS):
        _PAYMENT_OPTIONS.append((name, ref, source))


def register_card_payment_option(name: str, ref: str, *, source: str) -> None:
    """The A2A agent card's ``authentication_options[name]`` block (the
    ``a2a.payment`` hook): ``fn() -> dict | None`` (None = omit)."""
    ref = _check_ref(ref, f"card payment option {name} from {source}")
    if not any(n == name for n, _r, _s in _CARD_OPTIONS):
        _CARD_OPTIONS.append((name, ref, source))


def _load_in_tree() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for name in _IN_TREE:
        importlib.import_module(name)


def router_contributions(slot: Optional[str] = None) -> Tuple[RouterContribution, ...]:
    _load_in_tree()
    return tuple(c for c in _ROUTERS if slot is None or c.slot == slot)


def mount_routers(app, slot: str, logger) -> int:
    """Include every router contributed to ``slot``; a contribution whose module
    cannot be imported is logged at its ``missing_level`` and skipped."""
    mounted = 0
    for c in router_contributions(slot):
        try:
            router = resolve(c.ref)
        except ImportError as e:
            logger.log(c.missing_level, f"{c.slot} endpoints not available: {e}")
            continue
        app.include_router(router, prefix=c.prefix, tags=list(c.tags))
        mounted += 1
        if c.announce:
            resolve(c.announce)(logger)
    return mounted


def install_middleware(app, slot: str, logger) -> bool:
    _load_in_tree()
    entry = _MIDDLEWARE.get(slot)
    if entry is None:
        return False
    installer, _source = entry
    resolve(installer)(app, logger)
    return True


def auth_methods() -> List[Tuple[str, Callable[..., Optional[str]]]]:
    _load_in_tree()
    return [(name, resolve(ref)) for name, ref, _s in _AUTH]


def payment_options() -> List[Tuple[str, Callable[..., Dict[str, Any]]]]:
    _load_in_tree()
    return [(name, resolve(ref)) for name, ref, _s in _PAYMENT_OPTIONS]


def card_payment_options() -> List[Tuple[str, Callable[[], Optional[Dict[str, Any]]]]]:
    """Resolved card options; one that cannot import is skipped (the card is
    public and must render without the rail)."""
    _load_in_tree()
    out = []
    for name, ref, _s in _CARD_OPTIONS:
        try:
            out.append((name, resolve(ref)))
        except ImportError:
            continue
    return out


__all__ = ["MIDDLEWARE_SLOTS", "ROUTER_SLOTS", "RouterContribution", "auth_methods",
           "card_payment_options", "install_middleware", "mount_routers",
           "payment_options", "register_auth_method", "register_card_payment_option",
           "register_middleware", "register_payment_option", "register_router",
           "resolve", "router_contributions"]
