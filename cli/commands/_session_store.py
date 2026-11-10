"""The `polyrob session` verbs' one READ of the session tree and the chat rows.

113 W2 item 5 (T-04): `tail`/`history` read the ADMIN home (``session._data_root``)
while `list`/`show`/`cancel`/`pause`/`resume`/`costs`/`export` built a CLI container
and asked its SessionManager — the CLI home, which on a deployed box is not the
home the service writes. Every verb now reads the same tree, here.

The read is read-only on purpose. A SessionManager loaded from disk rewrites every
``running`` row as ``suspended`` (it assumes the process that ran it is gone), so a
`session list` built on one flipped a run that was live in another terminal or in
the service.

An unreadable store is not an empty one: a tree that cannot be listed raises, a
metadata file that cannot be parsed is named in ``errors``, and a chat store that
cannot be read is reported as ``chat_error`` — never as "no chat".
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import click

#: The statuses that count as live (``SessionManager.get_active_sessions``).
ACTIVE_STATUSES = frozenset({"created", "running", "resumed"})


def _metadata_paths(root: Path) -> List[Path]:
    """``<user>/<session>/metadata.json`` and the legacy ``<user>/sessions/<session>/``.
    Raises ``click.ClickException`` when the tree exists and cannot be listed."""
    out: List[Path] = []
    try:
        users = sorted(os.listdir(root))
    except FileNotFoundError:
        return out
    except OSError as exc:
        raise click.ClickException(f"could not read the session tree {root}: {exc}")
    for user in users:
        user_dir = root / user
        if not user_dir.is_dir():
            continue
        for parent in (user_dir, user_dir / "sessions"):
            try:
                children = sorted(os.listdir(parent))
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise click.ClickException(f"could not read {parent}: {exc}")
            for child in children:
                meta = parent / child / "metadata.json"
                if meta.is_file():
                    out.append(meta)
    return out


def read_sessions(root) -> Tuple[List[dict], List[str]]:
    """``(sessions, errors)``: every session's metadata under *root*, newest first.
    ``errors`` names each metadata file that could not be read."""
    seen, sessions, errors = set(), [], []
    for meta in _metadata_paths(Path(root)):
        try:
            info = json.loads(meta.read_text())
            if not isinstance(info, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as exc:
            errors.append(f"{meta}: {type(exc).__name__}")
            continue
        sid = info.get("id") or info.get("session_id") or meta.parent.name
        if sid in seen:
            continue
        seen.add(sid)
        sessions.append(info)
    sessions.sort(key=lambda s: str(s.get("updated_at") or s.get("created_at") or ""),
                  reverse=True)
    return sessions, errors


def read_session(root, session_id: str, *, exact: bool = False) -> Tuple[Optional[dict], Optional[Path]]:
    """``(metadata, directory)`` of one session (an id prefix finds it unless *exact*).
    ``(None, None)`` when there is none; ``(None, directory)`` when its metadata is
    missing. An unreadable metadata file raises."""
    from cli.session_paths import session_directory
    directory = session_directory(Path(root), session_id)
    if directory is None or (exact and directory.name != session_id):
        return None, None
    meta = directory / "metadata.json"
    if not meta.is_file():
        return None, directory
    try:
        info = json.loads(meta.read_text())
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"could not read {meta}: {type(exc).__name__}")
    return (info if isinstance(info, dict) else None), directory


def chat_rows(data_home, session_ids) -> Tuple[Dict[str, dict], Optional[str]]:
    """``({session_id: {"name", "seat", "key"}}, error)`` from the chat store of
    *data_home*. A home with no ``surfaces.db`` has no chats (no error); a store that
    does not open or answer is ``({}, reason)``."""
    from core.runtime_paths import surfaces_db_path
    path = surfaces_db_path(str(data_home))
    if not os.path.isfile(path):
        return {}, None
    try:
        from core.surfaces.chats import chat_name, seat_word
        from core.surfaces.session_chat_registry import SessionChatRegistry
        registry = SessionChatRegistry(path)
        out = {}
        for sid in session_ids:
            if not sid:
                continue
            row = registry.resolve_by_session_id(str(sid))
            if row:
                out[str(sid)] = {"name": chat_name(row), "seat": seat_word(row.get("surface_id")),
                                 "key": row.get("session_key")}
        return out, None
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def title(info: dict, chat: Optional[dict] = None) -> str:
    """One line a person can tell a chat by: its task's first line, else its chat's
    name, else "(no title)"."""
    task = str(info.get("task") or "").strip()
    if task:
        return task.splitlines()[0].strip()
    if chat and chat.get("name"):
        return f"{chat['name']} chat in {chat.get('seat') or 'a chat app'}"
    return "(no title)"
