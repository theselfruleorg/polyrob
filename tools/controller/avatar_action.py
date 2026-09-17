"""The `agent_avatar` action — the agent reads and sends its OWN face.

The Mindprint identity (`avatar/mindprint.js`, `modules/pfp/`, the frozen
`identity/{instance}/pfp/`) has been complete and well-tested since 2026-07-19
and, from the agent's side, was dark: no tool could read it, and the only place
it reached a human at all was the x402 invoice card. This is the seam.

Two halves, both narrow:

- **read** — instance, kept / draft / absent, traits, and the voice signature
  (`core.instance.voice_signature`, whose only other caller is `polyrob pfp say`).
- **attach** — copy `pfp.png` into the SESSION WORKSPACE and return the path, so
  `message(media_paths=["avatar.png"])` delivers it over the rail that already
  exists.

⚠️ The copy is the design, not a shortcut. `core/surfaces/attachments.py::
validate_media_paths` requires every media path to resolve INSIDE the session
workspace, and special-casing one blessed file would weaken that confinement
rule for every caller. Materialising the file keeps exactly one rule.

⚠️ This action can NEVER generate, randomize, keep or push. `modules.pfp.store`
treats `keep` as a one-way permanent lock on the instance's identity, and the
only escape hatch is deleting the directory by hand. That ceremony belongs to
the owner (`polyrob pfp`), not to a model turn — and emphatically not to a
forged or autonomous one.

⚠️ Registry-closure landmine: NO `from __future__ import annotations` in this
module — the registry introspects the closure's first-param annotation to route
the validated param model (GLM live-test bug 2026-06-20).

⚠️ Registered from `tools/controller/service.py`, NOT `action_registration.py`:
that file sits at exactly its size-ratchet ceiling (1971), so even a four-line
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

#: The workspace filename the face is materialised as. Stable so the agent can
#: reference it across a turn without re-calling the action.
WORKSPACE_NAME = "avatar.png"

_NO_AVATAR = (
    "avatar: not set up for this instance. That is a normal, optional state — "
    "the owner runs `polyrob pfp generate` and then `polyrob pfp keep` (which is "
    "permanent). I cannot create or change it myself."
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


def _describe(meta: dict, instance_id: str) -> list:
    kept = bool(meta.get("locked", True))
    traits = meta.get("traits") if isinstance(meta.get("traits"), dict) else {}
    voice = meta.get("voice") if isinstance(meta.get("voice"), dict) else {}
    lines = [
        f"instance: {instance_id}",
        f"avatar: {'kept (permanent)' if kept else 'DRAFT — the owner has not kept it yet'}",
    ]
    if meta.get("seed_hex"):
        lines.append(f"seed: {meta['seed_hex']} ({meta.get('generator', '?')})")
    if traits:
        lines.append("traits: " + ", ".join(f"{k}={v}" for k, v in sorted(traits.items())))
    if voice:
        lines.append(
            "voice signature: pitch {p} · rate {r} · timbre {t} "
            "(engine-agnostic scalars, not a named voice)".format(
                p=voice.get("pitch", "?"), r=voice.get("rate", "?"),
                t=voice.get("timbre", "?")))
    return lines


def register_avatar_action(controller) -> None:
    """Register the read + attach `agent_avatar` action, gated
    AVATAR_TOOL_ENABLED (default OFF; ON under POLYROB_LOCAL)."""
    from core.config_policy import AutonomyConfig
    if not AutonomyConfig.avatar_tool_enabled():
        return

    class AgentAvatarAction(BaseModel):
        attach: bool = Field(
            default=False,
            description=("Also copy the face PNG into this session's workspace "
                         "and return its path, so it can be sent with "
                         "message(media_paths=[...]). Reading is free; only set "
                         "this when you actually intend to send it."))

    @controller.registry.action(
        "Look at your own identity: the instance you are, your frozen Mindprint "
        "face (traits, seed) and your voice signature. With attach=true it also "
        "places the face image in your workspace so you can send it with "
        "message(media_paths=[...]). Read-only — you cannot create, re-roll or "
        "change your identity; that is the owner's one-time setup.",
        param_model=AgentAvatarAction,
    )
    async def agent_avatar(params: AgentAvatarAction, execution_context=None) -> ActionResult:
        from core.instance import load_pfp_meta, pfp_path, resolve_instance_id

        instance_id = resolve_instance_id()
        try:
            home = _data_dir(controller)
            png = pfp_path(home, instance_id) if home else None
        except Exception as e:
            logger.debug("agent_avatar: could not resolve the identity home", exc_info=True)
            return ActionResult(
                extracted_content=(f"instance: {instance_id}\navatar: unavailable "
                                   f"({type(e).__name__}: {e})"),
                include_in_memory=True)

        if png is None or not png.is_file():
            return ActionResult(
                extracted_content=f"instance: {instance_id}\n{_NO_AVATAR}",
                include_in_memory=True)

        meta = load_pfp_meta(home, instance_id)
        if not isinstance(meta, dict):
            # The face is there but its record does not parse. Saying "kept"
            # would claim traits we cannot read; saying "not set up" would deny
            # a file that exists. Both are confident lies.
            lines = [f"instance: {instance_id}",
                     f"avatar: the image exists at {png} but its record "
                     f"(pfp.json) is unreadable, so I cannot report its traits"]
        else:
            lines = _describe(meta, instance_id)

        if params.attach:
            ws = _workspace(controller, execution_context)
            if not ws:
                lines.append("attach: no session workspace is available, so I "
                             "cannot place the image where a send can reach it")
            else:
                try:
                    dest = Path(ws) / WORKSPACE_NAME
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(png, dest)
                    lines.append(
                        f"attached: {WORKSPACE_NAME} is now in your workspace — "
                        f"send it with message(media_paths=[\"{WORKSPACE_NAME}\"])")
                except Exception as e:
                    logger.warning("agent_avatar: attach failed", exc_info=True)
                    lines.append(f"attach failed: {type(e).__name__}: {e}")

        return ActionResult(extracted_content="\n".join(lines), include_in_memory=True)
