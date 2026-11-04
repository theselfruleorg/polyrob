"""`browser_screenshot` writes a PNG the agent can DELIVER.

The step loop's vision capture lands in the session history directory, outside
the workspace, and `message(media_paths=...)` refuses any path outside the
workspace — so "take a screenshot and send it to me" could not work. The verb
writes into `<workspace>/screenshots/` and returns a path that passes the same
confinement check the delivery rail applies.
"""
import base64
import inspect
from types import SimpleNamespace

import pytest

import tools.browser.browser as browser_mod
from core.surfaces.attachments import validate_media_paths
from tools.browser.actions import ScreenshotAction, ScrollToTextAction
from tools.browser.screenshot import save_page_screenshot, screenshot_file_name

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


class _Ctx:
    def __init__(self, encoded):
        self.encoded = encoded
        self.full_page = None

    async def take_screenshot(self, full_page=False):
        self.full_page = full_page
        return self.encoded


def _exec(ws):
    return SimpleNamespace(workspace_dir=str(ws), session_id="s1", user_id="u1")


@pytest.mark.asyncio
async def test_the_png_lands_in_the_workspace_and_can_be_attached(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    ctx = _Ctx(base64.b64encode(_PNG).decode())

    res = await save_page_screenshot(ctx, ScreenshotAction(name="pricing", full_page=True), _exec(ws))

    path = ws / "screenshots" / "pricing.png"
    assert res.error is None
    assert path.read_bytes() == _PNG
    assert ctx.full_page is True
    assert str(path) in res.extracted_content
    assert "message(media_paths=" in res.extracted_content
    validated, err = validate_media_paths([str(path)], str(ws))
    assert err is None and validated


@pytest.mark.asyncio
async def test_an_empty_capture_is_an_error_not_a_file(tmp_path):
    res = await save_page_screenshot(_Ctx(""), ScreenshotAction(), _exec(tmp_path))
    assert res.error and "no image" in res.error
    assert not (tmp_path / "screenshots").exists()


@pytest.mark.asyncio
async def test_no_workspace_is_refused_by_name():
    res = await save_page_screenshot(_Ctx("eA=="), ScreenshotAction(),
                                     SimpleNamespace(workspace_dir=None, session_id=""))
    assert res.error and "no session workspace" in res.error


@pytest.mark.parametrize("raw, want", [
    ("../../etc/passwd", "passwd.png"),
    ("shot.PNG", "shot.png"),
    ("a b/c?d", "c-d.png"),
    (".hidden", "hidden.png"),
])
def test_the_file_name_cannot_leave_the_screenshots_folder(raw, want):
    assert screenshot_file_name(raw) == want


def test_an_unnamed_screenshot_gets_a_timestamp():
    name = screenshot_file_name(None)
    assert name.startswith("page-") and name.endswith(".png")


def test_screenshot_rejects_an_unknown_parameter():
    with pytest.raises(Exception):
        ScreenshotAction(path="/tmp/x.png")


def test_scroll_to_text_takes_text_and_still_accepts_the_old_keys_field():
    assert ScrollToTextAction(text="Pricing").text == "Pricing"
    assert ScrollToTextAction.model_validate({"keys": "Pricing"}).text == "Pricing"
    assert "text" in ScrollToTextAction.model_json_schema()["properties"]


def test_the_dead_upload_branch_is_gone():
    src = inspect.getsource(browser_mod)
    assert "a specific function to upload files" not in src


def test_the_screenshot_verb_has_a_policy_row():
    from core.verb_policy_rows import CORE_VERB_ROWS
    assert CORE_VERB_ROWS["browser"]["browser_screenshot"] == {"effect": "none"}
