"""Cron delivery channels a pack contributes (067 P3b).

``cron/delivery.py`` delivers a cron job's final report to a chat surface (the
surface catalog) or to a named CHANNEL. A channel that is not a chat surface —
X's public post sink ``twitter`` — used to be a hard-coded branch that imported
the tool. It is now a registration: a pack declares the channel name in its
``pack.toml`` (``delivery_channels = [...]``, DATA, read in the loader's
phase 1) and registers the sender through the ``cron.delivery_channel`` hook in
phase 2::

    hooks={"cron.delivery_channel": {"twitter": "polyrob_x.cron_delivery:deliver_post"}}

A sender is ``async (task_agent, job, final) -> bool | str`` (the
``deliver_result_ex`` outcome vocabulary). Core never imports a pack: an
absent sender is reported by :func:`unavailable_reason`, never a crash.

⚠️ Layering: core tier. It reads the pack loader state only when a loader ran
in this process (``sys.modules``), like ``core.config_policy.profiles.provided``.
"""
import sys
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

Sender = Callable[..., Awaitable[Any]]

#: channel name -> (pack id, sender)
_CHANNELS: Dict[str, Tuple[str, Sender]] = {}


def register_channel(name: str, sender: Sender, *, pack_id: str) -> None:
    """Register *sender* for channel *name* (the loader's phase 2)."""
    if not callable(sender):
        raise TypeError(f"delivery channel {name!r}: sender is not callable")
    owner = _CHANNELS.get(name)
    if owner is not None and owner[0] != pack_id:
        raise ValueError(f"delivery channel {name!r} is already registered by pack {owner[0]!r}")
    _CHANNELS[name] = (pack_id, sender)


def sender_for(name: str) -> Optional[Sender]:
    row = _CHANNELS.get(name)
    return row[1] if row else None


def unregister_pack(pack_id: str) -> None:
    """Roll back senders contributed by a pack that failed to load."""
    for name, (owner, _) in list(_CHANNELS.items()):
        if owner == pack_id:
            del _CHANNELS[name]


def _state():
    return sys.modules.get("core.packs.state")


def declared() -> Dict[str, str]:
    """channel name -> pack id, from the installed (not refused) packs'
    manifests. Includes a pack that phase 2 left disabled, so a stored job's
    channel stays recognised and its delivery can name why it did not go."""
    state = _state()
    out: Dict[str, str] = {}
    if state is None:
        return out
    for rec in state.records():
        m = rec.manifest
        if m is None or rec.status == state.REFUSED:
            continue
        for name in getattr(m, "delivery_channels", ()) or ():
            out.setdefault(name, rec.id)
    return out


def channel_names() -> Tuple[str, ...]:
    """Every channel name this install knows: registered or declared."""
    names = list(_CHANNELS)
    for name in declared():
        if name not in names:
            names.append(name)
    return tuple(names)


def unavailable_reason(name: str) -> Optional[str]:
    """Why channel *name* cannot deliver in this process, or None when a
    sender is registered."""
    if name in _CHANNELS:
        return None
    pack_id = declared().get(name)
    if pack_id is None:
        return f"no installed pack provides the delivery channel {name!r}"
    state = _state()
    rec = state.record(pack_id) if state is not None else None
    if rec is None:
        return f"delivery channel {name!r} comes from pack {pack_id!r}, which is not installed"
    why = f" ({rec.reason})" if rec.reason else ""
    return (f"delivery channel {name!r} comes from pack {pack_id!r}, which is "
            f"{rec.status}{why}")


def reset_for_tests() -> None:
    _CHANNELS.clear()


__all__ = ["Sender", "channel_names", "declared", "register_channel", "sender_for",
           "unavailable_reason"]
