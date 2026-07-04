def test_source_precedence_in_system_prompt(monkeypatch):
    monkeypatch.setenv("SOURCE_PRECEDENCE_PROMPT", "true")
    from agents.task.agent.prompts import SystemPrompt
    pb = SystemPrompt.__new__(SystemPrompt)  # avoid full init; method is static-ish text
    text = pb._get_source_precedence_content()
    assert "compacted-history" in text.lower() or "compacted summary" in text.lower()
    assert "recalled" in text.lower()
    # precedence: pinned task/skill ranks above compacted summary
    assert text.lower().index("pinned") < text.lower().index("compacted")
