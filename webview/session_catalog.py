"""Bounded chat summaries from existing session files, without a second index."""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

#: The code a part that would not read carries. The exception goes to the log,
#: never to the client (070 W0.11).
UNREADABLE = "unreadable"


def _read_json(path: Path):
    """The dict in *path*; ``None`` when the file does not exist yet.

    A missing file is "not known yet", not a failed read. Any other failure
    raises, so the caller can name the part as unreadable.
    """
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise ValueError(path.name)
    return data


def session_head(folder, *, task_chars: int = 240) -> dict:
    """The first facts of one session folder: task (*task_chars*), status, creator.

    ``creator`` comes from ``task.json`` (``creator``/``created_by``), else from
    ``metadata.json`` (where ``agents/task/agent/session.py`` writes it). A
    missing file leaves its field ``None``; any other failure sets
    ``unreadable[name] = "unreadable"`` and logs the exception with the path.
    """
    folder = Path(folder)
    head = {"task": None, "status": None, "creator": None, "unreadable": {}}
    for name in ("task", "status", "metadata"):
        if name == "metadata" and head["creator"]:
            break
        path = folder / f"{name}.json"
        try:
            data = _read_json(path)
        except (OSError, ValueError) as exc:
            logger.warning("session catalog: could not read %s: %s", path, exc)
            head["unreadable"][name] = UNREADABLE
            continue
        if data is None:
            continue
        if name == "task":
            head["task"] = str(data.get("task") or "")[:task_chars]
            head["creator"] = data.get("creator") or data.get("created_by")
        elif name == "status":
            head["status"] = data.get("status")
        else:
            head["creator"] = data.get("creator") or None
    return head


def session_page(root, scope: str, user_id: str, *, offset=0, limit=50) -> dict:
    """Enumerate directories; hydrate ONLY this page, never scan step/feed files.

    This is a directory snapshot, not a durable cursor/index. Refresh starts at
    page zero; new directories during pagination may shift the next page.
    """
    result = {"sessions": [], "unreadable": {}, "next_offset": None, "total": None}
    if scope == "none":
        result["total"] = 0
        return result
    root = Path(root)
    candidates = []
    try:
        users = list(root.iterdir()) if scope == "all" else [root / user_id]
        if scope == "user" and user_id == "_anonymous_":
            users.append(root / "anonymous")
        for user in users:
            if user.is_symlink() or not user.is_dir():
                continue
            try:
                for folder in user.iterdir():
                    if folder.is_symlink() or not folder.is_dir() or not (folder / "feed").is_dir():
                        continue
                    stat = folder.stat()
                    candidates.append((getattr(stat, "st_birthtime", stat.st_mtime), user.name, folder.name, folder))
            except OSError as exc:
                logger.warning("session catalog: could not list %s: %s", user, exc)
                result["unreadable"][user.name] = UNREADABLE
    except OSError as exc:
        logger.warning("session catalog: could not list %s: %s", root, exc)
        result["unreadable"]["catalog"] = UNREADABLE
        return result
    candidates.sort(key=lambda item: item[:3], reverse=True)
    result["total"] = len(candidates)
    for created, owner, sid, folder in candidates[offset:offset+limit]:
        when = datetime.fromtimestamp(created, timezone.utc)
        row = {"id": sid, "user": owner, "created": when.strftime('%Y-%m-%d %H:%M'),
               "created_iso": when.isoformat()}
        row.update(session_head(folder))
        result["sessions"].append(row)
    if offset + limit < len(candidates):
        result["next_offset"] = offset + limit
    return result
