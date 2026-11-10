"""The read half of the ``memory`` tool's notes: ``list`` and ``show``.

A failed read is named (``memory_read_error``), never "No notes yet" or "Note not
found", and never an exception for the generic error path.
"""
# No `from __future__ import annotations` here (the Controller action modules keep
# their annotations real).
from typing import Any, Callable, Optional

from agents.task.loop_types.controller_types import ActionResult


def fmt_day(ts: Any) -> str:
	"""A note timestamp as a local day, or "?"."""
	try:
		import time as _t
		return _t.strftime("%Y-%m-%d", _t.localtime(int(ts))) if ts else "?"
	except Exception:
		return "?"


def _read_failed(exc: BaseException) -> ActionResult:
	from agents.task.agent.core.memory_prefetch import memory_read_error
	return ActionResult(error=memory_read_error(exc, "Notes"), include_in_memory=False)


async def note_read_result(prov: Any, user_id: Optional[str], action: str,
                           note_id: Optional[int],
                           fmt_ts: Callable[[Any], str] = fmt_day) -> ActionResult:
	"""The ActionResult of ``memory(action='list'|'show')``."""
	from core.security.untrusted_wrap import wrap_untrusted
	if action == "list":
		try:
			notes = await prov.note_list(user_id)
			pending = await prov.note_list(user_id, status="pending")
		except Exception as e:
			return _read_failed(e)
		if not notes and not pending:
			return ActionResult(extracted_content="No notes yet.", include_in_memory=True)
		lines = []
		for n in notes:
			tag_s = f" [{', '.join(n['tags'])}]" if n["tags"] else ""
			title_s = n["title"] or "(untitled)"
			lines.append(f"#{n['id']} {title_s}{tag_s} ({fmt_ts(n['updated_ts'])}): "
			             f"{(n['content'] or '')[:120]}")
		body = "\n".join(lines)
		if pending:
			body += f"\n({len(pending)} pending note(s) awaiting owner review)"
		return ActionResult(
			extracted_content=f"## Your notes\n{wrap_untrusted('memory', body)}",
			include_in_memory=True)

	# show
	if note_id is None:
		return ActionResult(extracted_content="show requires note_id.", include_in_memory=True)
	try:
		note = await prov.note_get(user_id, note_id)
		backs = []
		if note is not None and note.get("title"):
			backs = await prov.note_backlinks(user_id, note["title"])
	except Exception as e:
		return _read_failed(e)
	if note is None:
		return ActionResult(extracted_content="Note not found.", include_in_memory=True)
	body = (f"#{note['id']} {note['title'] or '(untitled)'} "
	        f"[{', '.join(note['tags'])}] status={note['status']} "
	        f"({fmt_ts(note['created_ts'])})\n{note['content']}")
	if note["links"]:
		body += f"\nlinks: {', '.join('[[' + l + ']]' for l in note['links'])}"
	if backs:
		body += f"\nbacklinked from: {', '.join('#' + str(b['id']) + ' ' + (b['title'] or '') for b in backs)}"
	return ActionResult(
		extracted_content=f"## Note\n{wrap_untrusted('memory', body)}",
		include_in_memory=True)
