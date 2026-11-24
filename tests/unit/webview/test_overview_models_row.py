"""Agent › Overview "Models" reads the ONE chat-runtime resolver `/model` reads
(core.model_pins.chat_runtime_for), with the caller's tenant — not a second
CLI resolver that ignored the owner's default."""


def test_the_models_row_reads_chat_runtime_for_the_owner(monkeypatch):
    import core.model_pins as model_pins
    from webview import pages
    seen = []

    def fake(user_id, env=None, home_dir=None):
        seen.append(user_id)
        return ("zai", "glm-x", None, "your saved model (/model <name>)")

    monkeypatch.setattr(model_pins, "chat_runtime_for", fake)
    assert pages._provider_model("owner1") == ("zai", "glm-x")
    assert seen == ["owner1"]


def test_a_resolver_fault_is_no_model_not_a_crash(monkeypatch):
    import core.model_pins as model_pins
    from webview import pages

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(model_pins, "chat_runtime_for", boom)
    assert pages._provider_model("owner1") == (None, None)
