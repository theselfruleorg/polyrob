import pytest
from agents.task.agent.core.user_ingress import UserIngressMixin


class _MM:
    def __init__(self): self.appended = []
    def append_user_turn(self, text): self.appended.append(text)


class _Agent(UserIngressMixin):
    def __init__(self): self.message_manager = _MM()


def test_set_turn_input_appends_plain_user_message():
    a = _Agent()
    a.set_turn_input("follow-up question")
    assert a.message_manager.appended == ["follow-up question"]
