"""The browser tool's interaction actions: everything a person does on a page.

``Browser`` inherits this mixin, so each method registers as ``browser_<name>``
through ``BaseTool.get_actions``. The work itself is in
``tools/browser/interaction.py``; a method here resolves the element index,
calls the engine and turns the outcome into an ``ActionResult``.

No ``from __future__ import annotations``: the Registry introspects the
annotations of an action.
"""
from typing import Optional

from agents.task.loop_types.controller_types import ActionResult
from agents.task.loop_types.execution_context import ActionExecutionContext
from tools.base_tool import BaseTool
from tools.browser import interaction
from tools.browser.actions import (
	ClickAtAction, ClickElementAction, CloseTabAction, DialogAnswerAction, DragAndDropAction,
	ElementIndexAction, EvaluateScriptAction, InputTextAction, NoParamsAction, ScrollAction,
	SendKeysAction, SetCheckedAction, TypeTextAction, UploadFileAction, WaitAction,
)
from tools.browser.interaction import InteractionError

_NO_CONTEXT = 'Browser context not available'


def _coerce(model, params):
	if isinstance(params, model):
		return params
	from pydantic import TypeAdapter
	return TypeAdapter(model).validate_python(params)


def _ok(text: str, browser_context) -> ActionResult:
	return ActionResult(extracted_content=text + interaction.take_dialog_note(browser_context),
						include_in_memory=True)


def _failed(what: str, exc: Exception, browser_context=None) -> ActionResult:
	note = interaction.take_dialog_note(browser_context) if browser_context is not None else ''
	reason = str(exc).strip() or type(exc).__name__
	if len(reason) > 500:  # a Playwright error carries its whole call log
		reason = reason[:500] + '…'
	return ActionResult(error=f'{what} failed: {reason}{note}', include_in_memory=True)


def _brief(text: str, limit: int = 120) -> str:
	text = str(text)
	return text if len(text) <= limit else f'{text[:limit]}… ({len(text)} chars)'


async def _node(browser_context, index: int):
	session = await browser_context.get_session()
	node = session.cached_state.selector_map.get(index)
	if node is None:
		raise InteractionError(
			f'Element with index {index} does not exist - read the page state again; the indexes change when the page changes')
	return node


async def _handle(browser_context, index: int):
	handle = await browser_context.get_locate_element(await _node(browser_context, index))
	if handle is None:
		raise InteractionError(
			f'Element with index {index} is no longer on the page - the page changed; read the page state again')
	return handle


class BrowserInteractionActions:
	"""Click, type, keys, files, pointer, tabs, waiting, script and dialogs."""

	@BaseTool.action(
		'Click an element by its index from the page state. button="right" opens a context menu; double=true double-clicks.',
		param_model=ClickElementAction
	)
	async def click_element(self, params: ClickElementAction, execution_context: Optional[ActionExecutionContext]):
		"""Click an element by its index."""
		params = _coerce(ClickElementAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			session = await browser_context.get_session()
			element_node = await _node(browser_context, params.index)
			initial_pages = len(session.context.pages)

			download_path = await browser_context._click_element_node(
				element_node, button=params.button or 'left', click_count=2 if params.double else 1)
			if download_path:
				msg = f'💾 Downloaded file to {download_path}'
			else:
				label = (element_node.get_all_text_till_next_clickable_element(max_depth=2)
						 if hasattr(element_node, 'get_all_text_till_next_clickable_element') else '')
				msg = f'🖱️ Clicked element with index {params.index}: {label}'

			if len(session.context.pages) > initial_pages:
				msg += ' - New tab opened - switching to it'
				await browser_context.switch_to_tab(-1)
			return _ok(msg, browser_context)
		except Exception as e:
			return _failed('Click', e, browser_context)

	@BaseTool.action(
		'Click a point of the viewport by its pixel coordinates (a canvas, a map, a control with no index).',
		param_model=ClickAtAction
	)
	async def click_at(self, params: ClickAtAction, execution_context: Optional[ActionExecutionContext]):
		"""Click viewport coordinates."""
		params = _coerce(ClickAtAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			page = await browser_context.get_current_page()
			await page.mouse.click(params.x, params.y, button=params.button or 'left',
								   click_count=2 if params.double else 1)
			return _ok(f'🖱️ Clicked the point ({params.x}, {params.y})', browser_context)
		except Exception as e:
			return _failed('Click', e, browser_context)

	@BaseTool.action(
		'Move the pointer over an element by its index (opens a hover menu or a tooltip).',
		param_model=ElementIndexAction
	)
	async def hover_element(self, params: ElementIndexAction, execution_context: Optional[ActionExecutionContext]):
		"""Hover an element."""
		params = _coerce(ElementIndexAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			await interaction.hover(await _handle(browser_context, params.index))
			return _ok(f'🖱️ The pointer is over the element with index {params.index}', browser_context)
		except Exception as e:
			return _failed('Hover', e, browser_context)

	@BaseTool.action(
		'Put text into an element by its index: a text field, a text area, a date or picker field, or a rich-text editor. '
		'It replaces what the element holds (clear=false adds) and reports what the element holds after. submit=true presses Enter.',
		param_model=InputTextAction
	)
	async def input_text(self, params: InputTextAction, execution_context: Optional[ActionExecutionContext]):
		"""Input text into an element."""
		params = _coerce(InputTextAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			page = await browser_context.get_current_page()
			handle = await _handle(browser_context, params.index)
			held = await interaction.fill_element(page, handle, params.text, clear=params.clear is not False)
			if params.submit:
				await page.keyboard.press('Enter')
			return _ok(f'⌨️ Input {_brief(params.text)} into index {params.index}. {held}'
					   + (' Pressed Enter.' if params.submit else ''), browser_context)
		except Exception as e:
			return _failed('Input text', e, browser_context)

	@BaseTool.action(
		'Type text into the element that has the focus (click it first). Use it for an editor or a widget that '
		'browser_input_text cannot fill. A line break makes a new line. submit=true presses Enter after.',
		param_model=TypeTextAction
	)
	async def type_text(self, params: TypeTextAction, execution_context: Optional[ActionExecutionContext]):
		"""Type into the focused element."""
		params = _coerce(TypeTextAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			page = await browser_context.get_current_page()
			await interaction.type_text(page, params.text)
			if params.submit:
				await page.keyboard.press('Enter')
			return _ok(f'⌨️ Typed {_brief(params.text)} into the focused element'
					   + (' and pressed Enter' if params.submit else ''), browser_context)
		except Exception as e:
			return _failed('Type text', e, browser_context)

	@BaseTool.action(
		'Press keyboard keys: one key ("Enter", "Escape"), a shortcut ("Control+A") or a sequence ("Tab Tab Enter"). '
		'Text that is not a key is typed into the focused element.',
		param_model=SendKeysAction
	)
	async def send_keys(self, params: SendKeysAction, execution_context: Optional[ActionExecutionContext]):
		"""Send keyboard keys."""
		params = _coerce(SendKeysAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			page = await browser_context.get_current_page()
			chords = interaction.parse_keys(params.keys)
			if chords is None:
				await interaction.type_text(page, params.keys)
				return _ok(f'⌨️ Typed {_brief(params.keys)} into the focused element (it is text, not a key)',
						   browser_context)
			await interaction.press_keys(page, chords)
			return _ok(f'⌨️ Sent keys: {" ".join(chords)}', browser_context)
		except Exception as e:
			return _failed('Send keys', e, browser_context)

	@BaseTool.action(
		'Tick or clear a checkbox, a radio button or a switch by its index.',
		param_model=SetCheckedAction
	)
	async def set_checked(self, params: SetCheckedAction, execution_context: Optional[ActionExecutionContext]):
		"""Set a checkbox, radio or switch."""
		params = _coerce(SetCheckedAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			state = await interaction.set_checked(await _handle(browser_context, params.index), params.checked)
			return _ok(f'☑️ The element with index {params.index} is {"checked" if state else "not checked"}',
					   browser_context)
		except Exception as e:
			return _failed('Set checked', e, browser_context)

	@BaseTool.action(
		'Attach files from the workspace to a file field (an image, a document). Give the index of the file field, '
		'its upload button or its drop area; no index is needed when the page has one file field.',
		param_model=UploadFileAction
	)
	async def upload_file(self, params: UploadFileAction, execution_context: Optional[ActionExecutionContext]):
		"""Upload workspace files through a file input or a file chooser."""
		params = _coerce(UploadFileAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			from tools.browser.screenshot import _workspace_dir
			payloads = interaction.load_upload_payloads(params.paths, _workspace_dir(execution_context))
			page = await browser_context.get_current_page()
			handle = None if params.index is None else await _handle(browser_context, params.index)
			return _ok('📎 ' + await interaction.upload_files(page, handle, payloads), browser_context)
		except Exception as e:
			return _failed('Upload', e, browser_context)

	@BaseTool.action(
		'Drag one element onto another by their indexes (reorder a list, move a card, a slider).',
		param_model=DragAndDropAction
	)
	async def drag_and_drop(self, params: DragAndDropAction, execution_context: Optional[ActionExecutionContext]):
		"""Drag and drop between two elements."""
		params = _coerce(DragAndDropAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			page = await browser_context.get_current_page()
			source = await _handle(browser_context, params.from_index)
			target = await _handle(browser_context, params.to_index)
			await interaction.drag_and_drop(page, source, target)
			return _ok(f'✋ Dragged the element with index {params.from_index} onto index {params.to_index}',
					   browser_context)
		except Exception as e:
			return _failed('Drag and drop', e, browser_context)

	@BaseTool.action(
		'Scroll the page in the specified direction (up, down, left, right). With an element index it scrolls '
		'the scrollable area that holds that element.',
		param_model=ScrollAction
	)
	async def scroll(self, params: ScrollAction, execution_context: Optional[ActionExecutionContext]):
		"""Scroll the page or one scrollable area."""
		params = _coerce(ScrollAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			amount = params.amount or 500
			delta = {'up': (0, -amount), 'down': (0, amount),
					 'left': (-amount, 0), 'right': (amount, 0)}.get(str(params.direction).lower())
			if delta is None:
				return ActionResult(error=f'Invalid scroll direction: {params.direction} (up, down, left, right)',
									include_in_memory=True)
			arrow = {'up': '⬆️', 'down': '⬇️', 'left': '⬅️', 'right': '➡️'}[str(params.direction).lower()]
			if params.index is not None:
				handle = await _handle(browser_context, params.index)
				if not await interaction.scroll_element(handle, *delta):
					raise InteractionError(f'the element with index {params.index} is in no scrollable area; scroll the page')
				return _ok(f'{arrow} Scrolled the area of index {params.index} {params.direction} {amount}px',
						   browser_context)
			page = await browser_context.get_current_page()
			await page.evaluate('([x, y]) => window.scrollBy(x, y)', list(delta))
			return _ok(f'{arrow} Scrolled {params.direction} {amount}px', browser_context)
		except Exception as e:
			return _failed('Scroll', e, browser_context)

	@BaseTool.action(
		'Read one element by its index: its kind, state (disabled, read-only, checked), value and text. '
		'Use it to check a field after you fill it.',
		param_model=ElementIndexAction
	)
	async def read_element(self, params: ElementIndexAction, execution_context: Optional[ActionExecutionContext]):
		"""Describe one element."""
		params = _coerce(ElementIndexAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			info = await interaction.describe(await _handle(browser_context, params.index))
			kind = f"<{info['tag']}" + (f" type={info['type']}" if info['type'] else '') + '>'
			flags = [name for name, on in (('disabled', info['disabled']), ('read-only', info['readonly']),
										   ('rich-text', info['editable'] and not info['field'])) if on]
			if info['checked'] is not None:
				flags.append('checked' if info['checked'] else 'not checked')
			lines = [f'🔎 Element {params.index}: {kind}' + (f" ({', '.join(flags)})" if flags else '')]
			if info['label']:
				lines.append(f"label: {info['label']}")
			if info['files'] is not None:
				lines.append('files: ' + (', '.join(info['files']) or '(none)'))
			elif info['field']:
				lines.append('value: ' + interaction._shown(info))
			if info['text'] and not info['field']:
				lines.append('text: ' + _brief(info['text'], 2000))
			lines.append('html: ' + info['html'])
			return _ok('\n'.join(lines), browser_context)
		except Exception as e:
			return _failed('Read element', e, browser_context)

	@BaseTool.action(
		'Wait for a number of seconds, or until a text is on the page (seconds is then the limit). At most 30 seconds.',
		param_model=WaitAction
	)
	async def wait(self, params: WaitAction, execution_context: Optional[ActionExecutionContext]):
		"""Wait for time or for a text."""
		params = _coerce(WaitAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			page = await browser_context.get_current_page()
			return _ok('⏳ ' + await interaction.wait_for(page, seconds=params.seconds, text=params.text),
					   browser_context)
		except Exception as e:
			return _failed('Wait', e, browser_context)

	@BaseTool.action(
		'Go forward to the next page in the tab history.',
		param_model=NoParamsAction
	)
	async def go_forward(self, params: NoParamsAction, execution_context: Optional[ActionExecutionContext]):
		"""Navigate forward."""
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			page = await browser_context.get_current_page()
			if await page.go_forward(timeout=30000, wait_until='domcontentloaded') is None:
				return ActionResult(error='Go forward failed: there is no next page in this tab', include_in_memory=True)
			await self._wait_for_page_load(page)
			await browser_context._update_state()
			return _ok(f'🔜 Navigated forward to {page.url}', browser_context)
		except Exception as e:
			return _failed('Go forward', e, browser_context)

	@BaseTool.action(
		'Close a browser tab (default: the current tab) and continue in the last tab that stays open.',
		param_model=CloseTabAction
	)
	async def close_tab(self, params: CloseTabAction, execution_context: Optional[ActionExecutionContext]):
		"""Close a tab."""
		params = _coerce(CloseTabAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			import asyncio
			session = await browser_context.get_session()
			pages = session.context.pages
			if len(pages) <= 1:
				return ActionResult(error='Close tab failed: this is the only tab; navigate it to another URL',
									include_in_memory=True)
			current = await browser_context.get_current_page()
			if params.page_id is None:
				page = current
			elif 0 <= params.page_id < len(pages):
				page = pages[params.page_id]
			else:
				return ActionResult(error=f'Tab index {params.page_id} out of range (0-{len(pages) - 1})',
									include_in_memory=True)
			url = page.url
			await asyncio.wait_for(page.close(), 10)
			if page is current:
				session.current_page = session.context.pages[-1]
				await session.current_page.bring_to_front()
			await browser_context._update_state()
			return _ok(f'🗙 Closed the tab {url}. The current tab is {session.current_page.url}', browser_context)
		except Exception as e:
			return _failed('Close tab', e, browser_context)

	@BaseTool.action(
		'Run JavaScript in the current page and return its result (an expression or an arrow function). '
		'Use it to read data the page state does not show, or to drive a widget no other action reaches.',
		param_model=EvaluateScriptAction
	)
	async def evaluate_js(self, params: EvaluateScriptAction, execution_context: Optional[ActionExecutionContext]):
		"""Evaluate a script in the page."""
		params = _coerce(EvaluateScriptAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		try:
			page = await browser_context.get_current_page()
			return _ok('🧪 Result: ' + await interaction.evaluate_script(page, params.script), browser_context)
		except Exception as e:
			return _failed('Script', e, browser_context)

	@BaseTool.action(
		'Set the answer to the NEXT alert, confirm or prompt dialog of the page: accept=false cancels it, '
		'prompt_text fills a prompt. With no call a dialog is accepted. Call it before the click that opens the dialog.',
		param_model=DialogAnswerAction
	)
	async def handle_dialog(self, params: DialogAnswerAction, execution_context: Optional[ActionExecutionContext]):
		"""Plan the answer to the next dialog."""
		params = _coerce(DialogAnswerAction, params)
		browser_context = execution_context.browser_context
		if not browser_context:
			return ActionResult(error=_NO_CONTEXT, include_in_memory=True)
		interaction.set_dialog_answer(browser_context, params.accept, params.prompt_text)
		answer = 'accepted' if params.accept else 'dismissed'
		if params.accept and params.prompt_text is not None:
			answer += f' with the text {_brief(params.prompt_text)!r}'
		return _ok(f'💬 The next dialog will be {answer}', browser_context)
