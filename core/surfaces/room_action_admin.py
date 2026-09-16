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
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


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


def status(container: Any, surface: str, chat_id: str) -> str:
    """What this room sells, at what price, in what asset."""
    from core.surfaces import room_actions
    label = f"{surface}:{chat_id}"
    policy = _policy(container, surface, chat_id)
    if not policy.paid_enabled:
        return (f"Paid actions are not enabled in {label}. Set a price "
                f"(/paid price mute 0.50), an asset (/paid asset rob), then "
                f"/paid enable.")
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
        lines.append(
            f"⚠️ priced but NOT grantable to members here: "
            f"{', '.join(ungranted)} — grant with `/groups set here "
            f"member_verbs {','.join(sorted(granted | set(ungranted)))}`")
    open_rows = _store(container).open_offers(surface, str(chat_id))
    if open_rows:
        lines.append(f"{len(open_rows)} offer(s) awaiting payment")
    return "\n".join(lines)


def set_price(container: Any, surface: str, chat_id: str, verb: str,
              usd: float) -> str:
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
                "/paid disable or set the room's other verbs instead.")
    err = _set(container, surface, chat_id, f"chat.paid_{eff.verb}_usd", price)
    if err:
        return err
    dur = _max_dur_hint(eff.verb)
    return (f"✅ {eff.verb} costs ${price:.2f} in {surface}:{chat_id}." + dur)


def _max_dur_hint(verb: str) -> str:
    """Name the duration key a priced verb also has, so an owner does not have
    to discover it. Only the verbs that TAKE a duration have one."""
    from core.surfaces import room_actions
    eff = room_actions.effect(verb)
    if eff is None or not eff.needs_duration:
        return ""
    return (f" Longest {verb}: /groups set here paid_{verb}_max_duration <e.g. "
            f"24h>.")


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


def enable(container: Any, surface: str, chat_id: str) -> str:
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
                "(/paid price mute 0.50), or members will just be refused.")
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


def cancel(container: Any, offer_id: str, *, by: str = "owner") -> str:
    """Withdraw a PENDING offer.

    ⚠️ Refuses anything already paid. Cancelling a paid offer would take the
    money and cancel the service; that case is a credit, not a cancellation.
    """
    store = _store(container)
    row = store.get(offer_id)
    if row is None:
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


__all__ = ["cancel", "credits_owed", "disable", "enable", "offers",
           "render_credits", "set_asset", "set_price", "status"]
