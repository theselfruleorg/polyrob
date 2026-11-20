"""121 on the console: Agent › Settings shows the heavy/light/choice lines the
bare /model shows, and posts the /model words of each setting through the one
/model pipeline (model_command → model_settings.settings_command)."""
from webview.model_routes import _words


def test_body_to_model_words():
    assert _words({"tier": "heavy", "name": "glm-4.6"}) == ["heavy", "glm-4.6"]
    assert _words({"tier": "light", "reset": True}) == ["light", "reset"]
    assert _words({"choice": "listed"}) == ["choice", "listed"]
    assert _words({"name": "gpt-x"}) == ["gpt-x", "default"]
    assert _words({"reset": True}) == ["reset", "default"]


def test_malformed_bodies_are_refused():
    for body in ({"tier": "medium", "name": "x"}, {"tier": "heavy", "name": ""},
                 {"tier": "heavy", "name": "go"}, {"choice": "any"},
                 {"name": "a=b"}, {"name": "one two three"}, {}):
        assert _words(body) is None, body


def test_settings_lines_are_the_model_views(monkeypatch):
    import agents.task.model_settings as ms
    from webview import model_routes
    monkeypatch.setattr(ms, "settings_lines", lambda uid: ["Heavy model: x"])
    assert model_routes._settings("o") == ["Heavy model: x"]

    def boom(uid):
        raise OSError("x")

    monkeypatch.setattr(ms, "settings_lines", boom)
    assert "could not be read" in model_routes._settings("o")[0]
