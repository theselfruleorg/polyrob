"""P9 pass-14 — GuidanceMixin split out of message_manager/service.py."""


def test_message_manager_composes_guidance_mixin():
    from agents.task.agent.message_manager.service import MessageManager
    from agents.task.agent.messages.guidance import GuidanceMixin
    assert issubclass(MessageManager, GuidanceMixin)
    assert MessageManager.inject_user_guidance.__qualname__.startswith("GuidanceMixin")


def test_guidance_module_imports_cleanly():
    import agents.task.agent.messages.guidance as g
    assert g.GuidanceMixin is not None
