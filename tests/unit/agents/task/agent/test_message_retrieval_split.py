"""P9 pass-19 — MessageRetrievalMixin split out of message_manager/service.py."""


def test_message_manager_composes_retrieval_mixin():
    from agents.task.agent.message_manager.service import MessageManager
    from agents.task.agent.messages.retrieval import MessageRetrievalMixin
    assert issubclass(MessageManager, MessageRetrievalMixin)
    for m in ("get_messages", "get_messages_for_llm", "push_ephemeral_message",
              "calculate_llm_timeout", "get_llm_parameters", "_log_message_structure"):
        assert getattr(MessageManager, m).__qualname__.startswith("MessageRetrievalMixin")


def test_retrieval_module_imports_cleanly():
    import agents.task.agent.messages.retrieval as r
    assert r.MessageRetrievalMixin is not None
