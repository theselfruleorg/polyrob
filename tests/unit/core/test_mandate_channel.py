"""086 W2: how a run's trading mandate reaches the money kernel.

``payload["mandate"]`` -> the cron runner / goal dispatcher's session request ->
``SessionRequest.mandate`` -> ``orchestrator._mandate`` (survives an eviction) ->
``ActionExecutionContext.metadata["mandate"]`` -> ``core.money.mandate_gate``.
"""
import inspect
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]


def test_session_request_carries_the_mandate():
    from agents.task.task_agent_support import SessionRequest
    req = SessionRequest(task="t", provider="p", model="m", mandate="m_1")
    assert req.mandate == "m_1"


def test_every_hop_names_the_mandate():
    hops = {
        "cron/runner.py": 'request["mandate"] = str(payload["mandate"])',
        "agents/task/goals/goal_run.py": 'request["mandate"] = str(payload["mandate"])',
        "agents/task_agent_lite.py": "mandate=request.get('mandate')",
        "agents/task/task_agent_delivery.py": "orchestrator._mandate = ",
        "agents/task/agent/core/step_execution.py":
            '"mandate": getattr(self.orchestrator, "_mandate", None)',
    }
    for rel, needle in hops.items():
        assert needle in (REPO / rel).read_text(), rel
    from core.trading_mandate import METADATA_KEY, PAYLOAD_KEY
    assert METADATA_KEY == PAYLOAD_KEY == "mandate"
