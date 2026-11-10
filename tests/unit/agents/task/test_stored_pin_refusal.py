"""A stored pin on a model with no price cannot serve a billed session: every call would
fail. The pin is refused at load, by name, and the session runs on the default."""
import pytest

from agents.task.model_pin_state import stored_pin_refusal
from agents.task.model_switch_service import with_chat_pin
from agents.task.task_agent_delivery import _name_refused_pin
from agents.task.task_agent_support import refused_stored_pin
from core import model_pins
from core.billing_context import mark_billed, reset_billed
from modules.llm.messages import HumanMessage

PRICED = "z-ai/glm-5.2"
REMOVED = "acme/removed-row"


@pytest.fixture
def billed():
    token = mark_billed()
    yield
    reset_billed(token)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("core.runtime_paths.prefs_home_dir", lambda: str(tmp_path))


def test_a_removed_row_is_refused_by_name_and_a_priced_row_is_not():
    assert REMOVED in stored_pin_refusal("openrouter", REMOVED)
    assert stored_pin_refusal("openrouter", PRICED) == ""


def test_a_flat_rate_seat_is_not_refused():
    assert stored_pin_refusal("zai-coding", REMOVED) == ""


def test_a_billed_request_starts_on_the_default_and_the_notice_names_the_model(home, billed):
    model_pins.set_default("tenant1", "openrouter", REMOVED)
    body = with_chat_pin({"task": "t"}, "tenant1")
    assert "model" not in body and "provider" not in body and "_chat_pin" not in body
    assert REMOVED in body["_pin_notice"]


def test_a_billed_request_keeps_a_priced_pin(home, billed):
    model_pins.set_default("tenant1", "openrouter", PRICED)
    body = with_chat_pin({"task": "t"}, "tenant1")
    assert body["model"] == PRICED and "_pin_notice" not in body


def test_an_unbilled_request_keeps_its_pin(home):
    model_pins.set_default("owner1", "openrouter", REMOVED)
    body = with_chat_pin({"task": "t"}, "owner1")
    assert body["model"] == REMOVED and "_pin_notice" not in body


def _record(model, billing_limited=True):
    return {"billing_limited": billing_limited, "request": {"provider": "openrouter", "model": model}}


def test_a_recreated_billed_session_refuses_a_stored_model_with_no_price():
    assert REMOVED in refused_stored_pin(_record(REMOVED))
    assert refused_stored_pin(_record(PRICED)) == ""


def test_a_recreated_unbilled_session_keeps_its_stored_model():
    assert refused_stored_pin(_record(REMOVED, billing_limited=False)) == ""
    assert refused_stored_pin(None) == ""


def test_the_next_step_reads_the_refusal():
    pushed = []
    agent = type("A", (), {"message_manager": type("M", (), {"push_ephemeral_message": lambda self, m: pushed.append(m)})()})()
    _name_refused_pin(agent, "the refusal")
    assert isinstance(pushed[0], HumanMessage) and pushed[0].content == "the refusal"
    assert pushed[0].metadata == {"per_call": True}
