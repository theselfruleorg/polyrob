"""098 S1: every registered StateMachine table is well formed — each state
reachable from a root, each non-terminal state has an exit, no terminal state
has an outgoing edge (the last is enforced at construction)."""
import importlib

import pytest

from core.state.machine import REGISTRY

#: Modules that define production machines. Adding a machine? Add its module
#: (``module:factory`` when the machine is built on first use).
MACHINE_MODULES = [
    "core.app_service.registry",
    "core.trading_mandate",
    "modules.x402.invoice_status",
    "modules.credits.billing_failures",
    "core.wallet.bridge_guard",
    "core.signer.store",
    "core.open_positions",
    "core.surfaces.room_action_store",
    "modules.x402.subscriptions",
    "core.self_context_writer",
    "core.publish",
    "tools.hf_deploy.registry",
    "core.surfaces.correspondents",
    "agents.task.agent.core.run_state",
    "agents.task.agent.session",
    "agents.task.session_residency",
    "agents.task.agent.tool_call_tracker",
    "core.session_control",
    "core.interactive_gate",
    "agents.task.goals.machines",
    "cron.job_machine",
    "agents.task.agent.autonomy_state",
    "core.credit_sentinel",
    "core.wake_queue",
    "cli.ui.autonomy_live",
    "core.state.lifecycle",
    "tools.mcp.transport_state",
    "utils.circuit_breaker",
    "tools.shell.job_state",
    "tools.oauth.manager",
    "polyrob.app.bot",
    "core.signer.remote",
    "core.state.journal",
    "surfaces.email.inbound_state",
    "core.surfaces.outbound_queue",
    "core.surfaces.idempotency",
    "core.lifecycle:_request_machine",  # built on first use (import cycle)
    "agents.task.agent.core.curator",
    "modules.memory.sqlite_curated_store",
    "modules.payments.sweep_state",
    "core.money.receipt_state",
    "scripts.release_train:_rc_machine",  # built on first use
]


def _load(entry):
    mod, _, factory = entry.partition(":")
    m = importlib.import_module(mod)
    if factory:
        getattr(m, factory)()
    return m


@pytest.fixture(scope="module")
def machines():
    for entry in MACHINE_MODULES:
        _load(entry)
    return {k: m for k, m in REGISTRY.items() if not k.startswith("test.")}


def test_every_machine_module_registers_one(machines):
    for entry in MACHINE_MODULES:
        m = _load(entry)
        if ":" in entry:
            continue  # the factory registered it; validated below
        assert any(getattr(m, a, None) in machines.values() for a in dir(m)), entry


def test_tables_well_formed(machines):
    problems = [p for m in machines.values() for p in m.validate()]
    assert not problems, "\n".join(problems)


def test_the_illegal_transition_kind_is_catalogued(monkeypatch):
    """098 review: the kind the machine emits is the event-kind SSOT's."""
    from core import event_kinds
    from core.state import machine as m
    seen = []
    monkeypatch.setattr(m, "_EVENT_SINK", lambda kind, attrs: seen.append(kind))
    m._emit_illegal("x", "a", "b", None)
    assert seen == [event_kinds.STATE_ILLEGAL_TRANSITION]
    assert event_kinds.STATE_ILLEGAL_TRANSITION in event_kinds.KNOWN_KINDS
