"""The agent-callable page screenshot (`browser_screenshot`).

The step loop already captures a screenshot for vision into the session's
history directory, but that directory is outside the workspace, and
`message(media_paths=...)` only delivers a file INSIDE the workspace. This
writes the PNG to ``<workspace>/screenshots/<name>.png`` through the confined
writer, so the path it returns is one the agent can attach.
"""
import base64
import os
import re
from datetime import datetime, timezone
from typing import Optional

from agents.task.loop_types.controller_types import ActionResult

#: The workspace subfolder a screenshot lands in.
SCREENSHOT_DIR = 'screenshots'
_UNSAFE = re.compile(r'[^A-Za-z0-9._-]+')


def screenshot_file_name(name: Optional[str]) -> str:
	"""A safe ``<stem>.png``: no folder, no dot-file, a timestamp when unnamed."""
	stem = os.path.basename(str(name or '').strip())
	if stem.lower().endswith('.png'):
		stem = stem[:-4]
	stem = _UNSAFE.sub('-', stem).strip('.-')[:80]
	if not stem:
		stem = 'page-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
	return f'{stem}.png'


def _workspace_dir(execution_context) -> Optional[str]:
	ws = getattr(execution_context, 'workspace_dir', None)
	if ws:
		return str(ws)
	session_id = getattr(execution_context, 'session_id', None)
	if not session_id:
		return None
	try:
		from agents.task.path import pm
		return str(pm().get_workspace_dir(pm().clean_session_id(session_id),
										  getattr(execution_context, 'user_id', None)))
	except Exception:
		return None


async def save_page_screenshot(browser_context, params, execution_context) -> ActionResult:
	workspace = _workspace_dir(execution_context)
	if not workspace:
		return ActionResult(error='Screenshot not saved: this run has no session workspace',
							include_in_memory=True)
	encoded = await browser_context.take_screenshot(full_page=bool(params.full_page))
	if not encoded:
		return ActionResult(error='Screenshot failed: the page returned no image',
							include_in_memory=True)
	from core.security.workspace_io import write_bytes
	rel = os.path.join(SCREENSHOT_DIR, screenshot_file_name(params.name))
	path = os.path.join(workspace, rel)
	write_bytes(path, workspace, base64.b64decode(encoded))
	return ActionResult(
		extracted_content=(f'📸 Saved a screenshot of the current page to {path} '
						   f'(workspace path {rel}). To show it, attach it: '
						   f'message(media_paths=["{path}"]), one call per file.'),
		include_in_memory=True)
