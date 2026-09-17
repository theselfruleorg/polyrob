"""046: paid moderation actions in an allowlisted room.

A member replies to another member's post, names a verb, pays, and the effect is
applied by Telegram itself.

Three lines this module exists to hold:

* **The effect catalog is CODE.** An owner prices a row; nobody — owner or agent
  — authors a new effect from a seat. An agent that can define its own
  room-mutating action has granted itself moderation power, which is the class
  044's room deny-list and 031's pause record exist to forbid.
* **The envelope is the authority.** The agent may choose a price inside an
  owner-set band. It may not choose the asset, the recipient, the target
  protections, or the effect.
* **Never sell what we cannot deliver.** The bot's Telegram rights are checked
  BEFORE a mint, not after a settlement — and since phase 2, so is the payment
  rail itself (`rail_check`): a deployment whose settlement watcher never starts
  would otherwise mint offers, take real money and settle nothing, forever.

Pure core: nothing here imports `agents`/`tools`/`surfaces`/`modules` — not even
at call time, because `tests/test_layering_ratchet.py` reads imports statically
and its allowlist may only shrink. The price quoter, the invoice minter, the
member-status probe and the Telegram client all arrive as injected callables or
container services, the way `core/surfaces/group_admin.py` reaches `group_ledger`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

_DUR_RE = re.compile(r"^(\d+)\s*([mhd])$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


@dataclass(frozen=True)
class RoomEffect:
    """One thing a payment can buy.

    ``telegram_right`` is the bot permission the effect NEEDS. It is checked
    against ``getChatMember`` before any mint, so a room where the bot is not an
    admin refuses instead of taking money for something it cannot do.

    ``self_payable`` marks the COUNTER-PAY verbs: the one case where buying an
    action against yourself is legitimate, because it is the target buying their
    own way out. Every other verb refuses a self-target.

    ⚠️ On Telegram this is reachable by a THIRD PARTY, not usually by the target:
    a muted member cannot send `/unmute` (they cannot send anything) and a banned
    member is not in the chat. The rule is still the right one — it is what a
    surface where a silenced member can still issue a command needs, and it is
    what lets a friend pay to lift it — but do not document it as "buy your own
    way out" without saying who can actually type the line.
    """
    verb: str
    telegram_right: str
    max_duration_sec: int
    reversed_by: str = ""
    needs_duration: bool = True
    self_payable: bool = False


#: ⚠️ Every verb here MUST map to a method that exists on the chat client — the
#: adapter is `surfaces/telegram/room_moderator.py` and the mapping is pinned by
#: `tests/test_paid_room_action_ratchet.py`. `slowmode` was removed in phase 2:
#: `Bot.set_chat_slow_mode_delay` does not exist on aiogram (there is no Bot API
#: method behind it), so every slowmode sale would have taken money and written
#: a credit. A verb we cannot perform must not be in the vocabulary a refusal
#: echoes back as "I sell".
EFFECTS: Dict[str, RoomEffect] = {
    e.verb: e for e in (
        RoomEffect("mute", "can_restrict_members", 30 * 86400, "unmute", True),
        RoomEffect("unmute", "can_restrict_members", 30 * 86400, "", False,
                   self_payable=True),
        RoomEffect("ban", "can_restrict_members", 30 * 86400, "unban", True),
        RoomEffect("unban", "can_restrict_members", 30 * 86400, "", False,
                   self_payable=True),
    )
}

#: The grantable vocabulary, so a refusal can echo it back.
VERBS: Tuple[str, ...] = tuple(EFFECTS)


def effect(verb: Optional[str]) -> Optional[RoomEffect]:
    """The catalog row for *verb*, or ``None``. Never a default."""
    if not verb:
        return None
    return EFFECTS.get(str(verb).strip().lower())


def parse_duration(spec: Optional[str]) -> Optional[int]:
    """``30m``/``2h``/``1d`` in seconds, or ``None``.

    The SAME grammar `core/surfaces/group_admin.py::_parse_duration_seconds`
    accepts, so an owner never has to learn a second one.
    """
    if not spec:
        return None
    m = _DUR_RE.match(str(spec).strip().lower())
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0:
        return None
    return n * _UNIT_SECONDS[m.group(2)]


__all__ = ["EFFECTS", "MemberVerbRow", "RoomEffect", "VERBS", "describe_for_model", "effect", "format_duration", "max_duration_sec",
           "member_price_rows", "member_verbs", "parse_duration", "policy_for", "render_member_help", "render_member_prices"]


# ---------------------------------------------------------------------------
# The ONE mint path
# ---------------------------------------------------------------------------

import logging          # noqa: E402
import os               # noqa: E402
import time as _time    # noqa: E402
import uuid             # noqa: E402
from typing import Any, Callable, Set   # noqa: E402

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OfferResult:
    """What every seat renders. ``reason_code`` is for a test or a caller that
    must branch; ``text`` is the one sentence a human reads.

    ``invoice`` carries the minted row so a SEAT can render a payment card from
    it (`modules.x402.artifact` + `modules.pfp.cards` — both above core's
    layer). Core still renders every payable value in the TEXT; the seat only
    turns the same facts into a picture.
    """
    ok: bool
    text: str
    offer_id: str = ""
    reason_code: str = ""
    invoice: Optional[dict] = None


def room_actions_enabled() -> bool:
    from core.env import bool_env
    return bool_env("ROOM_ACTIONS_ENABLED", False)


def _data_dir(container: Any) -> str:
    from core.runtime_paths import container_data_home
    return container_data_home(container)


def _policy(container: Any, surface: str, chat_id: str):
    from core.surfaces.chat_policy import load_for_chat
    return load_for_chat(_data_dir(container), surface, chat_id)


def _store(container: Any):
    from core.surfaces.room_action_store import OfferStore, store_path
    return OfferStore(store_path(_data_dir(container)))


def offer_ttl_sec(policy) -> int:
    """This room's offer deadline, in seconds. The ONE reader.

    ⚠️ Phase 2 collapsed three disagreeing deadlines into this one: the offer
    text promised the room's `chat.paid_offer_ttl`, the heartbeat sweep enforced
    a global ``ROOM_ACTION_OFFER_TTL``, and the INVOICE lived 72 h because the
    minter passed no ``expiry_hours`` at all. The invoice now dies with the
    offer, and the sweep reads the room.
    """
    return (parse_duration(getattr(policy, "paid_offer_ttl", "") or "")
            or parse_duration(os.getenv("ROOM_ACTION_OFFER_TTL", "")) or 1800)


@dataclass(frozen=True)
class UnitPrice:
    """A room's TOKEN-denominated price, resolved against the room's asset.

    ``ok`` — ``amount_raw`` is the figure to charge.
    ``fall_back_to_usd`` — no unit price is set; use the USD price (legacy).
    Neither — a REFUSAL carrying ``reason``.
    """
    ok: bool = False
    amount_raw: Optional[int] = None
    fall_back_to_usd: bool = False
    reason: str = ""


def parse_unit_price(raw_value: Any, asset) -> UnitPrice:
    """Resolve ``"<amount> <asset_id>"`` against *asset*.

    ⚠️ The amount and its asset are ONE value, and this is why. A bare number
    was reinterpreted against whatever ``chat.paid_asset`` happened to be, so
    switching a room priced at 75,000 PNL (~$5) over to USDC would have quoted
    the next payer 75,000 USDC. Nothing warned; the figure was simply rescaled.

    So: a price written for another asset REFUSES and names both. It does NOT
    fall back to the USD price — the owner set a token price deliberately, and
    quietly charging a different figure is the same class of lie. Only an
    ABSENT price means "this room charges in dollars".

    A bare number is ambiguous and refuses too. Every write path binds the
    asset, so a bare value can only arrive by hand-edit.
    """
    from decimal import Decimal, InvalidOperation
    text = str(raw_value or "").strip()
    if not text:
        return UnitPrice(fall_back_to_usd=True)
    parts = text.split()
    if len(parts) != 2:
        return UnitPrice(reason=(
            f"{text!r} does not name an asset — a unit price is written "
            f"'<amount> <asset_id>', e.g. '75000 {getattr(asset, 'asset_id', 'pnl')}'"))
    amount_text, asset_id = parts[0], parts[1].strip().lower()
    want = str(getattr(asset, "asset_id", "") or "").strip().lower()
    if asset_id != want:
        return UnitPrice(reason=(
            f"this price is written in {asset_id!r} but the room is now "
            f"charging in {want!r} — re-price it, or switch the room back"))
    try:
        amount = Decimal(amount_text)
    except (InvalidOperation, ValueError):
        return UnitPrice(reason=f"{amount_text!r} is not a number")
    if amount <= 0:
        return UnitPrice(reason=f"{amount_text} is not a price anyone can pay")
    decimals = int(getattr(asset, "decimals", 0) or 0)
    return UnitPrice(ok=True,
                     amount_raw=int((amount * (10 ** decimals)).to_integral_value()))


def unit_price_for(policy, verb: str, asset) -> UnitPrice:
    """This room's unit price for *verb*, bound to *asset*.

    ⚠️ Read EXPLICITLY, never via a constructed attribute name: a dynamic
    ``getattr(policy, f"paid_{verb}_units")`` is invisible to the dead-key
    ratchet, which is how a ``chat.*`` key ships with nothing reading it.
    """
    by_verb = {
        "mute": policy.paid_mute_units,
        "ban": policy.paid_ban_units,
        "unmute": policy.paid_unmute_units,
        "unban": policy.paid_unban_units,
    }
    return parse_unit_price(by_verb.get(verb, ""), asset)


def _price_band(policy, verb: str):
    """``(default_usd, min_usd, max_usd)`` for *verb* in this room.

    A verb with no configured price has NO band at all, and the caller refuses —
    a room never sells something for nothing by omission.

    ⚠️ The field reads are LITERAL, not ``getattr(policy, f"paid_{verb}_usd")``.
    A dynamic read is invisible to `tests/unit/core/test_prefs_no_dead_keys_ratchet.py`,
    which is the check that a `chat.*` key an owner can set is a key something
    actually reads — and a typo in an f-string would silently price at zero.
    Adding a priced verb means adding its row here, on purpose. This is also the
    ONE price reader: `room_action_admin` calls it rather than keeping a second,
    dynamic copy that returned 0.0 for a verb whose field does not exist.
    """
    if verb == "mute":
        return (float(policy.paid_mute_usd or 0.0),
                float(policy.paid_mute_min_usd or policy.paid_mute_usd or 0.0),
                float(policy.paid_mute_max_usd or policy.paid_mute_usd or 0.0))
    if verb == "ban":
        return (float(policy.paid_ban_usd or 0.0),
                float(policy.paid_ban_min_usd or policy.paid_ban_usd or 0.0),
                float(policy.paid_ban_max_usd or policy.paid_ban_usd or 0.0))
    if verb == "unmute":
        # Counter-pay: one price, no band. The TARGET is buying their own way
        # out, so there is nothing for an agent to judge.
        p = float(policy.paid_unmute_usd or 0.0)
        return p, p, p
    if verb == "unban":
        p = float(policy.paid_unban_usd or 0.0)
        return p, p, p
    return 0.0, 0.0, 0.0


def _max_duration_spec(policy, verb: str) -> str:
    """This room's ceiling for *verb*, as the owner wrote it. Literal reads, for
    the same reason as :func:`_price_band`."""
    if verb == "mute":
        return policy.paid_mute_max_duration or ""
    if verb == "ban":
        return policy.paid_ban_max_duration or ""
    return ""


def max_duration_sec(policy, eff: RoomEffect) -> int:
    """The longest *eff* this room actually sells, in seconds.

    The ONE cap reader, for the same reason :func:`_price_band` is the ONE price
    reader: the member-facing help quotes this number, so a second copy would
    advertise a duration the offer path then refuses — a member paying for a
    ceiling nobody honours is the same class of defect as a price that is not
    the price.
    """
    room = parse_duration(_max_duration_spec(policy, eff.verb))
    return min(room or eff.max_duration_sec, eff.max_duration_sec)


def format_duration(seconds: int) -> str:
    """Seconds rendered back into the grammar :func:`parse_duration` accepts.

    So a cap we print is a cap a member can type back verbatim.
    """
    seconds = int(seconds or 0)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{max(1, seconds // 60)}m"


def member_verbs(policy) -> Tuple[str, ...]:
    """The CATALOG verbs this room granted to plain members, in catalog order.

    ``chat.member_verbs`` is owner-written free text and also carries non-effect
    verbs (``help``), so this intersects it with the code-owned catalog rather
    than trusting what was typed.
    """
    granted = {str(v).strip().lower()
               for v in (getattr(policy, "member_verbs", ()) or ())}
    return tuple(v for v in VERBS if v in granted)


@dataclass(frozen=True)
class MemberVerbRow:
    """One line of what a MEMBER of this room may buy.

    ``sellable`` false carries ``why`` — a verb an owner granted but never
    priced is named with its reason, never omitted. Silence would read as "that
    verb does not exist here" while every attempt to use it is refused.
    """
    verb: str
    price_usd: float
    max_duration: str
    sellable: bool
    why: str = ""
    #: 046b — the TOKEN amount a payer actually sends, already formatted with
    #: its symbol ("1,500 PNL"), or "" when this room prices in USD.
    #: ⚠️ When set it is what the member is QUOTED: showing "$0.10" while the
    #: payer sends 1,500 PNL would be wrong in the one place he reads before
    #: paying.
    token_price: str = ""


def member_price_rows(container: Any, *, surface: str, chat_id: str,
                      policy: Any = None) -> Tuple[MemberVerbRow, ...]:
    """What a member of ``surface:chat_id`` may buy, and for how much HERE.

    Prices come from :func:`_price_band` and caps from :func:`max_duration_sec`
    — the ONE readers — so a member is never quoted a figure the offer path
    would refuse.
    """
    if policy is None:
        policy = _policy(container, surface, str(chat_id))
    instance_off = not room_actions_enabled()
    # The room's asset, for a TOKEN-denominated price. Resolved the same way
    # `offer()` resolves it, so the member is quoted the figure he will pay.
    # Unresolvable => no token price is shown; the USD figure still is.
    from decimal import Decimal
    from core.payments.assets import DEFAULT_ASSET_ID, resolve as resolve_asset
    try:
        asset = resolve_asset(
            (policy.paid_asset or os.getenv("PAYMENT_DEFAULT_ASSET", "")
             or DEFAULT_ASSET_ID), data_home=_data_dir(container))
    except Exception as e:
        logger.debug("room prices: asset unresolved (%s)", e)
        asset = None
    rows = []
    for verb in member_verbs(policy):
        eff = EFFECTS[verb]
        price = _price_band(policy, verb)[0]
        token_price = ""
        unit_why = ""
        if asset is not None:
            unit = unit_price_for(policy, verb, asset)
            if unit.ok:
                whole = Decimal(unit.amount_raw) / (Decimal(10) ** int(asset.decimals))
                token_price = (f"{whole.normalize():,f} "
                               f"{asset.symbol or asset.asset_id}")
            elif not unit.fall_back_to_usd:
                # A member must never be quoted a figure the offer would refuse.
                unit_why = unit.reason
        # The owner's OWN words when his ceiling is the binding one ("24h", not
        # a helpfully-rewritten "1d"); the catalog's, formatted, when the
        # catalog is what actually caps it.
        room_spec = _max_duration_spec(policy, verb)
        room_sec = parse_duration(room_spec)
        cap_sec = max_duration_sec(policy, eff)
        cap = ""
        if eff.needs_duration:
            cap = (room_spec if room_sec and room_sec == cap_sec
                   else format_duration(cap_sec))
        if instance_off:
            why = "paid actions are off on this instance"
        elif not policy.paid_enabled:
            why = "paid actions are off in this room"
        elif unit_why:
            why = unit_why
        elif price <= 0 and token_price:
            # ⚠️ A token price with no declared USD would book the ledger at
            # $0.00. Name the remedy rather than reporting the verb unpriced.
            why = (f"{token_price} is set but no USD value is — the owner adds "
                   f"one with /paid price {verb} <usd>")
        elif price <= 0:
            why = "no price is set here"
        else:
            why = ""
        rows.append(MemberVerbRow(verb, price, cap, not why, why, token_price))
    return tuple(rows)


def _usage(row: "MemberVerbRow") -> str:
    """How a member types this verb. It is a REPLY, always — `/mute` with no
    reply is the unrelated pre-046 room verb that silences the AGENT, so a
    member who types it bare buys nothing and is told nothing."""
    arg = " 30m" if row.max_duration else ""
    return f"reply to their message with `/{row.verb}{arg}`"


def render_member_prices(container: Any, *, surface: str, chat_id: str,
                         policy: Any = None) -> str:
    """The price list a MEMBER sees, in the room. Never a confident silence."""
    rows = member_price_rows(container, surface=surface, chat_id=chat_id,
                             policy=policy)
    if not rows:
        return ("Nothing is for sale in this room — no paid action is granted "
                "to members here.")
    lines = ["Paid actions in this room:"]
    for row in rows:
        if not row.sellable:
            lines.append(f"• /{row.verb} — not available ({row.why})")
            continue
        cap = f", up to {row.max_duration}" if row.max_duration else ""
        shown = row.token_price or f"${row.price_usd:.2f}"
        lines.append(f"• /{row.verb} — {shown} — {_usage(row)}{cap}")
    if any(r.sellable for r in rows):
        lines.append("I quote the exact amount and the address to pay it to. "
                     "The action applies on its own once the transfer confirms. "
                     "If it fails you get a credit for a later one, not a refund.")
    return "\n".join(lines)


def render_member_help(container: Any, *, surface: str, chat_id: str,
                       agent_name: str = "") -> str:
    """What a MEMBER of this room can type. Rendered FOR the room.

    ⚠️ A member's `/help` used to answer with the OWNER's whole verb catalog —
    every admin, money and control verb — delivered to the OWNER's DM. The
    member who asked saw nothing at all, and the owner got a help text he did
    not ask for. This is the room's own answer, in the room.
    """
    who = (agent_name or "").strip()
    head = f"{who} here — what you can use in this room:" if who else \
        "What you can use in this room:"
    lines = [head]
    policy = policy_for(container, surface, chat_id)
    rows = member_price_rows(container, surface=surface, chat_id=chat_id,
                             policy=policy)
    for row in rows:
        if not row.sellable:
            lines.append(f"• /{row.verb} — not available ({row.why})")
            continue
        cap = f", up to {row.max_duration}" if row.max_duration else ""
        lines.append(f"• /{row.verb} — ${row.price_usd:.2f}, "
                     f"{_usage(row)}{cap}")
    # Only what this room ACTUALLY granted: advertising `/paid` in a room that
    # did not grant it sends every member into a refusal.
    granted = {str(v).strip().lower()
               for v in (getattr(policy, "member_verbs", ()) or ())}
    if "paid" in granted:
        lines.append("• /paid — the price list for this room")
    lines.append("• /help — this message")
    if any(r.sellable for r in rows):
        lines.append("Durations look like 30m, 2h or 1d. I quote the amount "
                     "and the address; the action applies once the transfer "
                     "confirms.")
    lines.append("Otherwise just talk to me — everything written here is public.")
    return "\n".join(lines)


def policy_for(container: Any, surface: str, chat_id: str):
    """This room's overlay. Public because a SEAT that renders two views of the
    same room must not load it twice and risk two answers."""
    return _policy(container, surface, str(chat_id))


def describe_for_model(container: Any, *, surface: str, chat_id: str,
                       policy: Any = None) -> str:
    """One paragraph for the `<surface>` block: that paid moderation verbs exist
    in THIS room, what they cost, and — the load-bearing half — that they are
    COMMANDS A MEMBER TYPES and not something the agent performs.

    ⚠️ A DESCRIPTION, never a capability. The room session is
    ``SessionProfile.PUBLIC`` with the read-only room toolset and
    ``x402_invoice_x402_request`` in ``ROOM_DENIED_ACTIONS``; nothing here
    widens either, and `tests/test_paid_room_action_ratchet.py` pins that. The
    reason to say it at all is the opposite failure: asked "can I get him
    muted?", a model that has never heard of the rail either denies a shipped
    capability or invents a way to perform it.

    Empty string when this room sells nothing — a room with no paid actions
    keeps a byte-identical prompt.
    """
    rows = [r for r in member_price_rows(container, surface=surface,
                                         chat_id=str(chat_id), policy=policy)
            if r.sellable]
    if not rows:
        return ""
    priced = "; ".join(
        f"{r.verb} ${r.price_usd:.2f}" + (f" (up to {r.max_duration})"
                                          if r.max_duration else "")
        for r in rows)
    return (
        "Members of this room can BUY moderation actions by typing a command: "
        f"{priced}. They do it by replying to the person's message with e.g. "
        "`/mute 30m`; I quote the price and the payment rail applies the effect "
        "on its own. You do NOT perform, price, sell, promise or arrange any of "
        "this — you have no tool for it and no say in it. If someone asks, name "
        "the command and the price above and stop there; never offer to do it "
        "yourself and never quote a different price.")



async def _maybe_await(value):
    """Await *value* if it is awaitable, else return it.

    ⚠️ The injected seams (`rights_fn`/`target_status_fn`/`mint_fn`) are SYNC in
    every test and ASYNC in production: a Telegram read must run on the bot's
    own event loop, and bridging it to another one is the 2026-09-15 outage
    ("got Future attached to a different loop"). Accepting both keeps the test
    doubles honest without pretending the real seam is synchronous.
    """
    import inspect
    if inspect.isawaitable(value):
        return await value
    return value


async def offer(container: Any, *, surface: str, chat_id: str, verb: str,
          target_user_id: str, requester_id: str, target_name: str = "",
          duration: str = "", price_usd: Optional[float] = None,
          rights_fn: Optional[Callable[[str, str], Set[str]]] = None,
          mint_fn: Optional[Callable] = None, quoter: Any = None,
          rail_check: Optional[Callable] = None,
          target_status_fn: Optional[Callable[[str, str, str], str]] = None,
          now: Optional[float] = None) -> OfferResult:
    """Mint ONE paid-action offer, or refuse with a NAMED reason.

    ⚠️ There is no ``recipient`` parameter, and there never may be. The payable
    address is always the configured treasury — a room line must have no path by
    which it can name where money goes (046 §6 T2/T3). The same is true of the
    asset: it is the ROOM's, not the request's.

    Checks run in this order, cheap and structural before anything touches a
    network:

    1. the feature flag, the room allowlist, and ``chat.paid_enabled``
    2. the verb is in the catalog
    3. the duration parses and fits both the room cap and the catalog cap
    4. the 031 pause record allows ``room_action_offer``
    5. the target is not the owner, not a chat admin, not the requester
    6. the daily caps hold for this payer and this target
    7. ⚠️ the bot HOLDS the Telegram right this effect needs — BEFORE the mint
    8. an unredeemed CREDIT this payer holds is spent instead of minting
    9. the price band exists for this verb
    10. ⚠️ the payment rail can actually settle this asset — BEFORE the mint
    11. the quote sizes the price into raw units, or refuses
    12. the invoice is minted and the offer row written

    ⚠️ 7 before 8 and 10 before 12 are both load-bearing: a credit must never be
    consumed into an action we cannot perform, and an offer must never be minted
    onto a rail that cannot settle it.

    ``mint_fn``/``rights_fn``/``quoter``/``rail_check``/``target_status_fn`` are
    injected rather than imported: `core` may not import `modules` or `surfaces`.
    """
    now = now or _time.time()
    label = f"{surface}:{chat_id}"

    # 1 --------------------------------------------------------------------
    if not room_actions_enabled():
        return OfferResult(False, "Paid actions are not enabled on this "
                                  "instance.", reason_code="disabled")
    from core.surfaces.group_allowlist import GroupAllowlist
    allow = GroupAllowlist(os.path.join(_data_dir(container),
                                        "group_allowlist.db"))
    policy = _policy(container, surface, str(chat_id))
    if not allow.is_allowed(surface, str(chat_id)) or not policy.paid_enabled:
        return OfferResult(False, f"Paid actions are not enabled in {label}.",
                           reason_code="disabled")

    # 2 --------------------------------------------------------------------
    eff = effect(verb)
    if eff is None:
        return OfferResult(False, f"I do not sell {verb!r}. I sell: "
                                  f"{', '.join(VERBS)}.",
                           reason_code="unknown_verb")
    verb = eff.verb

    # 3 --------------------------------------------------------------------
    duration_sec = 0
    if eff.needs_duration:
        duration_sec = parse_duration(duration) or 0
        room_spec = _max_duration_spec(policy, verb)
        cap = max_duration_sec(policy, eff)
        if duration_sec <= 0:
            return OfferResult(False, f"{duration!r} is not a duration "
                                      f"(use e.g. 30m, 2h, 1d).",
                               reason_code="bad_duration")
        if duration_sec > cap:
            return OfferResult(
                False, f"The longest {verb} this room sells is "
                       f"{room_spec or '30d'}.", reason_code="bad_duration")

    # 4 --------------------------------------------------------------------
    # Imported as a BARE name and called as one: the autonomy ratchet's
    # caller scan matches `allows(` with no attribute access before it, so a
    # dotted `autonomy_control.allows(...)` would read as ZERO callers and the
    # kind would look dormant.
    from core.autonomy_control import allows
    decision = allows("room_action_offer", _data_dir(container))
    if not decision.allowed:
        return OfferResult(False, f"Paid actions are paused — {decision.reason}.",
                           reason_code="paused")

    # 5 --------------------------------------------------------------------
    protected, why = await _target_protection(container, surface, str(chat_id),
                                        str(target_user_id), str(requester_id),
                                        eff=eff,
                                        target_status_fn=target_status_fn)
    if protected:
        return OfferResult(False, why[0], reason_code=why[1])

    store = _store(container)

    # 6 --------------------------------------------------------------------
    # ⚠️ The caps run BEFORE a credit is redeemed, on purpose. The cap bounds
    # how much moderation one member can aim at a room in a day; it is not a
    # payment budget, so a credit does not buy past it. The credit is not lost —
    # it is untouched and spendable tomorrow.
    since = now - 86400
    if policy.paid_max_per_payer_day and store.count_since(
            surface, str(chat_id), requester=str(requester_id),
            since_ts=since) >= policy.paid_max_per_payer_day:
        return OfferResult(
            False, f"You have used your {policy.paid_max_per_payer_day} paid "
                   f"actions in this room today.", reason_code="payer_cap")
    if policy.paid_max_per_target_day and store.count_since(
            surface, str(chat_id), target=str(target_user_id),
            since_ts=since) >= policy.paid_max_per_target_day:
        return OfferResult(
            False, "That person has already been targeted the maximum number "
                   "of times here today.", reason_code="target_cap")

    # 7 -- ⚠️ before the mint, always -------------------------------------
    if rights_fn is not None:
        try:
            held = set(await _maybe_await(
                rights_fn(surface, str(chat_id))) or ())
        except Exception as e:
            logger.warning("room action: rights probe failed (%s) — refusing", e)
            held = set()
        if eff.telegram_right not in held:
            return OfferResult(
                False, f"I cannot {verb} anyone here: I need the "
                       f"{eff.telegram_right} permission in this chat. Ask an "
                       f"admin to grant it, then try again.",
                reason_code="missing_right")

    # 8 -- an owed credit is spent before any new money is asked for --------
    redeemed = _redeem_credit(container, store, surface=surface,
                              chat_id=str(chat_id), verb=verb,
                              requester_id=str(requester_id),
                              target_user_id=str(target_user_id),
                              target_name=target_name or "",
                              duration_sec=duration_sec, now=now)
    if redeemed is not None:
        return redeemed

    # 9 --------------------------------------------------------------------
    default_usd, low, high = _price_band(policy, verb)
    if default_usd <= 0 and high <= 0:
        return OfferResult(
            False, f"{verb} has no price in {label} — the owner sets one with "
                   f"/paid price {verb} <usd>.", reason_code="disabled")
    chosen = float(price_usd) if price_usd is not None else default_usd
    clamped = min(max(chosen, low), high) if high else max(chosen, low)
    clamp_note = ""
    if price_usd is not None and abs(clamped - chosen) > 1e-9:
        clamp_note = (f" (asked ${chosen:.2f}; this room's band is "
                      f"${low:.2f}-${high:.2f})")

    # 10 -------------------------------------------------------------------
    from core.payments.assets import DEFAULT_ASSET_ID, resolve as resolve_asset
    from core.payments.quote import QuoteRefused, size_amount_raw
    asset_id = (policy.paid_asset or os.getenv("PAYMENT_DEFAULT_ASSET", "")
                or DEFAULT_ASSET_ID)
    asset = resolve_asset(asset_id, data_home=_data_dir(container))
    if asset is None:
        return OfferResult(
            False, f"{label} is priced in {asset_id!r}, which is not a "
                   f"configured asset.", reason_code="unpriceable")
    # ⚠️ Before the mint. `offer()` used to check only its own flag and the
    # room, so an instance with X402_INVOICE_ENABLED off — whose settlement
    # watcher therefore never starts — would mint offers, take real money and
    # settle nothing. The check lives above this module's layer, so it arrives
    # injected; absent, behaviour is unchanged (the test path).
    if rail_check is not None:
        try:
            reason = rail_check(asset)
        except Exception as e:
            logger.warning("room action: rail check raised (%s) — refusing", e)
            reason = f"the payment rail could not be checked ({e})"
        if reason:
            return OfferResult(
                False, f"I cannot take payment in this room right now: {reason}",
                reason_code="rail_unavailable")

    if quoter is None:
        try:
            quoter = container.get_service("payment_quoter")
        except Exception:
            quoter = None
    quote = None
    if quoter is not None:
        try:
            quote = quoter.quote(asset)
        except Exception as e:
            logger.warning("room action: quoter raised (%s) — no quote", e)
    # 11 -------------------------------------------------------------------
    # ⚠️ A TOKEN price decides the amount and needs no quote at all. The USD
    # figure above stays the owner's DECLARED value — the ledger, the invoice
    # cap and the owner's reporting all read it, and booking $0.00 here would
    # be the confident-zero this codebase keeps having to fix.
    unit = unit_price_for(policy, verb, asset)
    if unit.fall_back_to_usd:
        try:
            amount_raw = size_amount_raw(clamped, asset, quote, now=now)
        except QuoteRefused as e:
            return OfferResult(False, f"I cannot price this right now: {e}",
                               reason_code="unpriceable")
    elif not unit.ok:
        # ⚠️ A price written for a DIFFERENT asset refuses; it never falls back
        # to the USD figure. The owner set a token price on purpose, and
        # charging a number nobody chose is the failure this whole rail keeps
        # having to fix.
        return OfferResult(
            False, f"I cannot price this right now: {unit.reason}",
            reason_code="price_asset_mismatch")
    else:
        amount_raw = unit.amount_raw
        # The asset's own token floor is the REAL anti-dust control (a rising
        # price must not make an action free), so it still applies — pricing in
        # tokens skips the oracle, never the floor.
        floor_raw = int(getattr(asset, "min_amount_raw", 0) or 0)
        if floor_raw and amount_raw < floor_raw:
            return OfferResult(
                False,
                f"I cannot price this right now: {amount_raw} raw units of "
                f"{asset.symbol or asset.asset_id} is below this asset's floor "
                f"of {floor_raw}",
                reason_code="unpriceable")

    # 12 -------------------------------------------------------------------
    ttl_sec = offer_ttl_sec(policy)
    offer_id = f"off_{uuid.uuid4().hex[:12]}"
    if mint_fn is None:
        try:
            mint_fn = container.get_service("payment_minter")
        except Exception:
            mint_fn = None
    if mint_fn is None:
        return OfferResult(False, "No payment rail is available right now.",
                           reason_code="mint_failed")
    try:
        invoice = await _maybe_await(mint_fn(
            offer_id=offer_id, surface=surface, chat_id=str(chat_id), verb=verb,
            target_user_id=str(target_user_id), asset=asset,
            amount_raw=amount_raw, price_usd=clamped, ttl_sec=ttl_sec,
            # ⚠️ PUBLIC. `GET /api/x402/requests/{id}` serves this verbatim as
            # the challenge description, so it names the ACTION and the room and
            # never the target: "mute Alice in telegram:-100…" published the
            # name of a person someone was buying an action against to anyone
            # holding the invoice id. The full detail rides in metadata, which
            # only the owner seats read.
            purpose=f"a paid {verb} in {label}"))
    except Exception as e:
        logger.warning("room action: mint failed (%s)", e)
        return OfferResult(False, f"I could not create the payment request: {e}",
                           reason_code="mint_failed")

    # ⚠️ The row's amount is the one the INVOICE carries, not the one we asked
    # for. `create_payment_request` nudges a pinned raw amount when another
    # PENDING row at this treasury already holds it, so two same-price offers in
    # one room can never share a payable amount — the settlement match is an
    # exact raw compare, oldest-first, and a collision applied the wrong offer
    # against the wrong person.
    minted_raw = str(invoice.get("amount_raw") or amount_raw)
    from core.surfaces.room_action_store import Offer
    store.create(Offer(
        offer_id=offer_id, surface=surface, chat_id=str(chat_id), verb=verb,
        target_user_id=str(target_user_id), target_name=target_name or "",
        duration_sec=duration_sec, price_usd=clamped, asset_id=asset.asset_id,
        amount_raw=minted_raw, requester_id=str(requester_id),
        invoice_id=str(invoice.get("request_id") or ""), status="pending",
        reason="", created_at=now))
    _event(container, "room_action_offered", offer_id=offer_id, verb=verb,
           chat_id=str(chat_id), asset_id=asset.asset_id, price_usd=clamped)
    return OfferResult(True, render_offer(
        verb=verb, target_name=target_name or str(target_user_id),
        duration=duration if eff.needs_duration else "", price_usd=clamped,
        asset=asset, amount_raw=int(minted_raw), invoice=invoice,
        ttl=policy.paid_offer_ttl, note=clamp_note, offer_id=offer_id),
        offer_id=offer_id, invoice=invoice)


def _redeem_credit(container: Any, store, *, surface: str, chat_id: str,
                   verb: str, requester_id: str, target_user_id: str,
                   target_name: str, duration_sec: int,
                   now: float) -> Optional[OfferResult]:
    """Spend an unredeemed credit this payer holds, or return ``None``.

    ⚠️ `OfferStore.redeemable_credit` had ZERO callers. The failure path told a
    payer "you hold credit cr_… , good for one {verb} in this room at no further
    charge" and nothing in the tree could honour it — while, because the apply
    seam was unwired, that was the outcome of every paid action.

    The consume is a CONDITIONAL update: two turns racing for the same credit,
    and only one wins. The loser falls through to the ordinary paid path rather
    than being refused — they still want the action.

    Returns an `OfferResult` with ``reason_code="credit_redeemed"``; the SEAT
    then applies it (this module stays sync, `apply` is async).
    """
    try:
        credit = store.redeemable_credit(surface, chat_id, requester_id, verb)
    except Exception as e:
        logger.warning("room action: credit lookup failed (%s) — charging", e)
        return None
    if credit is None:
        return None
    new_id = f"off_{uuid.uuid4().hex[:12]}"
    try:
        if not store.consume_credit(credit.offer_id, new_id):
            return None
    except Exception as e:
        logger.warning("room action: credit consume failed (%s) — charging", e)
        return None
    from core.surfaces.room_action_store import Offer
    store.create(Offer(
        offer_id=new_id, surface=surface, chat_id=chat_id, verb=verb,
        target_user_id=target_user_id, target_name=target_name,
        duration_sec=duration_sec, price_usd=0.0, asset_id=credit.asset_id,
        amount_raw="0", requester_id=requester_id, invoice_id="",
        status="paid", reason=f"credit {credit.credit_id}", created_at=now,
        settled_at=now))
    _event(container, "room_action_credit_redeemed", offer_id=new_id,
           credit_of=credit.offer_id, verb=verb, chat_id=chat_id)
    return OfferResult(
        True,
        f"Using the credit you hold from {credit.offer_id} — no charge.",
        offer_id=new_id, reason_code="credit_redeemed")


async def _target_protection(container: Any, surface: str, chat_id: str,
                       target_user_id: str, requester_id: str, *,
                       eff: Optional[RoomEffect] = None,
                       target_status_fn: Optional[Callable] = None):
    """``(protected, (text, reason_code))``.

    ⚠️ This used to call ``core.instance.is_owner(raw_telegram_id)``, which with
    no ``owner_principal=`` and ``local=False`` is a PERMANENT ``False`` — so the
    owner was never protected at all, and the only thing standing between a
    member and a paid mute of the OWNER was a `group_roles` row the owner
    normally does not have (routing resolves the owner by PRINCIPAL, never from
    that table). The owner is now resolved by their ADDRESS on this surface, the
    way routing does it.

    ⚠️ Fail-CLOSED three ways: an unresolvable owner REFUSES the sale (a sale we
    cannot bound is not a sale), an unreadable live member status reads as
    protected, and an unreadable roles store reads as `member` (its own
    least-privilege default) but never lifts either of the first two.
    """
    if str(target_user_id) == str(requester_id):
        # The counter-pay verbs are the one legitimate self-target: the target
        # is buying their own way out.
        if eff is None or not eff.self_payable:
            return True, ("You cannot buy this against yourself.", "self_target")

    from core.surfaces.owner_target import (CHAT_ADMIN_STATUSES, OwnerUnknown,
                                            is_owner_address)
    try:
        if is_owner_address(surface, str(target_user_id)):
            return True, ("That person cannot be targeted here.",
                          "protected_target")
    except OwnerUnknown as e:
        logger.warning("room action: owner unresolvable on %s (%s) — refusing",
                       surface, e)
        return True, (f"I cannot tell who the owner is in {surface}:{chat_id}, "
                      f"so I will not sell an action against anyone here.",
                      "owner_unknown")
    except Exception as e:
        logger.warning("room action: owner check failed (%s) — treating the "
                       "target as PROTECTED", e)
        return True, ("That person cannot be targeted here.",
                      "protected_target")

    # A LIVE chat administrator, whether or not we ever wrote them a row.
    if target_status_fn is not None:
        try:
            status = str(await _maybe_await(
                target_status_fn(surface, chat_id, str(target_user_id))) or "")
        except Exception as e:
            logger.warning("room action: member-status probe failed (%s) — "
                           "treating the target as PROTECTED", e)
            return True, ("That person cannot be targeted here.",
                          "protected_target")
        if status in CHAT_ADMIN_STATUSES:
            return True, ("That person cannot be targeted here.",
                          "protected_target")

    from core.surfaces.group_roles import GroupRoles
    role = GroupRoles(os.path.join(_data_dir(container), "surfaces.db")).role(
        surface, chat_id, str(target_user_id), is_owner=False)
    if role in ("owner", "admin"):
        return True, ("That person cannot be targeted here.", "protected_target")
    return False, ("", "")


def render_offer(*, verb, target_name, duration, price_usd, asset, amount_raw,
                 invoice, ttl, note="", offer_id="") -> str:
    """The ONE offer text.

    ⚠️ Rendered by CORE, never by the model. Every payable value in it comes
    from the invoice row and the asset row, so a room line has no path by which
    it can author a payment instruction.
    """
    human = _token_amount(amount_raw, asset.decimals)
    dur = f" for {duration}" if duration else ""
    return (
        f"💰 {verb.title()} {target_name}{dur} — {human} {asset.symbol} "
        f"(${price_usd:.2f}){note}\n"
        f"Send exactly that amount to {invoice.get('recipient')} on "
        f"{asset.chain}. It applies automatically once the transfer confirms.\n"
        f"Offer {offer_id or invoice.get('request_id')} "
        f"(invoice {invoice.get('request_id')}) expires in {ttl}.")


def _token_amount(raw: int, decimals: int) -> str:
    """Full precision, never a fixed two decimals — that format renders a
    sub-cent token amount as nothing at all."""
    from decimal import Decimal
    value = Decimal(int(raw)) / (Decimal(10) ** int(decimals))
    return format(value.normalize(), "f")


def _event(container: Any, kind: str, **attrs) -> None:
    """Fail-open telemetry. A telemetry fault must never cost an offer."""
    try:
        from core.event_log import event_log_enabled, get_event_log
        if event_log_enabled():
            get_event_log().record(kind, user_id="", session_id="",
                                   source="room_actions", attrs=attrs)
    except Exception as e:
        logger.debug("room action telemetry failed (%s)", e)


# ---------------------------------------------------------------------------
# Settlement actuates the effect
# ---------------------------------------------------------------------------

def _status_probe(container: Any):
    """A ``(surface, chat_id, user_id) -> status`` probe from the registered
    moderator, or ``None``. Used so the settlement-time re-check sees a live
    chat admin, not only a roles row."""
    try:
        mod = container.get_service("room_moderator") if container else None
    except Exception:
        return None
    # ⚠️ Async FIRST. `_target_protection` awaits through `_maybe_await`, and
    # the sync twin bridges to another event loop — which is what made every
    # target read as PROTECTED on 2026-09-15.
    probe = (getattr(mod, "member_status_async", None)
             or getattr(mod, "member_status_sync", None))
    return probe if callable(probe) else None


async def apply(container: Any, offer_id: str, *, perform_fn: Any = None,
                now: Optional[float] = None) -> OfferResult:
    """Actuate a PAID offer. Idempotent on ``offer_id``.

    ⚠️ NOT pause-gated. We already hold the payer's money, and stranding the
    effect behind a pause is a silent default on an obligation — the same
    reasoning that leaves 031's cold-start requeue ungated.

    ⚠️ The target protections are RE-CHECKED here. An ``admin`` grant made
    between the mint and the settlement must win, or a paid offer outlives the
    authority it was aimed at.

    Any failure writes a CREDIT (redeemable once, same verb, same room — see
    :func:`_redeem_credit`, which is what spends it) and an honest message. There
    is deliberately no refund verb: an outbound payment is a money-SPEND and
    belongs on the owner-approved lane (046 §8).

    ``perform_fn`` is injected — `core` may not import `surfaces`. It is
    ``async (offer, effect, until_ts) -> object with .ok/.reason``; absent, it
    is resolved from the container service ``room_moderator``
    (`surfaces/telegram/room_moderator.py`).
    """
    now = now or _time.time()
    store = _store(container)
    row = store.get(offer_id)
    if row is None:
        return OfferResult(False, f"unknown offer {offer_id}",
                           reason_code="unknown")
    if row.status == "applied":
        return OfferResult(True, "already applied", offer_id=offer_id)
    if row.status in ("credited", "expired", "refused", "redeemed"):
        return OfferResult(False, f"offer {offer_id} is {row.status}",
                           offer_id=offer_id, reason_code=row.status)

    eff = effect(row.verb)
    if eff is None:
        return _credit(container, store, row,
                       f"{row.verb} is no longer a catalog verb", now)

    protected, why = await _target_protection(container, row.surface, row.chat_id,
                                        row.target_user_id, row.requester_id,
                                        eff=eff,
                                        target_status_fn=_status_probe(container))
    if protected:
        return _credit(container, store, row,
                       "the target became protected before this could apply", now)

    if perform_fn is None:
        try:
            perform_fn = container.get_service("room_moderator")
        except Exception:
            perform_fn = None
    if perform_fn is None:
        return _credit(container, store, row,
                       "no chat transport was available to apply it", now)

    until_ts = int(now + row.duration_sec) if row.duration_sec else 0
    try:
        result = await perform_fn(row, eff, until_ts)
    except Exception as e:
        logger.warning("room action: perform raised for %s (%s)", offer_id, e)
        return _credit(container, store, row, f"the chat rejected it: {e}", now)
    if not getattr(result, "ok", False):
        return _credit(container, store, row,
                       getattr(result, "reason", "") or "the chat rejected it",
                       now)

    store.set_status(offer_id, "applied", applied_at=now)
    _event(container, "room_action_applied", offer_id=offer_id, verb=row.verb,
           chat_id=row.chat_id, asset_id=row.asset_id, price_usd=row.price_usd)
    return OfferResult(True, receipt(row, eff), offer_id=offer_id)


def mark_settled(container: Any, offer_id: str, *,
                 now: Optional[float] = None) -> bool:
    """Record that the offer's invoice SETTLED, before the effect is attempted.

    A separate durable step on purpose: if the process dies between the money
    arriving and the effect landing, the row still says ``paid`` and the
    obligation is visible rather than lost.

    ⚠️ ONLY ``pending -> paid``. It used to move a row from ANY status, which
    silently resurrected an EXPIRED offer into ``paid`` and applied an effect we
    had already told the payer would not happen. A settlement against a
    non-pending row returns False, and the caller reports the late payment
    instead of acting on it.
    """
    now = now or _time.time()
    ok = _store(container).settle_pending(offer_id, now)
    if ok:
        _event(container, "room_action_settled", offer_id=offer_id)
    return ok


def _credit(container: Any, store, row, reason: str, now: float) -> OfferResult:
    """Record a credit, and say plainly what failed.

    ⚠️ Credit, NEVER cash. An automatic outbound refund is a money-SPEND verb:
    it would need a `tx_guard` intent, the owner-approved lane and every money
    gate list. The credit discharges the obligation with no new spend path — and
    since phase 2 it is genuinely spendable (`_redeem_credit`).
    """
    credit_id = f"cr_{uuid.uuid4().hex[:12]}"
    store.set_status(row.offer_id, "credited", reason=str(reason)[:300],
                     credit_id=credit_id, applied_at=now)
    _event(container, "room_action_credited", offer_id=row.offer_id,
           verb=row.verb, chat_id=row.chat_id, reason=str(reason)[:200],
           credit_id=credit_id)
    # ⚠️ "I took payment" is FALSE when this offer was itself a redeemed credit
    # — no money changed hands this time, and telling a payer otherwise is the
    # kind of confident wrong sentence the whole review was about. The credit
    # still rolls forward either way, which is the part that matters to them.
    took = ("I took payment for" if float(row.price_usd or 0) > 0
            else "I used your credit for")
    return OfferResult(
        False,
        f"⚠️ {took} {row.verb} but could not apply it: {reason}. "
        f"You hold credit {credit_id}, good for one {row.verb} in this room at "
        f"no further charge — just ask again. The owner has been notified.",
        offer_id=row.offer_id, reason_code="apply_failed")


#: The past participle of each catalog verb. A literal table, because deriving
#: it ("ban" + "d") produced "band", and a receipt is the one line the room
#: reads to learn what it paid for.
_PAST = {"mute": "muted", "unmute": "unmuted",
         "ban": "banned", "unban": "unbanned"}


def receipt(row, eff) -> str:
    """The ONE receipt text, rendered by core."""
    if row.duration_sec:
        minutes = row.duration_sec // 60
        dur = (f" for {minutes} min" if minutes < 120
               else f" for {row.duration_sec // 3600}h")
    else:
        dur = ""
    undo = (f" They can end it early with /{eff.reversed_by}."
            if eff.reversed_by else "")
    who = row.target_name or row.target_user_id
    return f"✅ {who} is {_PAST.get(row.verb, row.verb + 'd')}{dur}.{undo}"
