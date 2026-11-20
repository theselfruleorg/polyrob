"""The public knowledge row loads in its own try: a failed owner/self doc read above
it must not drop identity/public.md from every session."""
import logging
from types import SimpleNamespace

from agents.task.agent.core.construction import AgentConstructionMixin


def test_public_knowledge_survives_a_failed_self_context(monkeypatch, tmp_path):
    import core.instance as inst
    import core.runtime_paths as rp

    def boom(*a, **k):
        raise OSError("owner.md unreadable")

    monkeypatch.setattr(inst, "load_self_context", boom)
    monkeypatch.setattr(inst, "load_public_doc", lambda home: "We build open tools.")
    monkeypatch.setattr(rp, "prefs_home_dir", lambda: str(tmp_path))
    got = {}
    a = AgentConstructionMixin.__new__(AgentConstructionMixin)
    a.orchestrator = SimpleNamespace(user_id="u1")
    a.logger = logging.getLogger("t")
    a.message_manager = SimpleNamespace(set_public_knowledge=lambda c: got.setdefault("c", c),
                                        set_self_context_blocks=lambda blocks: None)
    a._init_self_context(None)
    assert got.get("c") == "We build open tools."
