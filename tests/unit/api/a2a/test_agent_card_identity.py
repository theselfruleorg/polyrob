"""The A2A agent card names THIS instance, not the framework.

The card's name, description and provider organization come from the one
identity read the ERC-8004 registration file uses, so the two never disagree.
"""
import pytest

import api.a2a.agent_card as agent_card
import modules.eip8004.registration as registration


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setattr(registration, "agent_identity",
                        lambda: ("Example Agent", "An example agent."))
    monkeypatch.delenv("A2A_BASE_URL", raising=False)


def test_card_takes_the_instance_identity(identity):
    card = agent_card.build_agent_card()
    assert card.name == "Example Agent"
    assert card.description == "An example agent."
    assert card.provider.organization == "Example Agent"


def test_card_and_registration_file_name_the_same_agent(identity):
    card = agent_card.build_agent_card()
    reg = registration.build_registration_file("https://agent.example")
    assert card.name == reg.name


def test_card_has_no_framework_literals():
    card = agent_card.build_agent_card()
    assert card.name != "POLYROB"
    assert card.provider.organization != "The Self Rule"


def test_card_renders_without_the_eip8004_package(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _no_eip8004(name, *a, **kw):
        if name.startswith("modules.eip8004"):
            raise ImportError("no web3")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_eip8004)
    from core.instance import resolve_instance_id
    name, description = agent_card._card_identity()
    assert name == resolve_instance_id()
    assert description
