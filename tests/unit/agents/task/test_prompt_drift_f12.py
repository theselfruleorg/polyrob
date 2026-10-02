"""F12: runtime texts name real actions and real parameters."""


def test_json_template_hint_names_real_actions_only():
    from agents.task.robust_parse_config import RobustParseConfig
    hint = RobustParseConfig.get_json_template_hint()
    assert '{"click":' not in hint and '{"type":' not in hint
    assert '"selector"' not in hint
    assert '{"browser_click_element": {"index": 3}}' in hint
    assert '{"browser_input_text": {"index": 3, "text": "input"}}' in hint
    assert '"filesystem_write_file"' in hint


def test_json_template_hint_params_match_the_action_models():
    from tools.browser.actions import ClickElementAction, InputTextAction
    from tools.controller.views import WriteFileAction
    ClickElementAction.model_validate({"index": 3})
    InputTextAction.model_validate({"index": 3, "text": "input"})
    WriteFileAction.model_validate({"file_path": "path", "content": "text"})


def test_delegation_result_frame_says_relay_to_a_waiting_user():
    import inspect
    from agents.task.agent import async_delegation
    src = inspect.getsource(async_delegation)
    assert "If a user is waiting for this result, send it to them with send_message." in src
