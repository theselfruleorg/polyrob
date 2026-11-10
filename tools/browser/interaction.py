"""The page-interaction engine of the browser tool.

Every function takes a Playwright page or element handle and does ONE user
operation: fill a field, type, press keys, attach a file, hover, drag, tick a
box, answer a dialog. The agent-facing actions are in
``tools/browser/interaction_actions.py``; they resolve an index to a handle and
call in here.

Three rules hold for all of it:

- **Bounded.** Each Playwright call carries ``ACTION_TIMEOUT_MS``. A read-only
  date field once held a ``fill`` for its 30 s default four times in a row.
- **Honest.** A write reads the element back and reports what it holds. A
  script that sets ``el.value`` on a ``<p>`` raises nothing and changes nothing.
- **No text in script source.** Text reaches the page as an argument, never
  interpolated into JavaScript.
"""
import asyncio
import json
import mimetypes
import os
from typing import Any, List, Optional

ACTION_TIMEOUT_MS = 5000
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_UPLOAD_FILES = 10
MAX_WAIT_SECONDS = 30
EVALUATE_TIMEOUT_SECONDS = 15
MAX_EVALUATE_CHARS = 8000
#: Text up to this size is typed key by key (real key events, which an
#: autocomplete needs); longer text is inserted in one input event per line.
_TYPE_KEYS_MAX_CHARS = 300


class InteractionError(Exception):
	"""A named reason the agent can act on; its text is the action error."""


_DESCRIBE_JS = """el => {
	const tag = el.tagName.toLowerCase();
	const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : '')).toLowerCase();
	const field = tag === 'input' || tag === 'textarea' || tag === 'select';
	const aria = el.getAttribute('aria-checked');
	const box = type === 'checkbox' || type === 'radio';
	return {
		tag, type, field,
		editable: !!el.isContentEditable,
		readonly: !!el.readOnly,
		disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
		checked: box ? !!el.checked : (aria === null ? null : aria === 'true'),
		value: field ? String(el.value == null ? '' : el.value) : null,
		text: (el.innerText || el.textContent || '').trim().slice(0, 4000),
		files: (tag === 'input' && type === 'file') ? Array.from(el.files || []).map(f => f.name) : null,
		multiple: !!el.multiple,
		label: (el.getAttribute('aria-label') || el.getAttribute('placeholder')
			|| el.getAttribute('name') || el.id || '').slice(0, 120),
		html: el.outerHTML.slice(0, 500),
	};
}"""

# The native setter, so a framework that wraps `value` (React) sees the change.
# A flatpickr field first: the widget keeps its own date, and the visible field of
# its alt-input mode is a sibling of the one that holds the instance.
_SET_VALUE_JS = """(el, value) => {
	const before = el.previousElementSibling;
	const picker = el._flatpickr || (before && before._flatpickr);
	if (picker && typeof picker.setDate === 'function') {
		picker.setDate(value, true);
		if (String(el.value)) return String(el.value);
	}
	const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
	const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
	el.focus();
	setter.call(el, value);
	el.dispatchEvent(new Event('input', { bubbles: true }));
	el.dispatchEvent(new Event('change', { bubbles: true }));
	el.blur();
	return String(el.value);
}"""

_EDITABLE_ROOT_JS = """el => {
	let root = el;
	while (root.parentElement && root.parentElement.isContentEditable) root = root.parentElement;
	return root;
}"""

_HAS_FOCUS_JS = "el => el === document.activeElement || el.contains(document.activeElement)"

_INNER_FIELD_SELECTOR = (
	'input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio]), '
	'textarea, [contenteditable=""], [contenteditable="true"], [contenteditable="plaintext-only"]')

# The file input an upload control stands for: the element, one inside it, the
# control of its <label>, or the only one near it (a styled button hides it).
_FIND_FILE_INPUT_JS = """el => {
	const isFile = n => n && n.tagName === 'INPUT' && (n.getAttribute('type') || '').toLowerCase() === 'file';
	if (isFile(el)) return el;
	const inner = el.querySelector('input[type=file]');
	if (inner) return inner;
	const label = el.closest('label');
	if (label) {
		if (isFile(label.control)) return label.control;
		const inLabel = label.querySelector('input[type=file]');
		if (inLabel) return inLabel;
	}
	// Never as far as <body>: the only file field of a page is not "near" every element.
	let node = el.parentElement;
	for (let depth = 0; node && node !== document.body && depth < 4; depth++, node = node.parentElement) {
		const found = node.querySelectorAll('input[type=file]');
		if (found.length === 1) return found[0];
		if (found.length > 1 || node.tagName === 'FORM') return null;
	}
	return null;
}"""

_SCROLL_ELEMENT_JS = """(el, delta) => {
	let node = el;
	while (node && node !== document.body && node !== document.documentElement) {
		const style = getComputedStyle(node);
		const y = /(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight;
		const x = /(auto|scroll)/.test(style.overflowX) && node.scrollWidth > node.clientWidth;
		if ((delta.y && y) || (delta.x && x)) { node.scrollBy(delta.x, delta.y); return true; }
		node = node.parentElement;
	}
	return false;
}"""


async def describe(handle) -> dict:
	"""What the element is and what it holds, in one round trip."""
	return await handle.evaluate(_DESCRIBE_JS)


def _shown(info: dict) -> str:
	"""The value a write left behind, as the agent may read it."""
	if info.get('type') == 'password':
		return f"(hidden, {len(info.get('value') or '')} chars)"
	held = info['value'] if info.get('field') else info.get('text', '')
	held = held or ''
	return repr(held if len(held) <= 200 else held[:200] + f'… ({len(held)} chars)')


# ---------------------------------------------------------------- keys

_NAMED_KEYS = [
	'Enter', 'Tab', 'Escape', 'Backspace', 'Delete', 'Insert', 'Home', 'End', 'PageUp',
	'PageDown', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Space', 'CapsLock',
	'Shift', 'Control', 'Alt', 'Meta', 'ControlOrMeta',
] + [f'F{n}' for n in range(1, 13)]
_KEY_NAMES = {name.lower(): name for name in _NAMED_KEYS}
_KEY_NAMES.update({
	'esc': 'Escape', 'return': 'Enter', 'ctrl': 'Control', 'cmd': 'Meta', 'command': 'Meta',
	'option': 'Alt', 'del': 'Delete', 'up': 'ArrowUp', 'down': 'ArrowDown', 'left': 'ArrowLeft',
	'right': 'ArrowRight', 'pgup': 'PageUp', 'pgdn': 'PageDown', 'spacebar': 'Space',
})


def _chord(token: str) -> Optional[str]:
	"""``ctrl+a`` -> ``Control+a``; None when *token* is not a key or a chord."""
	if token == '+':
		return token
	parts = token.split('+')
	out = []
	for part in parts:
		named = _KEY_NAMES.get(part.lower())
		if named:
			out.append(named)
		elif len(part) == 1:
			out.append(part)
		else:
			return None
	return '+'.join(out)


def parse_keys(keys: str) -> Optional[List[str]]:
	"""The key presses *keys* names, or None when it is text to type.

	One token may be a single character (``a``). In a sequence (``Tab Tab Enter``)
	every token must be a named key or a chord, so a sentence is never pressed
	letter by letter with its spaces lost."""
	tokens = str(keys or '').split()
	if not tokens:
		return None
	chords = [_chord(token) for token in tokens]
	if any(chord is None for chord in chords):
		return None
	if len(tokens) > 1 and any(len(token) == 1 for token in tokens):
		return None
	return chords


async def press_keys(page, chords: List[str]) -> None:
	for chord in chords:
		await page.keyboard.press(chord)


async def type_text(page, text: str) -> None:
	"""Type *text* into whatever holds the focus. A line break is Shift+Enter:
	a new line in a text area and in a rich-text editor, and never the Enter
	that sends a chat composer."""
	lines = str(text).replace('\r\n', '\n').replace('\r', '\n').split('\n')
	by_key = len(text) <= _TYPE_KEYS_MAX_CHARS
	for number, line in enumerate(lines):
		if number:
			await page.keyboard.press('Shift+Enter')
		if not line:
			continue
		if by_key:
			await page.keyboard.type(line)
		else:
			await page.keyboard.insert_text(line)


# ---------------------------------------------------------------- text input

#: What a typed <input> accepts; any other text leaves it empty.
_FIELD_FORMATS = {
	'date': 'YYYY-MM-DD', 'time': 'HH:MM', 'datetime-local': 'YYYY-MM-DDTHH:MM',
	'month': 'YYYY-MM', 'week': 'YYYY-Www', 'number': 'a number', 'color': '#rrggbb',
}


async def _editable_root(handle):
	root = (await handle.evaluate_handle(_EDITABLE_ROOT_JS)).as_element()
	return root or handle


async def _focus(handle) -> None:
	"""Give the element the focus: by script first (no pointer side effect),
	by a click when the page moved the focus elsewhere or refused it."""
	try:
		await handle.focus()
		if await handle.evaluate(_HAS_FOCUS_JS):
			return
	except Exception:
		pass
	await handle.click(timeout=ACTION_TIMEOUT_MS)


async def _fill_editable(page, handle, text: str, clear: bool):
	root = await _editable_root(handle)
	try:
		await root.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
	except Exception:
		pass
	await _focus(root)
	if clear:
		await page.keyboard.press('ControlOrMeta+A')
		await page.keyboard.press('Backspace')
	else:
		await page.keyboard.press('ControlOrMeta+End')
	await type_text(page, text)
	return root


async def _fill_field(handle, text: str, info: dict, clear: bool) -> str:
	"""Returns a note for the result ('' when the plain path worked)."""
	value = text if clear else (info.get('value') or '') + text
	refusal = None
	if not info['readonly']:
		try:
			await handle.fill(value, timeout=ACTION_TIMEOUT_MS)
			return ''
		except Exception as exc:
			refusal = str(exc).splitlines()[0][:160]
	await handle.evaluate(_SET_VALUE_JS, value)
	if info['readonly']:
		return (' The field is read-only (a picker widget owns it), so the value was set by'
				' script: check that the page shows it, or use the widget.')
	return f' The field refused typed input ({refusal}); the value was set by script.'


async def fill_element(page, handle, text: str, *, clear: bool = True) -> str:
	"""Put *text* into the element at *handle*; returns what it now holds.

	Covers a plain field, a read-only picker field, a rich-text editor (any
	element inside a ``contenteditable`` region, in an iframe or not) and a
	wrapper around one of those."""
	info = await describe(handle)
	tag, kind = info['tag'], info['type']
	if tag == 'select':
		raise InteractionError('this element is a dropdown: use browser_select_dropdown_option')
	if tag == 'input' and kind == 'file':
		raise InteractionError('this element is a file field: use browser_upload_file')
	if tag == 'input' and kind in ('checkbox', 'radio'):
		raise InteractionError(f'this element is a {kind}: use browser_set_checked')
	if info['disabled']:
		raise InteractionError('this element is disabled; the page does not accept input in it now')

	note = ''
	target = handle
	if tag in ('input', 'textarea'):
		note = await _fill_field(handle, text, info, clear)
	elif info['editable']:
		target = await _fill_editable(page, handle, text, clear)
	else:
		inner = await handle.query_selector(_INNER_FIELD_SELECTOR)
		if inner is not None:
			return await fill_element(page, inner, text, clear=clear)
		raise InteractionError(
			f'this element is a <{tag}> that holds no text field. Click it, then type with'
			' browser_type_text (the text goes to the element that has the focus)')

	after = await describe(target)
	held = after['value'] if after.get('field') else after.get('text', '')
	if text.strip() and not (held or '').strip():
		wanted = _FIELD_FORMATS.get(kind) if tag == 'input' else None
		if wanted:
			raise InteractionError(f'the {kind} field did not accept {text!r}: it takes {wanted}')
		raise InteractionError('the element is still empty after the input; the page did not accept it.'
							   ' Click the element, then use browser_type_text')
	return f'The element now holds {_shown(after)}.{note}'


# ---------------------------------------------------------------- files

def load_upload_payloads(paths: List[str], workspace_dir: Optional[str]) -> List[dict]:
	"""Read the workspace files to attach. The bytes travel in the call, so a
	browser in another process or on another host receives them too."""
	from core.security import workspace_io
	from core.surfaces.attachments import validate_media_paths

	if not paths:
		raise InteractionError('give at least one file path')
	if len(paths) > MAX_UPLOAD_FILES:
		raise InteractionError(f'at most {MAX_UPLOAD_FILES} files in one upload')
	validated, error = validate_media_paths(list(paths), workspace_dir)
	if error:
		raise InteractionError(f'upload refused: {error}')
	payloads = []
	for path in validated:
		try:
			data = workspace_io.read_bytes(path, workspace_dir, max_bytes=MAX_UPLOAD_BYTES + 1)
		except FileNotFoundError:
			raise InteractionError(f'upload refused: no such file in the workspace: {path}')
		except Exception as exc:
			raise InteractionError(f'upload refused: could not read {path} ({exc})')
		if len(data) > MAX_UPLOAD_BYTES:
			raise InteractionError(f'upload refused: {os.path.basename(path)} is above the 32 MiB file limit')
		payloads.append({
			'name': os.path.basename(path),
			'mimeType': mimetypes.guess_type(path)[0] or 'application/octet-stream',
			'buffer': data,
		})
	return payloads


async def upload_files(page, handle, payloads: List[dict]) -> str:
	"""Attach *payloads* through the file field *handle* stands for (or the
	page's only file field when *handle* is None). Returns what the field holds."""
	names = ', '.join(p['name'] for p in payloads)
	target = None
	if handle is not None:
		target = (await handle.evaluate_handle(_FIND_FILE_INPUT_JS)).as_element()
	else:
		fields = await page.query_selector_all('input[type=file]')
		if len(fields) == 1:
			target = fields[0]
		elif not fields:
			raise InteractionError('the page has no file field. Give the index of the upload button or drop area')
		else:
			raise InteractionError(f'the page has {len(fields)} file fields. Give the index of the'
								   ' upload button or drop area of the one you want')

	if target is not None:
		info = await describe(target)
		if len(payloads) > 1 and not info['multiple']:
			raise InteractionError('this file field takes one file; upload one file')
		await target.set_input_files(payloads, timeout=ACTION_TIMEOUT_MS)
		held = (await describe(target)).get('files') or []
		if not held:
			raise InteractionError('the file field is empty after the upload; the page did not accept the file')
		return f'Attached {", ".join(held)}.'

	# No file input in the page yet: the control opens the browser's file chooser.
	try:
		async with page.expect_file_chooser(timeout=ACTION_TIMEOUT_MS) as waiting:
			await handle.click(timeout=ACTION_TIMEOUT_MS)
		chooser = await waiting.value
	except Exception:
		raise InteractionError('this element has no file field and its click opened no file chooser.'
							   ' Give the index of the upload button or drop area, or give no index'
							   ' when the page has one file field')
	if len(payloads) > 1 and not chooser.is_multiple():
		raise InteractionError('this file chooser takes one file; upload one file')
	await chooser.set_files(payloads, timeout=ACTION_TIMEOUT_MS)
	return f'Gave {names} to the file chooser. Check that the page shows the file.'


# ---------------------------------------------------------------- pointer

async def hover(handle) -> None:
	await handle.hover(timeout=ACTION_TIMEOUT_MS)


async def _centre(handle) -> tuple:
	await handle.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
	box = await handle.bounding_box()
	if not box:
		raise InteractionError('the element has no position on the page (it is not shown)')
	return box['x'] + box['width'] / 2, box['y'] + box['height'] / 2


async def drag_and_drop(page, source, target) -> None:
	"""A real pointer drag (down, move in steps, up): the shape both HTML5
	drag-and-drop and a pointer-event sortable list react to."""
	start = await _centre(source)
	await page.mouse.move(*start)
	await page.mouse.down()
	try:
		end = await _centre(target)
		await page.mouse.move(*end, steps=12)
	finally:
		await page.mouse.up()


async def set_checked(handle, checked: bool) -> bool:
	"""Tick or clear a checkbox, radio or switch; returns the state it has after."""
	info = await describe(handle)
	if info['checked'] is None:
		inner = await handle.query_selector('input[type=checkbox], input[type=radio], [role=checkbox], [role=switch], [role=radio]')
		if inner is None:
			raise InteractionError('this element is not a checkbox, a radio or a switch; click it')
		handle, info = inner, await describe(inner)
	if info['checked'] != checked:
		try:
			await handle.click(timeout=ACTION_TIMEOUT_MS)
		except Exception:
			# A styled box hides the real input behind its label.
			await handle.evaluate('el => el.click()')
		info = await describe(handle)
	if info['checked'] != checked:
		raise InteractionError(f'the element is still {"" if info["checked"] else "not "}checked after the click')
	return bool(info['checked'])


async def scroll_element(handle, dx: int, dy: int) -> bool:
	"""Scroll the nearest scrollable container of the element; False when it has none."""
	return bool(await handle.evaluate(_SCROLL_ELEMENT_JS, {'x': dx, 'y': dy}))


# ---------------------------------------------------------------- waiting, script

async def wait_for(page, *, seconds: Optional[float] = None, text: Optional[str] = None) -> str:
	limit = min(float(seconds) if seconds is not None else (10.0 if text else 2.0), MAX_WAIT_SECONDS)
	limit = max(limit, 0.0)
	if not text:
		await asyncio.sleep(limit)
		return f'Waited {limit:g} s.'
	try:
		await page.get_by_text(text, exact=False).first.wait_for(state='visible', timeout=limit * 1000)
	except Exception:
		raise InteractionError(f'the text {text!r} did not show within {limit:g} s')
	return f'The text {text!r} is on the page.'


async def evaluate_script(page, script: str) -> str:
	"""Run *script* in the page and return its result as bounded JSON."""
	try:
		value: Any = await asyncio.wait_for(page.evaluate(script), EVALUATE_TIMEOUT_SECONDS)
	except asyncio.TimeoutError:
		raise InteractionError(f'the script did not return within {EVALUATE_TIMEOUT_SECONDS} s')
	try:
		out = json.dumps(value, ensure_ascii=False, default=str)
	except Exception:
		out = str(value)
	if len(out) > MAX_EVALUATE_CHARS:
		out = out[:MAX_EVALUATE_CHARS] + f'… ({len(out)} chars in all; return less)'
	return out


# ---------------------------------------------------------------- dialogs

def attach_dialog_handler(playwright_context, owner) -> None:
	"""Answer every alert, confirm and prompt and keep a note of it on *owner*.

	With no handler Playwright dismisses a dialog silently: a "Delete? OK /
	Cancel" then always cancels and the agent never learns why nothing happened.
	The default answer is accept; ``set_dialog_answer`` changes the next one."""
	owner._dialog_notes = []
	owner._dialog_answer = None

	async def on_dialog(dialog):
		answer, owner._dialog_answer = owner._dialog_answer, None
		accept = True if answer is None else bool(answer.get('accept', True))
		prompt_text = (answer or {}).get('prompt_text')
		note = f'{dialog.type} "{(dialog.message or "")[:300]}" was {"accepted" if accept else "dismissed"}'
		try:
			if accept:
				await (dialog.accept(prompt_text) if prompt_text is not None else dialog.accept())
			else:
				await dialog.dismiss()
		except Exception as exc:
			note += f' (the answer failed: {exc})'
		owner._dialog_notes.append(note)
		del owner._dialog_notes[:-5]

	playwright_context.on('dialog', on_dialog)


def set_dialog_answer(owner, accept: bool, prompt_text: Optional[str]) -> None:
	owner._dialog_answer = {'accept': accept, 'prompt_text': prompt_text}


def take_dialog_note(owner) -> str:
	"""The dialogs since the last call, as a sentence for an action result ('' if none)."""
	notes = getattr(owner, '_dialog_notes', None)
	if not isinstance(notes, list) or not notes:
		return ''
	text = ' The page opened a dialog: ' + '; '.join(notes) + '.'
	notes.clear()
	return text
