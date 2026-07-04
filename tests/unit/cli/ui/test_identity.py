"""The chat UI shows the agent's INSTANCE name (resolve_instance_id), not a
hardcoded 'rob' and not the framework name 'polyrob'."""


def test_agent_display_name_default(monkeypatch):
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    monkeypatch.delenv("BOT_INSTANCE_ID", raising=False)
    from cli.ui.identity import agent_display_name
    assert agent_display_name() == "rob"


def test_agent_display_name_honors_instance_id(monkeypatch):
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "aria")
    from cli.ui.identity import agent_display_name
    assert agent_display_name() == "aria"


def test_separator_label_uses_instance_name(monkeypatch):
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "aria")
    from cli.ui.app import separator_label
    from cli.ui.state import SessionState
    label = separator_label(SessionState())
    assert label.startswith("aria")  # not the hardcoded "rob"


def test_activity_line_uses_instance_name(monkeypatch):
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "aria")
    from cli.ui.activity import ActivityLine
    line = ActivityLine(None)
    assert line.compose_text().startswith("aria")


def test_plain_renderer_message_uses_instance_name(monkeypatch):
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "aria")
    import io
    from cli.ui.plain_renderer import PlainRenderer
    from cli.ui.state import SessionState
    buf = io.StringIO()
    PlainRenderer(state=SessionState(), stream=buf)._write_bubble("hello there")
    assert "aria:" in buf.getvalue()
    assert "rob:" not in buf.getvalue()
