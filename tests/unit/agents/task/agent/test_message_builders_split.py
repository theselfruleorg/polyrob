"""P9 pass-18 — MessageBuildersMixin split out of message_manager/service.py."""


def test_message_manager_composes_builders_mixin():
    from agents.task.agent.message_manager.service import MessageManager
    from agents.task.agent.messages.builders import MessageBuildersMixin
    assert issubclass(MessageManager, MessageBuildersMixin)
    for m in ("add_state_message", "add_model_output", "_add_output_header",
              "add_tool_response", "add_tool_call_pair_atomic"):
        assert getattr(MessageManager, m).__qualname__.startswith("MessageBuildersMixin")


def test_builders_module_imports_cleanly():
    import agents.task.agent.messages.builders as b
    assert b.MessageBuildersMixin is not None
