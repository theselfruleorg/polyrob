"""Bounded chat summaries from existing session files, without a second index."""
import json
from datetime import datetime, timezone
from pathlib import Path


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
                result["unreadable"][user.name] = str(exc)
    except OSError as exc:
        result["unreadable"]["catalog"] = str(exc)
        return result
    candidates.sort(key=lambda item: item[:3], reverse=True)
    result["total"] = len(candidates)
    for created, owner, sid, folder in candidates[offset:offset+limit]:
        row = {"id": sid, "user": owner, "status": None, "task": None, "creator": None,
               "created": datetime.fromtimestamp(created, timezone.utc).strftime('%Y-%m-%d %H:%M'),
               "unreadable": {}}
        for name in ("task", "status"):
            try:
                data = json.loads((folder / f"{name}.json").read_text())
                if not isinstance(data, dict):
                    raise ValueError(name)
                if name == "task":
                    row["task"] = str(data.get("task") or "")[:240]
                    row["creator"] = data.get("creator") or data.get("created_by")
                else:
                    row["status"] = data.get("status")
            except (OSError, ValueError) as exc:
                row["unreadable"][name] = str(exc)
        result["sessions"].append(row)
    if offset + limit < len(candidates):
        result["next_offset"] = offset + limit
    return result
