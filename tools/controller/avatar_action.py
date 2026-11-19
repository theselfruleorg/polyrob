"""The `agent_avatar` action — the agent knows it has a face, sends it, sets it,
clears it and pushes it to its own accounts.

The avatar is ONE image slot per instance (`core/avatar.py`). Core generates no
face; the image comes from a file, a URL, or an NFT's image. The parts:

- **read** — the instance and the slot: set (and from where) / not set /
  unreadable.
- **attach** — copy the image into the SESSION WORKSPACE and return the path, so
  `message(media_paths=["avatar.png"])` delivers it over the rail that already
  exists.
- **set** — `set_from` = a workspace image file, an https/ipfs/data URL, or
  `nft:chain:contract:id`. An identity change, so ONLY on an owner turn.
- **clear** — back to the default mark. The same rule as `set_from`.
- **push** — make the agent's own account pictures (X, Telegram, Discord) show
  the slot: `modules.avatar.push.push_avatar`, the ONE procedure the CLI and the
  chat `/avatar push` also run (no switch: the credentials decide; hash-idempotent). The same rule as `set_from`: it changes the public face.

⚠️ The copy is the design, not a shortcut. `core/surfaces/attachments.py::
validate_media_paths` requires every media path to resolve INSIDE the session
workspace, and special-casing one blessed file would weaken that confinement
rule for every caller. Materialising the file keeps exactly one rule. The same
rule confines a `set_from` file path.

⚠️ `set_from`, `clear` and `push` refuse on a delegated, leaf, forged (self-wake / delegation
re-entry), autonomous (cron / goal) or non-owner turn
(`core.security.owner_turn.owner_turn_refusal` + `is_autonomous_session`): a
model turn nobody asked for must not change the agent's face.

⚠️ Registry-closure landmine: NO `from __future__ import annotations` in this
module — the registry introspects the closure's first-param annotation to route
the validated param model (GLM live-test bug 2026-06-20).

⚠️ Registered from `tools/controller/service.py`, NOT `action_registration.py`:
that file sits at exactly its size-ratchet ceiling, so even a four-line
delegator fails `tests/test_file_size_ratchet.py`. Same escape hatch
`autonomy_control_action.py` established.
"""
import logging
import shutil
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

from agents.task.agent.views import ActionResult

logger = logging.getLogger(__name__)

#: The workspace filename stem the image is materialised as (`avatar.<ext>`).
WORKSPACE_STEM = "avatar"

_NO_AVATAR = (
    "avatar: not set. That is a normal, optional state — the owner sets one with "
    "`/avatar set <url|nft:chain:contract:id>` in the chat, or asks me to set it "
    "(agent_avatar set_from) on an owner turn."
)


def _data_dir(controller) -> Optional[str]:
    from core.runtime_paths import container_data_home
    return container_data_home(getattr(controller, "container", None))


def _workspace(controller, execution_context) -> Optional[str]:
    for holder in (execution_context, getattr(controller, "orchestrator", None)):
        ws = getattr(holder, "workspace_dir", None)
        if ws:
            return str(ws)
    return None


def _set_refusal(execution_context, verb: str = "set_from") -> Optional[str]:
    """The ONE rule for every write of the face (set, clear, push)."""
    from core.security.owner_turn import owner_turn_refusal
    refusal = owner_turn_refusal(
        execution_context, verb=f"agent_avatar {verb}",
        does="changes the agent's face", public="change the agent's avatar")
    if refusal:
        return refusal
    from agents.task.session_class import is_autonomous_session
    if is_autonomous_session(getattr(execution_context, "session_id", None)):
        return (f"agent_avatar {verb} denied: an autonomous run (a cron job or a goal, "
                "also a standing one) never changes the agent's face; the owner asks "
                "for it in a chat turn")
    return None


def _registration_note() -> str:
    """What the on-chain registration shows after a change — hosted (live) or a
    snapshot. Never claims "now" for a snapshot (120 T6)."""
    try:
        from tools.defi.agent_registration import uri_is_hosted
        hosted = uri_is_hosted()
    except Exception:
        return "the on-chain registration: could not tell if it follows the slot"
    if hosted:
        return ("an on-chain registration that points at my live registration file "
                "shows it now")
    return ("an on-chain registration made on this setup is a snapshot: it keeps the "
            "old image until defi_trade set_agent_uri writes a new one")


def _allowed_telegram_rooms(controller, home: str) -> set:
    """Chat ids of the Telegram rooms the owner put me in (a push target must be
    one of them). Raises when the allowlist cannot be read."""
    from core.surfaces.group_allowlist import GroupAllowlist
    from core.surfaces.room_targets import scoped_rooms
    rows = GroupAllowlist(f"{home}/group_allowlist.db").list_all()
    rooms = scoped_rooms(controller, [r for r in rows if r.get("status") == "active"])
    return {str(r["chat_id"]) for r in rooms if r.get("surface") == "telegram"}


async def _push(controller, home: str, instance_id: str, names) -> list:
    """The lines of one push. ``telegram:<chat_id>`` names a room's photo."""
    import asyncio
    from modules.avatar import push as pushmod
    surfaces, chats = [], []
    for raw in names:
        word = str(raw).strip()
        if word.lower().startswith("telegram:") and word[len("telegram:"):].strip():
            chats.append(word[len("telegram:"):].strip())
        elif word:
            surfaces.append(word)
    if chats:
        try:
            allowed = _allowed_telegram_rooms(controller, home)
        except Exception as e:
            return [f"push: the room list could not be read ({type(e).__name__}: {e}); "
                    f"no room photo was changed"]
        strangers = [c for c in chats if c not in allowed]
        if strangers:
            return [f"push refused: {', '.join(strangers)} is not a Telegram room the owner "
                    f"put me in (room_read lists them); nothing was pushed"]
    results = await asyncio.to_thread(pushmod.push_avatar, home, instance_id,
                                      surfaces=surfaces, telegram_chats=chats)
    return ["push:"] + ["  " + line for r in results for line in r.text.splitlines()]


def register_avatar_action(controller) -> None:
    """Register the `agent_avatar` action (read / attach / set / clear / push). No
    switch: a read is free and every write needs an owner turn."""

    class AgentAvatarAction(BaseModel):
        attach: bool = Field(
            default=False,
            description=("Also copy the avatar image into this session's "
                         "workspace and return its path, so it can be sent with "
                         "message(media_paths=[...]). Reading is free; only set "
                         "this when you actually intend to send it."))
        set_from: Optional[str] = Field(
            default=None,
            description=("Set a NEW avatar image (replaces the current one). One "
                         "of: a workspace image file path, an https/ipfs/data "
                         "URL, or nft:chain:contract:token_id (the image its "
                         "metadata names). Only when the owner asks you to — it "
                         "is refused on any turn that is not the owner's."))
        clear: bool = Field(
            default=False,
            description=("Remove the avatar image (the default mark comes back). "
                         "Only when the owner asks you to."))
        push: Optional[List[str]] = Field(
            default=None,
            description=("Make your own account pictures show the avatar. A list "
                         "of: 'x', 'telegram', 'discord', or 'telegram:<chat_id>' "
                         "for the photo of a room you are in; an empty list = each "
                         "connected account. Each one answers updated / unchanged / "
                         "not connected / failed. With "
                         "set_from in the same call the new image is set first. "
                         "Only when the owner asks you to."))

    @controller.registry.action(
        "Your avatar: the one image slot that is your face (console, chat, "
        "on-chain registration). Your X, Telegram and Discord account pictures "
        "are copies: they change only on a push. Reads whether it is set and "
        "where it came from. With attach=true it also places the image in your "
        "workspace so you can send it with message(media_paths=[...]). With "
        "set_from it replaces the image, with clear it removes it, with push it "
        "updates your account pictures — each only when the owner asks.",
        param_model=AgentAvatarAction,
    )
    async def agent_avatar(params: AgentAvatarAction, execution_context=None) -> ActionResult:
        from core.avatar import AvatarError, clear_avatar, describe, load_avatar, set_avatar
        from core.instance import resolve_instance_id

        instance_id = resolve_instance_id()
        home = _data_dir(controller)
        if not home:
            return ActionResult(
                extracted_content=(f"instance: {instance_id}\navatar: unavailable "
                                   f"(no data home resolves for this session)"),
                include_in_memory=True)
        ws = _workspace(controller, execution_context)
        lines = [f"instance: {instance_id}"]

        writes = [w for w, on in (("set_from", params.set_from), ("clear", params.clear),
                                  ("push", params.push is not None)) if on]
        if params.set_from and params.clear:
            return ActionResult(error="agent_avatar: set_from and clear are opposites; "
                                      "give one", include_in_memory=True)
        if writes:
            refusal = _set_refusal(execution_context, verb=writes[0])
            if refusal:
                return ActionResult(error=refusal, include_in_memory=True)

        if params.set_from:
            try:
                from tools import avatar_sources
                data, source = await avatar_sources.image_from_ref(
                    params.set_from, workspace=ws or "")
                st = set_avatar(home, instance_id, data, source=source)
            except AvatarError as e:
                return ActionResult(error=f"agent_avatar set_from refused: {e}",
                                    include_in_memory=True)
            except Exception as e:
                logger.warning("agent_avatar: set failed", exc_info=True)
                return ActionResult(error=f"agent_avatar set_from failed: "
                                          f"{type(e).__name__}: {e}",
                                    include_in_memory=True)
            lines.append(f"avatar: {describe(st)} — the console and the chat show it now; "
                         f"{_registration_note()}")
            if params.push is None:
                lines.append("my X, Telegram and Discord pictures keep the old image "
                             "until a push (agent_avatar push=[])")
        elif params.clear:
            try:
                removed = clear_avatar(home, instance_id)
            except Exception as e:
                logger.warning("agent_avatar: clear failed", exc_info=True)
                return ActionResult(error=f"agent_avatar clear failed: "
                                          f"{type(e).__name__}: {e}", include_in_memory=True)
            st = load_avatar(home, instance_id)
            lines.append(("avatar: cleared; " if removed else "avatar: there was none to "
                          "clear; ") + f"it is {describe(st)}")
        else:
            st = load_avatar(home, instance_id)
            if st.state == "none":
                lines.append(_NO_AVATAR)
                return ActionResult(extracted_content="\n".join(lines),
                                    include_in_memory=True)
            lines.append(f"avatar: {describe(st)}")
            if st.is_set and st.set_at:
                lines.append(f"set at: {st.set_at}")

        if params.push is not None:
            lines.extend(await _push(controller, home, instance_id, params.push))

        if params.attach:
            if not st.is_set:
                lines.append("attach: there is no readable image to attach")
            elif not ws:
                lines.append("attach: no session workspace is available, so I "
                             "cannot place the image where a send can reach it")
            else:
                name = f"{WORKSPACE_STEM}{st.path.suffix}"
                try:
                    dest = Path(ws) / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(st.path, dest)
                    lines.append(
                        f"attached: {name} is now in your workspace — "
                        f"send it with message(media_paths=[\"{name}\"])")
                    if not st.is_raster:
                        lines.append("note: it is an SVG; some chat surfaces "
                                     "deliver it as a file, not a picture")
                except Exception as e:
                    logger.warning("agent_avatar: attach failed", exc_info=True)
                    lines.append(f"attach failed: {type(e).__name__}: {e}")

        return ActionResult(extracted_content="\n".join(lines), include_in_memory=True)
