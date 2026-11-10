"""The browser interaction engine against a real Chromium page.

An agent filled a public form and reported two fields it "could not drive": a
rich-text box and a file upload. The log of that run shows four tool defects,
and each has a test here:

- text into a ``contenteditable`` editor failed (and a later attempt reported a
  success that changed nothing);
- text sent as "keys" was refused as an unknown key;
- a read-only date field held the action until the 120 s action limit;
- there was no file upload.

The page tests skip when no Chromium can start (a core install has none).
"""
import time

import pytest
import pytest_asyncio

from tools.browser import interaction
from tools.browser.interaction import InteractionError

PAGE = """
<html><body>
<input id="plain" value="old">
<input id="picker" readonly value="">
<input id="day" type="date">
<input id="secret" type="password">
<textarea id="area"></textarea>
<div id="editor" contenteditable="true"><p id="para">old text</p></div>
<div id="wrap"><span>Name</span><input id="inner"></div>
<div id="bare">no field here</div>
<select id="pick"><option>One</option><option>Two</option></select>
<input id="box" type="checkbox">
<label id="styled"><input id="hidden-box" type="checkbox" style="position:absolute;opacity:0;width:0;height:0"><span>Agree</span></label>
<input id="off" disabled>
<iframe id="frame" srcdoc="<body contenteditable='true'><p>in frame</p></body>"></iframe>

<div id="drop"><button id="upload-button" type="button">Choose a cover image</button>
  <input id="file" type="file" accept="image/*" style="display:none"></div>
<button id="chooser" type="button"
  onclick="const i = document.createElement('input'); i.type = 'file'; i.onchange = () => { document.getElementById('chosen').textContent = i.files[0].name; }; i.click();">Attach</button>
<div id="chosen"></div>

<button id="ask" onclick="document.getElementById('answer').textContent = String(confirm('Delete it?'))">Ask</button>
<div id="answer"></div>
<div id="hover" onmouseover="this.textContent = 'hovered'" style="width:80px;height:20px">menu</div>
<div id="from" style="width:60px;height:30px;background:#ccc"
  onmousedown="document.getElementById('log').textContent = 'down'">drag me</div>
<div id="to" style="width:60px;height:30px;background:#eee;margin-top:40px"
  onmouseup="document.getElementById('log').textContent += '-up'">target</div>
<div id="log"></div>
<div id="list" style="height:60px;overflow-y:auto"><div id="row" style="height:600px">rows</div></div>
</body></html>
"""


@pytest_asyncio.fixture
async def page():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip('playwright is not installed')
    manager = async_playwright()
    playwright = await manager.start()
    browser = None
    for options in ({}, {'channel': 'chromium'}):  # the headless shell, then the full build
        try:
            browser = await playwright.chromium.launch(**options)
            break
        except Exception as exc:
            reason = str(exc).splitlines()[0]
    if browser is None:
        await manager.__aexit__(None, None, None)
        pytest.skip(f'no Chromium to launch: {reason}')
    context = await browser.new_context()
    context.set_default_timeout(60000)  # the tool's own default: a wait must not rely on it
    page = await context.new_page()
    await page.set_content(PAGE)
    try:
        yield page
    finally:
        await browser.close()
        await manager.__aexit__(None, None, None)


async def _el(page, selector):
    handle = await page.query_selector(selector)
    assert handle is not None, selector
    return handle


# ------------------------------------------------------------------ text

@pytest.mark.asyncio
async def test_plain_field_is_replaced_and_read_back(page):
    out = await interaction.fill_element(page, await _el(page, '#plain'), 'new value')
    assert await page.input_value('#plain') == 'new value'
    assert "'new value'" in out


@pytest.mark.asyncio
async def test_clear_false_adds_at_the_end(page):
    await interaction.fill_element(page, await _el(page, '#plain'), ' more', clear=False)
    assert await page.input_value('#plain') == 'old more'


@pytest.mark.asyncio
async def test_rich_text_editor_takes_lines_and_quotes(page):
    """The element the agent indexed was the <p> INSIDE the editor."""
    text = 'First "line" with a quote\n\nSecond line — after a gap'
    out = await interaction.fill_element(page, await _el(page, '#para'), text)
    held = await page.inner_text('#editor')
    assert 'First "line" with a quote' in held and 'Second line — after a gap' in held
    assert 'old text' not in held
    assert 'First' in out


@pytest.mark.asyncio
async def test_long_text_goes_into_the_editor(page):
    text = '\n'.join(f'Paragraph {n} ' + 'word ' * 40 for n in range(6))
    await interaction.fill_element(page, await _el(page, '#editor'), text)
    held = await page.inner_text('#editor')
    assert 'Paragraph 0' in held and 'Paragraph 5' in held


@pytest.mark.asyncio
async def test_editor_inside_an_iframe(page):
    frame = page.frame_locator('#frame')
    handle = await frame.locator('p').element_handle(timeout=5000)
    await interaction.fill_element(page, handle, 'typed in the frame')
    assert 'typed in the frame' in await frame.locator('body').inner_text()


@pytest.mark.asyncio
async def test_read_only_picker_field_is_set_fast_and_says_so(page):
    started = time.monotonic()
    out = await interaction.fill_element(page, await _el(page, '#picker'), '2026-01-15')
    assert time.monotonic() - started < 10, 'a read-only field must not hold the action'
    assert await page.input_value('#picker') == '2026-01-15'
    assert 'read-only' in out


@pytest.mark.asyncio
async def test_picker_widget_is_told_the_date_not_only_the_field(page):
    """A flatpickr-shaped widget keeps its own date; a bare field value leaves it empty."""
    await page.evaluate("""() => {
        const el = document.getElementById('picker');
        el._flatpickr = { chosen: null, setDate(value, fire) { this.chosen = value; el.value = value; } };
    }""")
    await interaction.fill_element(page, await _el(page, '#picker'), '2026-01-15')
    assert await page.evaluate("document.getElementById('picker')._flatpickr.chosen") == '2026-01-15'
    assert await page.input_value('#picker') == '2026-01-15'


@pytest.mark.asyncio
async def test_date_field(page):
    await interaction.fill_element(page, await _el(page, '#day'), '2026-01-15')
    assert await page.input_value('#day') == '2026-01-15'


@pytest.mark.asyncio
async def test_date_field_names_its_format_for_a_wrong_one(page):
    with pytest.raises(InteractionError, match='YYYY-MM-DD'):
        await interaction.fill_element(page, await _el(page, '#day'), '15-01-2026')


@pytest.mark.asyncio
async def test_password_value_is_not_echoed(page):
    out = await interaction.fill_element(page, await _el(page, '#secret'), 'hunter-two')
    assert 'hunter-two' not in out and 'hidden' in out


@pytest.mark.asyncio
async def test_wrapper_around_a_field_fills_the_field(page):
    await interaction.fill_element(page, await _el(page, '#wrap'), 'inside')
    assert await page.input_value('#inner') == 'inside'


@pytest.mark.asyncio
@pytest.mark.parametrize('selector,remedy', [
    ('#pick', 'browser_select_dropdown_option'),
    ('#box', 'browser_set_checked'),
    ('#file', 'browser_upload_file'),
    ('#bare', 'browser_type_text'),
    ('#off', 'disabled'),
])
async def test_wrong_element_names_the_right_action(page, selector, remedy):
    with pytest.raises(InteractionError, match=remedy):
        await interaction.fill_element(page, await _el(page, selector), 'x')


@pytest.mark.asyncio
async def test_type_text_goes_to_the_focus_and_keeps_line_breaks(page):
    await page.focus('#area')
    await interaction.type_text(page, 'one\ntwo')
    assert await page.input_value('#area') == 'one\ntwo'


# ------------------------------------------------------------------ keys

@pytest.mark.parametrize('keys,expected', [
    ('Enter', ['Enter']),
    ('enter', ['Enter']),
    ('ctrl+a', ['Control+a']),
    ('Control+Shift+ArrowLeft', ['Control+Shift+ArrowLeft']),
    ('Tab Tab Enter', ['Tab', 'Tab', 'Enter']),
    ('esc', ['Escape']),
    ('a', ['a']),
    ('+', ['+']),
    ('F5', ['F5']),
])
def test_keys_parse(keys, expected):
    assert interaction.parse_keys(keys) == expected


@pytest.mark.parametrize('text', [
    'The mint opens on Friday. Each item has its own page.',
    'hello',
    'a b',
    'Enter the code',
    '',
])
def test_text_is_not_keys(text):
    assert interaction.parse_keys(text) is None


# ------------------------------------------------------------------ files

def _workspace(tmp_path):
    ws = tmp_path / 'ws'
    ws.mkdir()
    (ws / 'cover.png').write_bytes(b'\x89PNG fake')
    (ws / 'second.png').write_bytes(b'\x89PNG two')
    return ws


def test_upload_payload_is_read_from_the_workspace(tmp_path):
    ws = _workspace(tmp_path)
    (payload,) = interaction.load_upload_payloads(['cover.png'], str(ws))
    assert payload == {'name': 'cover.png', 'mimeType': 'image/png', 'buffer': b'\x89PNG fake'}


def test_upload_refuses_a_path_outside_the_workspace(tmp_path):
    ws = _workspace(tmp_path)
    outside = tmp_path / 'secret.env'
    outside.write_text('KEY=1')
    (ws / 'link.png').symlink_to(outside)
    for path in (str(outside), '../secret.env', 'link.png'):
        with pytest.raises(InteractionError, match='upload refused'):
            interaction.load_upload_payloads([path], str(ws))


def test_upload_refuses_a_missing_file_no_workspace_and_too_many(tmp_path):
    ws = _workspace(tmp_path)
    with pytest.raises(InteractionError, match='no such file'):
        interaction.load_upload_payloads(['absent.png'], str(ws))
    with pytest.raises(InteractionError, match='upload refused'):
        interaction.load_upload_payloads(['cover.png'], None)
    with pytest.raises(InteractionError, match='at most'):
        interaction.load_upload_payloads(['cover.png'] * (interaction.MAX_UPLOAD_FILES + 1), str(ws))
    with pytest.raises(InteractionError, match='at least one'):
        interaction.load_upload_payloads([], str(ws))


def test_upload_refuses_a_file_above_the_limit(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    monkeypatch.setattr(interaction, 'MAX_UPLOAD_BYTES', 4)
    with pytest.raises(InteractionError, match='file limit'):
        interaction.load_upload_payloads(['cover.png'], str(ws))


@pytest.mark.asyncio
async def test_upload_through_the_button_of_a_hidden_file_field(page, tmp_path):
    payloads = interaction.load_upload_payloads(['cover.png'], str(_workspace(tmp_path)))
    out = await interaction.upload_files(page, await _el(page, '#upload-button'), payloads)
    assert 'cover.png' in out
    assert await page.evaluate("document.getElementById('file').files[0].name") == 'cover.png'
    assert await page.evaluate("document.getElementById('file').files[0].size") == len(b'\x89PNG fake')


@pytest.mark.asyncio
async def test_upload_with_no_index_uses_the_only_file_field(page, tmp_path):
    payloads = interaction.load_upload_payloads(['cover.png'], str(_workspace(tmp_path)))
    await interaction.upload_files(page, None, payloads)
    assert await page.evaluate("document.getElementById('file').files.length") == 1


@pytest.mark.asyncio
async def test_upload_through_a_file_chooser(page, tmp_path):
    payloads = interaction.load_upload_payloads(['cover.png'], str(_workspace(tmp_path)))
    out = await interaction.upload_files(page, await _el(page, '#chooser'), payloads)
    assert 'file chooser' in out
    assert await page.inner_text('#chosen') == 'cover.png'


@pytest.mark.asyncio
async def test_upload_refuses_two_files_for_a_single_file_field(page, tmp_path):
    payloads = interaction.load_upload_payloads(['cover.png', 'second.png'], str(_workspace(tmp_path)))
    with pytest.raises(InteractionError, match='one file'):
        await interaction.upload_files(page, await _el(page, '#file'), payloads)


@pytest.mark.asyncio
async def test_upload_on_an_element_with_no_file_field_is_named(page, tmp_path):
    payloads = interaction.load_upload_payloads(['cover.png'], str(_workspace(tmp_path)))
    with pytest.raises(InteractionError, match='no file field'):
        await interaction.upload_files(page, await _el(page, '#hover'), payloads)


# ------------------------------------------------------------------ pointer, boxes

@pytest.mark.asyncio
async def test_set_checked_on_a_box_and_on_a_styled_label(page):
    assert await interaction.set_checked(await _el(page, '#box'), True) is True
    assert await interaction.set_checked(await _el(page, '#box'), True) is True  # no second click
    assert await page.is_checked('#box')
    assert await interaction.set_checked(await _el(page, '#box'), False) is False
    assert await interaction.set_checked(await _el(page, '#styled'), True) is True
    assert await page.is_checked('#hidden-box')
    with pytest.raises(InteractionError, match='not a checkbox'):
        await interaction.set_checked(await _el(page, '#bare'), True)


@pytest.mark.asyncio
async def test_hover_and_drag(page):
    await interaction.hover(await _el(page, '#hover'))
    assert await page.inner_text('#hover') == 'hovered'
    await interaction.drag_and_drop(page, await _el(page, '#from'), await _el(page, '#to'))
    assert await page.inner_text('#log') == 'down-up'


@pytest.mark.asyncio
async def test_scroll_inside_a_scrollable_area(page):
    assert await interaction.scroll_element(await _el(page, '#row'), 0, 200) is True
    assert await page.evaluate("document.getElementById('list').scrollTop") == 200
    assert await interaction.scroll_element(await _el(page, '#plain'), 0, 200) is False


# ------------------------------------------------------------------ dialogs, waiting, script

class _Owner:
    pass


@pytest.mark.asyncio
async def test_dialog_is_accepted_by_default_and_reported(page):
    owner = _Owner()
    interaction.attach_dialog_handler(page.context, owner)
    await page.click('#ask')
    assert await page.inner_text('#answer') == 'true'
    note = interaction.take_dialog_note(owner)
    assert 'Delete it?' in note and 'accepted' in note
    assert interaction.take_dialog_note(owner) == ''


@pytest.mark.asyncio
async def test_dialog_answer_holds_for_one_dialog(page):
    owner = _Owner()
    interaction.attach_dialog_handler(page.context, owner)
    interaction.set_dialog_answer(owner, False, None)
    await page.click('#ask')
    assert await page.inner_text('#answer') == 'false'
    assert 'dismissed' in interaction.take_dialog_note(owner)
    await page.click('#ask')
    assert await page.inner_text('#answer') == 'true'


@pytest.mark.asyncio
async def test_wait_for_text_and_its_limit(page):
    await page.evaluate("setTimeout(() => { document.getElementById('log').textContent = 'Saved now'; }, 300)")
    assert 'Saved now' in await interaction.wait_for(page, seconds=5, text='Saved now')
    with pytest.raises(InteractionError, match='did not show'):
        await interaction.wait_for(page, seconds=0.3, text='never on this page')


@pytest.mark.asyncio
async def test_evaluate_returns_bounded_json(page):
    assert await interaction.evaluate_script(page, '() => ({n: 1 + 1})') == '{"n": 2}'
    long = await interaction.evaluate_script(page, "'x'.repeat(20000)")
    assert len(long) < interaction.MAX_EVALUATE_CHARS + 80 and 'return less' in long


def test_dialog_note_of_an_owner_with_no_handler_is_empty():
    from unittest.mock import MagicMock
    assert interaction.take_dialog_note(MagicMock()) == ''
    assert interaction.take_dialog_note(object()) == ''
