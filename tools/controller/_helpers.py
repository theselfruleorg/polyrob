"""Shared Controller helpers (UP-11 god-file split).

Extracted from `tools/controller/service.py` so the focused Controller mixins
(execution / tool_management / introspection / action_registration) can import
these symbols WITHOUT importing `service.py` (which would be a circular import,
since service.py imports the mixins). `service.py` re-exports all four so the
established `from tools.controller.service import ToolInfo / make_denylist_hook /
build_load_skill_result` call sites (tests + callers) keep working unchanged.

Pure code-motion — bodies are verbatim.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict

from tools.controller.types import ActionResult


# Define observe function to replace the import
def observe(*args, **_kwargs):
	def decorator(func):
		return func
	if len(args) == 1 and callable(args[0]):
		return decorator(args[0])
	return decorator


@dataclass
class ToolInfo:
    """Information about a registered tool."""
    instance: Any
    actions: Dict[str, Callable]
    name: str


def make_denylist_hook(denied_action_names):
    """Build a pre_tool_call hook that denies the given action names.

    Returns a hook ``(action_name, params, context) -> Optional[str]`` suitable for
    ``Controller.register_pre_tool_call_hook``. Useful for allow/deny lists.
    """
    denied = set(denied_action_names or [])

    def _hook(action_name, params, context):
        if action_name in denied:
            return f"'{action_name}' is on the tool denylist"
        return None

    return _hook


def list_skill_resources(skill_dir, *, max_files: int = 50):
    """List a skill's on-disk resource files (Task 17), relative to ``skill_dir``.

    Excludes ``SKILL.md`` itself (that's the pinned body, not a "resource") and
    any ``.git`` internals. Read-only listing — never inspects file content.
    Fail-open: an unresolvable/missing dir returns ``[]``.
    """
    from pathlib import Path
    out = []
    try:
        base = Path(skill_dir).resolve()
    except OSError:
        return out
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*")):
        if len(out) >= max_files:
            break
        if not p.is_file() or p.name == "SKILL.md" or ".git" in p.parts:
            continue
        try:
            out.append(str(p.resolve().relative_to(base)))
        except (ValueError, OSError):
            continue
    return out


def read_skill_resource_confined(skill_dir, rel_path: str, *, max_bytes: int = 200_000):
    """Read a single skill resource file, realpath-confined to ``skill_dir`` (Task 17).

    Read-only — this NEVER executes a resource (e.g. a ``scripts/*.sh`` file),
    it only returns its text content, UP-06-wrapped as untrusted data (skill
    resources may originate from a third-party skill author). Rejects ``../``
    escapes, absolute paths, symlink escapes (via ``.resolve()`` + parents
    check), and oversize files. Returns ``(ok, content_or_error_message)``.
    """
    from pathlib import Path
    from core.security.untrusted_wrap import wrap_untrusted
    try:
        base = Path(skill_dir).resolve()
        target = (base / rel_path).resolve()
    except OSError as e:
        return False, f"cannot resolve resource: {e}"
    if target != base and base not in target.parents:
        return False, "resource path escapes skill directory"
    if not target.is_file():
        return False, "no such resource"
    try:
        if target.stat().st_size > max_bytes:
            return False, f"resource too large (>{max_bytes} bytes)"
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return False, f"read failed: {e}"
    return True, wrap_untrusted("skill_resource", content)


def build_session_search_hint(recalled: str, limit: int, sort: str = None) -> str:
    """T2.6 (review fix): pagination hint from the `(id N)` tags a with_ids=True
    provider result embeds (SqliteMemoryProvider/LocalVectorMemoryProvider). ''
    when no tags are present (a legacy provider/test-double, or the KB
    collection path, neither of which embed ids) or the page wasn't full —
    "full page" is a heuristic (returned-id count >= the [1,20]-clamped limit),
    not an exact "more rows exist" check.

    The `before_id=<smallest id>` suggestion is advertised ONLY for
    sort="newest" — the one lossless forward-pagination mode (rowid DESC, so
    `rowid < before_id` is exactly "continue where this page stopped"). Proven
    lossy for rank sort (the default): `before_id` narrows MATCH candidates
    BEFORE ranking, so a page1 of rank-ordered ids like [2, 4] would suggest
    before_id=2 and permanently strand any id (e.g. 3, 5) that ranked between
    them but below the returned page — a real match silently unreachable. Rank
    sort gets an honest "refine or switch to newest" nudge instead; "oldest"
    gets nothing (its filter runs the SAME direction as the ascending ORDER
    BY, so a "page further" suggestion would be actively misleading, not just
    lossy).
    """
    ids = [int(m) for m in re.findall(r"\(id (\d+)\)", recalled or "")]
    effective_limit = max(1, min(20, limit))
    if not ids or len(ids) < effective_limit:
        return ""
    if sort == "newest":
        return f"\nMore available: pass before_id={min(ids)} to page further."
    if sort is None:
        return "\nRefine the query or use sort='newest' with before_id to page chronologically."
    return ""  # sort == "oldest": no forward-pagination story exists


def build_load_skill_result(session_skills, skill_id, activated=None, skill_dir=None) -> ActionResult:
    """Resolve a load_skill(skill_id) call to an ActionResult (S-1).

    Pure so it is unit-testable without a live Controller. ``session_skills`` maps
    skill_id -> object with a ``.content`` attribute (the full SKILL.md body).
    Returns an error result for an unknown id, else the body wrapped in a
    cache-preserving <skill> block tagged with ``metadata.skill_loaded``.

    ``activated`` (Task 12 — activation dedup): an optional session-scoped
    ``set`` of skill_ids already delivered via this call. When ``skill_id`` is
    already in ``activated``, the full body is NOT re-emitted — a short
    "already active" ack is returned instead (``metadata.skill_already_active =
    True``), so a model that calls ``load_skill`` twice for the same id in one
    session doesn't burn tokens re-printing an unchanged body. On a fresh id the
    body is returned as before and the id is recorded into ``activated``
    (mutated in place, e.g. a Controller's ``self._activated_skills``) so the
    NEXT call for that id short-circuits. ``activated=None`` (the default)
    disables tracking entirely — existing callers that don't pass it keep the
    old always-return-the-body behavior, byte-identical.

    ``skill_dir`` (Task 17 — resource read-path): an optional on-disk directory
    for this skill (resolved by the caller, e.g. ``SkillManager.resolve_skill_dir``).
    When provided, ``metadata['skill_resources']`` is set to
    ``list_skill_resources(skill_dir)`` (``[]`` if the dir doesn't resolve to any
    files) so the model can see what it may subsequently read via
    ``read_skill_resource``. ``skill_dir=None`` (the default) leaves ``metadata``
    untouched — byte-identical to pre-Task-17 callers that don't pass it.
    """
    sid = (skill_id or "").strip().strip('"')
    session_skills = session_skills or {}
    skill = session_skills.get(sid)
    if skill is None:
        available = ", ".join(session_skills.keys()) or "(none)"
        return ActionResult(
            error=f"Unknown skill_id '{sid}'. Available in this session: {available}",
            include_in_memory=True,
        )
    if activated is not None and sid in activated:
        result = ActionResult(
            extracted_content=f"Skill '{sid}' is already active this session — no need to reload.",
            include_in_memory=True,
            metadata={'skill_already_active': True},
        )
        if skill_dir is not None:
            result.metadata['skill_resources'] = list_skill_resources(skill_dir)
        return result
    body = getattr(skill, 'content', '') or ''
    if activated is not None:
        activated.add(sid)
    result = ActionResult(
        extracted_content=f'<skill id="{sid}">\n{body}\n</skill>',
        include_in_memory=True,
        metadata={'skill_loaded': sid},
    )
    if skill_dir is not None:
        result.metadata['skill_resources'] = list_skill_resources(skill_dir)
    return result


def self_mod_emitter(execution_context, controller, user_id, *, kind, source,
                     item_id=None, created_by="agent"):
    """Build the fail-open ``_self_mod_ev`` closure used by the self-modifying
    actions (memory notes / skill_manage / self_context_manage / owner_doc_manage /
    preferences) to record a first-class ``self_modification`` event on the
    durable event log.

    ``item_id``/``created_by`` are the per-action defaults; the returned closure
    accepts per-call overrides. Mirrors the keyword surface of
    ``core.self_events.emit_self_modification`` exactly.
    NOTE: the closure is an internal telemetry helper, never a registered action,
    so the registry's first-param-annotation introspection does not apply here.
    """
    default_item_id = item_id
    default_created_by = created_by

    def _self_mod_ev(action, item_id=None, *, pending=None, ok=True, created_by=None):
        try:
            from core.self_events import emit_self_modification
            emit_self_modification(
                kind=kind, action=action,
                item_id=str((item_id if item_id is not None else default_item_id) or ""),
                user_id=user_id or "",
                session_id=(getattr(execution_context, 'session_id', None)
                            or getattr(controller, 'session_id', '') or ""),
                pending=pending,
                created_by=(created_by if created_by is not None else default_created_by),
                source=source, ok=ok)
        except Exception:
            pass

    return _self_mod_ev
