"""Stage explicit local attachments through the shared chat-media rail."""
from pathlib import Path


async def stage_attachments(paths, workspace, text=""):
    from core.surfaces.inbound_attachments import (
        inbound_media_enabled, inbound_media_max_files, inbound_media_max_mb,
        persist_inbound_file, inject_file_content,
    )
    if not inbound_media_enabled():
        raise ValueError("Inbound attachments are disabled in this profile.")
    if len(paths) > inbound_media_max_files():
        raise ValueError(f"At most {inbound_media_max_files()} attachments are allowed per turn.")
    images, receipts = [], []
    cap = int(inbound_media_max_mb() * 1024 * 1024)
    for value in paths:
        path = Path(value).expanduser()
        if not path.is_file():
            raise ValueError(f"Attachment is not a readable file: {path}")
        with path.open("rb") as handle:
            data = handle.read(cap + 1)
        relative, reason = persist_inbound_file(str(workspace), path.name, data, kind="file")
        if relative is None:
            raise ValueError(f"Attachment {path.name}: {reason}")
        text, blocks = inject_file_content(str(workspace), relative, text)
        images.extend(blocks or [])
        receipts.append(str(Path(workspace) / relative))
    return text, images, receipts


async def attach_to_session(orchestrator, session_id, user_id, paths):
    from agents.task.path import pm
    workspace = pm().get_workspace_dir(session_id, user_id)
    text, images, receipts = await stage_attachments(paths, workspace)
    await orchestrator.submit_user_message(
        None, text, kind="comment", metadata={"image_attachments": images}
    )
    return receipts
