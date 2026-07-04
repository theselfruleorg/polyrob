"""Conversational turn input: append the user's message as an ordinary trailing
turn (R4), distinct from inject_user_guidance's position-1 task-directive framing."""
from __future__ import annotations


class TurnInputMixin:
    def set_turn_input(self, text: str) -> None:
        self.message_manager.append_user_turn(text)
