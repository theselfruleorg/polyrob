"""The browser interaction ACTIONS through the real stack: the DOM service
indexes a page, the agent names an index, the action drives the element.

This is the path of the prod run the engine tests come from: a form with a
title field, a rich-text description, a date picker and a cover-image upload.
The tests skip when no Chromium can start.
"""
import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio

from tools.browser.actions import (
	ClickElementAction, DialogAnswerAction, ElementIndexAction, EvaluateScriptAction,
	InputTextAction, SendKeysAction, TypeTextAction, UploadFileAction,
)
from tools.browser.browser import Browser
from tools.browser.context import BrowserContext, BrowserContextConfig

FORM = """
<html><body>
<form onsubmit="event.preventDefault(); document.getElementById('sent').textContent = 'sent';">
<input id="title" aria-label="Title">
<div id="editor" contenteditable="true" aria-label="Description"><p>Describe it</p></div>
<input id="date" aria-label="Date" readonly>
<label id="cover-label">Cover image
  <button id="cover-button" type="button">Upload a cover</button>
  <input id="cover" type="file" style="display:none"></label>
<iframe id="frame" srcdoc="<body contenteditable='true'><p id='framed'>in the frame</p></body>"></iframe>
<button id="remove" type="button"
  onclick="document.getElementById('sent').textContent = confirm('Remove the draft?') ? 'removed' : 'kept'">Remove</button>
<button id="submit" type="submit">Submit</button>
</form>
<div id="sent"></div>
</body></html>
"""


class _Tool(Browser):
	"""The action methods, with no tool lifecycle around them."""

	def __init__(self):
		self.logger = logging.getLogger('test.browser')


@pytest_asyncio.fixture
async def rig(tmp_path):
	try:
		from playwright.async_api import async_playwright
	except ImportError:
		pytest.skip('playwright is not installed')
	manager = async_playwright()
	playwright = await manager.start()
	launched = None
	for options in ({}, {'channel': 'chromium'}):
		try:
			launched = await playwright.chromium.launch(**options)
			break
		except Exception as exc:
			reason = str(exc).splitlines()[0]
	if launched is None:
		await manager.__aexit__(None, None, None)
		pytest.skip(f'no Chromium to launch: {reason}')

	async def get_playwright_browser():
		return launched

	owner = SimpleNamespace(get_playwright_browser=get_playwright_browser,
							browser_config=SimpleNamespace(chrome_instance_path=None))
	context = BrowserContext(config=BrowserContextConfig(highlight_elements=False), browser=owner)
	page = await context.get_current_page()
	await page.set_content(FORM)
	workspace = tmp_path / 'ws'
	workspace.mkdir()
	(workspace / 'cover.png').write_bytes(b'\x89PNG cover')
	execution = SimpleNamespace(browser_context=context, workspace_dir=str(workspace),
								session_id=None, user_id=None)

	async def index_of(element_id):
		state = await context.get_state(capture_screenshot=False)
		for index, node in state.selector_map.items():
			if node.attributes.get('id') == element_id:
				return index
		raise AssertionError(f'#{element_id} is not in the page state: '
							 f'{[n.attributes.get("id") or n.tag_name for n in state.selector_map.values()]}')

	try:
		yield SimpleNamespace(tool=_Tool(), context=context, page=page, execution=execution, index_of=index_of)
	finally:
		await context.close()
		await launched.close()
		await manager.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_the_form_of_the_prod_run_is_filled_in_full(rig):
	tool, page, ctx = rig.tool, rig.page, rig.execution

	res = await tool.input_text(InputTextAction(index=await rig.index_of('title'), text='A mint'), ctx)
	assert res.error is None and "'A mint'" in res.extracted_content

	description = 'Line one with "quotes".\n\nLine two.'
	res = await tool.input_text(InputTextAction(index=await rig.index_of('editor'), text=description), ctx)
	assert res.error is None, res.error
	held = await page.inner_text('#editor')
	assert 'Line one with "quotes".' in held and 'Line two.' in held and 'Describe it' not in held

	res = await tool.input_text(InputTextAction(index=await rig.index_of('date'), text='2026-01-15'), ctx)
	assert res.error is None and 'read-only' in res.extracted_content
	assert await page.input_value('#date') == '2026-01-15'

	res = await tool.upload_file(UploadFileAction(index=await rig.index_of('cover-button'), paths=['cover.png']), ctx)
	assert res.error is None, res.error
	assert 'cover.png' in res.extracted_content
	assert await page.evaluate("document.getElementById('cover').files[0].name") == 'cover.png'


@pytest.mark.asyncio
async def test_an_editor_inside_an_iframe_is_in_the_page_state_and_takes_text(rig):
	"""The classic editor shape: an iframe whose <body> is the editable region."""
	index = await rig.index_of('framed')
	res = await rig.tool.input_text(InputTextAction(index=index, text='typed in the frame'), rig.execution)
	assert res.error is None, res.error
	assert 'typed in the frame' in await rig.page.frame_locator('#frame').locator('body').inner_text()


@pytest.mark.asyncio
async def test_upload_needs_no_index_when_the_page_has_one_file_field(rig):
	res = await rig.tool.upload_file(UploadFileAction(paths='cover.png'), rig.execution)
	assert res.error is None, res.error
	assert await rig.page.evaluate("document.getElementById('cover').files.length") == 1


@pytest.mark.asyncio
async def test_upload_of_a_file_outside_the_workspace_is_refused(rig, tmp_path):
	(tmp_path / 'outside.env').write_text('KEY=1')
	res = await rig.tool.upload_file(UploadFileAction(paths=[str(tmp_path / 'outside.env')]), rig.execution)
	assert res.error and 'upload refused' in res.error
	assert await rig.page.evaluate("document.getElementById('cover').files.length") == 0


@pytest.mark.asyncio
async def test_text_sent_as_keys_is_typed_and_a_key_is_pressed(rig):
	tool, page, ctx = rig.tool, rig.page, rig.execution
	await tool.click_element(ClickElementAction(index=await rig.index_of('title')), ctx)
	res = await tool.send_keys(SendKeysAction(keys='A sentence, not a key.'), ctx)
	assert res.error is None and 'text, not a key' in res.extracted_content
	assert await page.input_value('#title') == 'A sentence, not a key.'
	res = await tool.send_keys(SendKeysAction(keys='Enter'), ctx)
	assert res.error is None and await page.inner_text('#sent') == 'sent'


@pytest.mark.asyncio
async def test_type_text_goes_to_the_clicked_element_and_submits(rig):
	tool, page, ctx = rig.tool, rig.page, rig.execution
	await tool.click_element(ClickElementAction(index=await rig.index_of('title')), ctx)
	res = await tool.type_text(TypeTextAction(text='typed', submit=True), ctx)
	assert res.error is None
	assert await page.input_value('#title') == 'typed'
	assert await page.inner_text('#sent') == 'sent'


@pytest.mark.asyncio
async def test_a_dialog_is_reported_in_the_click_result_and_can_be_declined(rig):
	tool, page, ctx = rig.tool, rig.page, rig.execution
	index = await rig.index_of('remove')
	res = await tool.click_element(ClickElementAction(index=index), ctx)
	assert res.error is None and 'Remove the draft?' in res.extracted_content and 'accepted' in res.extracted_content
	assert await page.inner_text('#sent') == 'removed'

	await tool.handle_dialog(DialogAnswerAction(accept=False), ctx)
	res = await tool.click_element(ClickElementAction(index=index), ctx)
	assert 'dismissed' in res.extracted_content
	assert await page.inner_text('#sent') == 'kept'


@pytest.mark.asyncio
async def test_read_element_and_script(rig):
	tool, ctx = rig.tool, rig.execution
	index = await rig.index_of('date')
	res = await tool.read_element(ElementIndexAction(index=index), ctx)
	assert res.error is None and '<input' in res.extracted_content and 'read-only' in res.extracted_content
	res = await tool.evaluate_js(EvaluateScriptAction(script='() => document.forms.length'), ctx)
	assert res.extracted_content.endswith('Result: 1')


@pytest.mark.asyncio
async def test_an_index_that_is_not_on_the_page_is_named(rig):
	await rig.index_of('title')
	res = await rig.tool.input_text(InputTextAction(index=9999, text='x'), rig.execution)
	assert res.error and 'does not exist' in res.error


def test_every_browser_action_has_a_policy_row():
	"""A new action with no row in the verb policy table is unclassified egress."""
	from core.verb_policy_rows import CORE_VERB_ROWS as ROWS

	names = {f'browser_{name}' for name in dir(Browser)
			 if getattr(getattr(Browser, name), '__is_action__', False)}
	assert names - set(ROWS['browser']) == set()
	assert set(ROWS['browser']) - names == set()
