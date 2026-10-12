"""send_message only reaches the session's own principal (2026-10-09 19:01Z: a den
cron run used it for a room reply and the line landed in the owner's private chat).
Its description must say so and name the verb that posts to a room."""
import types


def _make_controller(tmp_path):
    import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator cycle
    from tools.controller.service import Controller

    orch = types.SimpleNamespace(session_id="s1", user_id="u1", workspace_dir=str(tmp_path))
    container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    return Controller(container=container, orchestrator=orch)


def test_send_message_description_names_its_only_recipient(tmp_path):
    desc = _make_controller(tmp_path).registry.registry.actions["send_message"].description
    assert "owner" in desc
    assert "never" in desc.lower() and "room" in desc
    assert "message(" in desc
