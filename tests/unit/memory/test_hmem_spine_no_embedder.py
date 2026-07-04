"""H-MEM injects its spine (session summary, findings, completed phases, recent steps)
with NO embedder present. Section 3 (semantic) is the only embedder-gated section."""
from core.config import BotConfig
from modules.memory.task.task_context_manager import TaskContextManager


def _tcm():
    # Real BotConfig, NO container/embedding_model service => semantic_retriever is None
    # (server default). Signature: TaskContextManager(name, config, base_path=None).
    return TaskContextManager(name="test-tcm", config=BotConfig())


def test_injection_nonempty_without_embedder():
    tcm = _tcm()
    sid = "s1"
    tcm.create_session(sid, task="compare pricing of A and B")
    tcm.add_step_memory(sid, step=1,
                        brain_state={"phase": "research", "memory": "A costs $10/mo", "next": "find B"},
                        action_summary="looked up A pricing", finding="A costs $10/mo")
    out = tcm.get_context_injection(sid)
    assert out, "H-MEM returned empty injection without an embedder — spine must not be embedder-gated"
    assert "A costs $10/mo" in out, "current-phase finding (section 2) missing from embedder-free injection"


def test_cross_phase_recall_without_embedder(monkeypatch):
    """Section-3 cross-phase recall must work via LexicalRetriever when no embedder exists.

    The scenario: a key fact is recorded in 'research' phase. In a later 'summary' phase
    the agent's brain state mentions that fact. H-MEM should surface it in the injection
    via lexical (TF cosine) retrieval — no torch/embedder required.
    """
    monkeypatch.setenv("HMEM_SEMANTIC", "auto")
    from core.config import BotConfig
    from modules.memory.task.task_context_manager import TaskContextManager
    tcm = TaskContextManager(name="t-xphase", config=BotConfig())
    sid = "sx"
    tcm.create_session(sid, task="research then summarize")
    # Phase 1 – research: record the key finding
    tcm.add_step_memory(sid, step=1,
                        brain_state={"phase": "research", "memory": "Acme price is $42",
                                     "next": "go"},
                        action_summary="found price", finding="Acme price is $42")
    # Phase 2 – summary: brain state references the fact; the retriever must surface it
    tcm.add_step_memory(sid, step=2,
                        brain_state={"phase": "summary", "memory": "writing summary about Acme price",
                                     "next": "done"},
                        action_summary="summarizing", finding="need the Acme price")
    out = tcm.get_context_injection(
        sid,
        brain_state={"phase": "summary", "memory": "what was the Acme price", "next": "x"},
    )
    assert out, "injection empty"
    # Cross-phase lexical recall should surface the phase-1 fact in a later phase.
    assert "Acme price is $42" in out, (
        f"cross-phase lexical recall failed; injection was:\n{out}"
    )
