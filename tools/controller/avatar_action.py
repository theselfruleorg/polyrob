"""The `agent_avatar` action — the agent knows it has a face, sends it, and sets it.

The avatar is ONE image slot per instance (`core/avatar.py`). Core generates no
face; the image comes from a file, a URL, or an NFT's image. Three halves:

- **read** — the instance and the slot: set (and from where) / not set /
  unreadable.
- **attach** — copy the image into the SESSION WORKSPACE and return the path, so
  `message(media_paths=["avatar.png"])` delivers it over the rail that already
  exists.
- **set** — `set_from` = a workspace image file, an https/ipfs/data URL, or
  `nft:chain:contract:id`. An identity change, so ONLY on an owner turn.

⚠️ The copy is the design, not a shortcut. `core/surfaces/attachments.py::
validate_media_paths` requires every media path to resolve INSIDE the session
workspace, and special-casing one blessed file would weaken that confinement
rule for every caller. Materialising the file keeps exactly one rule. The same
rule confines a `set_from` file path.

⚠️ `set_from` refuses on a delegated, leaf, forged (self-wake / delegation
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
from typing import Optional

from pydantic import BaseModel, Field

from agents.task.agent.views import ActionResult

logger = logging.getLogger(__name__)

#: The workspace filename stem the image is materialised as (`avatar.<ext>`).
WORKSPACE_STEM = "avatar"

_NO_AVATAR = (
    "avatar: not set. That is a normal, optional state — the owner sets one with "
    "`polyrob avatar set <image|url|nft:chain:contract:id>` or `/avatar set` in "
    "the chat, or asks me to set it on an owner turn."
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


def _set_refusal(execution_context) -> Optional[str]:
    from core.security.owner_turn import owner_turn_refusal
    refusal = owner_turn_refusal(
        execution_context, verb="agent_avatar set_from",
        does="changes the agent's face", public="change the agent's avatar")
    if refusal:
        return refusal
    from agents.task.session_class import is_autonomous_session
    if is_autonomous_session(getattr(execution_context, "session_id", None)):
        return ("agent_avatar set_from denied: an autonomous (cron / goal) run "
                "never changes the agent's face — only an owner turn may")
    return None


async def _image_for(set_from: str, workspace: Optional[str]):
    """``(bytes, source)`` for ``set_from``. Raises ``core.avatar.AvatarError``."""
    from core.avatar import AvatarError, MAX_BYTES
    from tools import avatar_sources
    ref = set_from.strip()
    if ref.startswith("nft:"):
        chain, contract, token_id = avatar_sources.parse_nft_ref(ref[len("nft:"):])
        return await avatar_sources.image_from_nft(chain, contract, token_id)
    if ref.startswith(("https://", "http://", "ipfs://", "data:")):
        return await avatar_sources.image_from_url(ref)
    from core.surfaces.attachments import validate_media_paths
    validated, err = validate_media_paths([ref], workspace)
    if err:
        raise AvatarError(err)
    p = Path(validated[0])
    if not p.is_file():
        raise AvatarError(f"no such file in the workspace: {ref}")
    if p.stat().st_size > MAX_BYTES:
        raise AvatarError(f"{p.name} is {p.stat().st_size} bytes; the limit is {MAX_BYTES}")
    return p.read_bytes(), f"file:{p.name}"


def register_avatar_action(controller) -> None:
    """Register the `agent_avatar` action (read / attach / set), gated
    AVATAR_TOOL_ENABLED (default OFF; ON under POLYROB_LOCAL)."""
    from core.config_policy import AutonomyConfig
    if not AutonomyConfig.avatar_tool_enabled():
        return

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

    @controller.registry.action(
        "Your avatar: the one image slot that is your face (console, chat, "
        "on-chain registration; the X/Telegram profile photos follow it only when "
        "the owner runs `polyrob avatar push`). Reads whether it is set and "
        "where it came from. With attach=true it also places the image in your "
        "workspace so you can send it with message(media_paths=[...]). With "
        "set_from it replaces the image — only when the owner asks.",
        param_model=AgentAvatarAction,
    )
    async def agent_avatar(params: AgentAvatarAction, execution_context=None) -> ActionResult:
        from core.avatar import AvatarError, describe, load_avatar, set_avatar
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

        if params.set_from:
            refusal = _set_refusal(execution_context)
            if refusal:
                return ActionResult(error=refusal, include_in_memory=True)
            try:
                data, source = await _image_for(params.set_from, ws)
                st = set_avatar(home, instance_id, data, source=source)
            except AvatarError as e:
                return ActionResult(error=f"agent_avatar set_from refused: {e}",
                                    include_in_memory=True)
            except Exception as e:
                logger.warning("agent_avatar: set failed", exc_info=True)
                return ActionResult(error=f"agent_avatar set_from failed: "
                                          f"{type(e).__name__}: {e}",
                                    include_in_memory=True)
            lines.append(f"avatar: {describe(st)} — the console and the on-chain "
                         f"registration show it now; the X/Telegram profile photos "
                         f"keep the old image until the owner runs `polyrob avatar push`")
        else:
            st = load_avatar(home, instance_id)
            if st.state == "none":
                lines.append(_NO_AVATAR)
                return ActionResult(extracted_content="\n".join(lines),
                                    include_in_memory=True)
            lines.append(f"avatar: {describe(st)}")
            if st.is_set and st.set_at:
                lines.append(f"set at: {st.set_at}")

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
