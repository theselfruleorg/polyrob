"""046: the ONE owner-admin helper set for paid room actions.

Every seat that configures or inspects paid actions — Telegram `/paid`,
`polyrob owner paid …`, the REPL, a console panel — renders through this module,
never through `OfferStore`/`chat_policy`/`core.payments.assets` directly. One
function, one verified sentence back. Mirrors `core/surfaces/group_admin.py`.

Pure core: no import from `agents`/`surfaces`/`tools`/`modules` at module top.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Seat-aware remedies
# ---------------------------------------------------------------------------
#
# Every sentence this module returns is rendered VERBATIM by whichever seat
# called it, and the remedies inside them were slash verbs with `here` — a
# grammar that exists only in a chat. `polyrob owner paid show <chat>` printed
# "set a price (/paid price mute 0.50)" and "grant with `/groups set here …`"
# at a SHELL prompt, where neither is a command the operator can type. Same
# defect, same fix, as `core/surfaces/inbox_render.py`'s remedy tables (C21).
#
# The values are format strings; the keys are the remedy, not the verb, so a
# seat that renames a verb cannot silently lose one. A caller passes the table
# for ITS seat; the CHAT form is the default, so Telegram is byte-identical.

RemedyTable = Mapping[str, str]

#: Telegram / the REPL: slash verbs, `here` for the room you are in.
CHAT_REMEDIES: Dict[str, str] = {
    "price": "/paid price mute 0.50",
    "asset": "/paid asset rob",
    "enable": "/paid enable",
    "disable": "/paid disable",
    "member_verbs": "/groups set here member_verbs {verbs}",
    "max_duration": "/groups set here paid_{verb}_max_duration <e.g. 24h>",
}

#: The `polyrob` CLI. Its verbs are `polyrob owner …` and they name the room
#: explicitly — there is no `here` at a shell prompt.
CLI_REMEDIES: Dict[str, str] = {
    "price": "polyrob owner paid price {chat_id} mute 0.50",
    "asset": "polyrob owner paid asset {chat_id} rob",
    "enable": "polyrob owner paid enable {chat_id}",
    "disable": "polyrob owner paid disable {chat_id}",
    "member_verbs":
        "polyrob owner groups set {surface} {chat_id} member_verbs {verbs}",
    "max_duration":
        "polyrob owner groups set {surface} {chat_id} "
        "paid_{verb}_max_duration <e.g. 24h>",
}


def _remedy(remedies: Optional[RemedyTable], key: str, **fields: Any) -> str:
    """One remedy, rendered for the caller's seat. Fail-open to the chat form.

    A missing key or a bad field never raises into a reply — an owner sentence
    without its remedy is poor, an owner sentence that 500s is worse.
    """
    table = remedies if remedies is not None else CHAT_REMEDIES
    spec = table.get(key) or CHAT_REMEDIES.get(key) or ""
    try:
        return spec.format(**fields)
    except Exception:
        logger.warning("room_action_admin: remedy %r unrenderable for %r",
                       key, sorted(fields), exc_info=True)
        return spec


def _data_dir(container: Any) -> str:
    from core.surfaces.group_admin import data_home
    return data_home(container)


def _owner_uid() -> str:
    from core.instance import resolve_owner_user_id
    return resolve_owner_user_id() or ""


def _policy(container: Any, surface: str, chat_id: str):
    from core.surfaces.chat_policy import load_for_chat
    return load_for_chat(_data_dir(container), surface, str(chat_id))


def _store(container: Any):
    from core.surfaces.room_action_store import OfferStore, store_path
    return OfferStore(store_path(_data_dir(container)))


def _set(container: Any, surface: str, chat_id: str, key: str, value) -> str:
    from core.surfaces import chat_policy
    ok, msg = chat_policy.set(_data_dir(container), _owner_uid(), surface,
                              str(chat_id), key, value)
    return "" if ok else f"❌ {msg}"


def status(container: Any, surface: str, chat_id: str,
           remedies: Optional[RemedyTable] = None) -> str:
    """What this room sells, at what price, in what asset.

    ``remedies`` is this seat's remedy table (:data:`CHAT_REMEDIES` by default,
    :data:`CLI_REMEDIES` for ``polyrob owner paid``).
    """
    from core.surfaces import room_actions
    label = f"{surface}:{chat_id}"
    policy = _policy(container, surface, chat_id)
    if not policy.paid_enabled:
        return (f"Paid actions are not enabled in {label}. Set a price "
                f"({_remedy(remedies, 'price', surface=surface, chat_id=chat_id)}), "
                f"an asset ({_remedy(remedies, 'asset', surface=surface, chat_id=chat_id)}), "
                f"then {_remedy(remedies, 'enable', surface=surface, chat_id=chat_id)}.")
    asset = policy.paid_asset or "(instance default)"
    lines = [f"Paid actions in {label} — asset {asset}"]
    for verb in room_actions.VERBS:
        # ⚠️ ONE price reader. This used to be `getattr(policy,
        # f"paid_{verb}_usd", 0.0)`, which returns the DEFAULT for a verb whose
        # field does not exist — so a verb could read as priced in one place and
        # unpriced in the other, and neither said why.
        price, _lo, _hi = room_actions._price_band(policy, verb)
        if price > 0:
            dur = room_actions._max_duration_spec(policy, verb)
            lines.append(f"• {verb}: ${price:.2f}"
                         + (f" (up to {dur})" if dur else ""))
    if len(lines) == 1:
        lines.append("• (no verb is priced — nothing is for sale)")
    lines.append(f"caps: {policy.paid_max_per_payer_day}/payer/day, "
                 f"{policy.paid_max_per_target_day}/target/day, "
                 f"offers expire in {policy.paid_offer_ttl}")
    member = ", ".join(policy.member_verbs or ()) or "(none)"
    lines.append(f"member verbs: {member}")
    # ⚠️ Per PRICED verb, not just `mute`. A room that prices `ban` and grants
    # only `mute` sells something no member can ask for, and nothing said so.
    granted = set(policy.member_verbs or ())
    priced = [v for v in room_actions.VERBS
              if room_actions._price_band(policy, v)[0] > 0]
    ungranted = [v for v in priced if v not in granted]
    if ungranted:
        _grant = _remedy(remedies, "member_verbs", surface=surface,
                         chat_id=chat_id,
                         verbs=",".join(sorted(granted | set(ungranted))))
        lines.append(
            f"⚠️ priced but NOT grantable to members here: "
            f"{', '.join(ungranted)} — grant with `{_grant}`")
    open_rows = _store(container).open_offers(surface, str(chat_id))
    if open_rows:
        lines.append(f"{len(open_rows)} offer(s) awaiting payment")
    return "\n".join(lines)


def set_price(container: Any, surface: str, chat_id: str, verb: str,
              usd: float, remedies: Optional[RemedyTable] = None) -> str:
    """Price one verb. An unknown verb echoes the vocabulary."""
    from core.surfaces import room_actions
    eff = room_actions.effect(verb)
    if eff is None:
        return (f"❌ {verb!r} is not a paid action. I sell: "
                f"{', '.join(room_actions.VERBS)}.")
    try:
        price = float(usd)
    except (TypeError, ValueError):
        return f"❌ {usd!r} is not a price."
    if price <= 0:
        return ("❌ a price must be positive — to stop selling a verb, use "
                f"{_remedy(remedies, 'disable', surface=surface, chat_id=chat_id)}"
                " or set the room's other verbs instead.")
    err = _set(container, surface, chat_id, f"chat.paid_{eff.verb}_usd", price)
    if err:
        return err
    dur = _max_dur_hint(eff.verb, surface, chat_id, remedies)
    return (f"✅ {eff.verb} costs ${price:.2f} in {surface}:{chat_id}." + dur)


def _max_dur_hint(verb: str, surface: str = "", chat_id: str = "",
                  remedies: Optional[RemedyTable] = None) -> str:
    """Name the duration key a priced verb also has, so an owner does not have
    to discover it. Only the verbs that TAKE a duration have one."""
    from core.surfaces import room_actions
    eff = room_actions.effect(verb)
    if eff is None or not eff.needs_duration:
        return ""
    return " Longest {}: {}.".format(
        verb, _remedy(remedies, "max_duration", verb=verb, surface=surface,
                      chat_id=chat_id))


def set_asset(container: Any, surface: str, chat_id: str, asset_id: str) -> str:
    """Set the asset this room is paid in. An unconfigured id REFUSES."""
    from core.payments.assets import resolve, vocabulary
    row = resolve((asset_id or "").strip().lower(),
                  data_home=_data_dir(container))
    if row is None:
        return (f"❌ {asset_id!r} is not a configured asset (known: "
                f"{vocabulary(data_home=_data_dir(container))}). Pin one with "
                f"`polyrob wallet asset add`.")
    err = _set(container, surface, chat_id, "chat.paid_asset", row.asset_id)
    return err or (f"✅ {surface}:{chat_id} is paid in {row.symbol} "
                   f"({row.asset_id}) on {row.chain}.")


def enable(container: Any, surface: str, chat_id: str,
           remedies: Optional[RemedyTable] = None) -> str:
    """Turn paid actions on.

    ⚠️ Refuses when nothing is priced. Enabling a room that sells nothing
    reads as working and refuses every member who tries.
    """
    from core.surfaces import room_actions
    policy = _policy(container, surface, chat_id)
    priced = [v for v in room_actions.VERBS
              if room_actions._price_band(policy, v)[0] > 0]
    if not priced:
        return ("❌ nothing is priced in this room yet — set a price first "
                f"({_remedy(remedies, 'price', surface=surface, chat_id=chat_id)})"
                ", or members will just be refused.")
    err = _set(container, surface, chat_id, "chat.paid_enabled", True)
    if err:
        return err
    return (f"✅ Paid actions ON in {surface}:{chat_id} for "
            f"{', '.join(priced)}.")


def disable(container: Any, surface: str, chat_id: str) -> str:
    err = _set(container, surface, chat_id, "chat.paid_enabled", False)
    return err or (f"✅ Paid actions OFF in {surface}:{chat_id}. A member's "
                   f"/mute is now refused, not priced.")


def offers(container: Any, surface: str, chat_id: str, limit: int = 10) -> str:
    """Recent offers in this room, newest first."""
    rows = _store(container).recent(surface, str(chat_id), limit=limit)
    if not rows:
        return f"No paid actions in {surface}:{chat_id} yet."
    lines = [f"Last {len(rows)} paid action(s) in {surface}:{chat_id}:"]
    for r in rows:
        when = time.strftime("%m-%d %H:%M", time.gmtime(r.created_at))
        who = r.target_name or r.target_user_id
        note = f" — {r.reason}" if r.reason else ""
        lines.append(f"• [{when}] {r.verb} {who} ${r.price_usd:.2f} "
                     f"{r.status}{note} ({r.offer_id})")
    return "\n".join(lines)


def cancel(container: Any, offer_id: str, *, by: str = "owner",
           surface: Any = None, chat_id: Any = None) -> str:
    """Withdraw a PENDING offer.

    ⚠️ Refuses anything already paid. Cancelling a paid offer would take the
    money and cancel the service; that case is a credit, not a cancellation.

    ⚠️ D15: ``surface``/``chat_id`` SCOPE the cancellation to one room. The
    offer store is global, and `/paid` is reachable by a ROOM ADMIN — so
    without the scope one room's admin could withdraw any other room's offer
    by quoting its id, which `/paid offers` next door prints in full. Omitting
    them is the unscoped legacy call and is reserved for the owner's own seats.
    """
    store = _store(container)
    row = store.get(offer_id)
    if row is None:
        return f"❌ unknown offer {offer_id}."
    if surface is not None and chat_id is not None:
        if (str(row.surface) != str(surface)
                or str(row.chat_id) != str(chat_id)):
            # Deliberately the SAME sentence as an unknown id: confirming that
            # an id exists somewhere else is itself information about another
            # room's trade.
            return f"❌ unknown offer {offer_id}."
    if row.status != "pending":
        return (f"❌ offer {offer_id} is {row.status}, not pending — a paid "
                f"offer cannot be cancelled, only credited.")
    store.set_status(offer_id, "refused", reason=f"cancelled by {by}")
    return f"✅ Offer {offer_id} withdrawn ({row.verb} on {row.target_name or row.target_user_id})."


def credits_owed(container: Any) -> List:
    """Paid offers whose effect never landed. Money we HOLD."""
    try:
        return _store(container).credits_owed()
    except Exception as e:
        logger.warning("room actions: credits-owed read failed (%s)", e)
        raise


def render_credits(container: Any) -> str:
    try:
        rows = credits_owed(container)
    except Exception as e:
        return f"⚠️ paid-action credits unreadable ({e.__class__.__name__})."
    if not rows:
        return "No paid-action credits owed."
    lines = [f"⚠️ {len(rows)} paid-action credit(s) owed:"]
    for r in rows:
        lines.append(f"• {r.offer_id}: {r.verb} in {r.surface}:{r.chat_id} "
                     f"${r.price_usd:.2f} — {r.reason}")
    return "\n".join(lines)


__all__ = ["CHAT_REMEDIES", "CLI_REMEDIES", "RemedyTable",
           "cancel", "credits_owed", "disable", "enable", "offers",
           "render_credits", "set_asset", "set_price", "status"]
