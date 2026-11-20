"""The agent core is reached from above through ONE facade (proposal 125 S29).

Code outside ``agents/`` reads an agent-core name from :mod:`agents.task.public`,
never from ``agents.task.agent.core.*`` directly. The direct edges that remain are
a SHRINK-ONLY allowlist: move a use onto the facade and delete its row; a new
edge needs a facade row, not an allowlist row. The scan is the layering
ratchet's (AST, lazy imports and ``"module:attr"`` strings count).
"""
import subprocess
import sys

from tests.test_layering_ratchet import REPO_ROOT, _imported_modules, _source_files, _string_refs

_CORE = "agents.task.agent.core."

#: (file, agent-core module) edges from outside agents/. SHRINK-ONLY. Seeded 2026-11-20 with
#: 13 edges; the 11 that use a public name moved onto the facade in the same change.
ALLOWLISTED_AGENT_CORE_EDGES = frozenset({
    # a private name (_clip_entry): needs a public one before it can move to the facade
    ("tools/controller/_helpers.py", "agents.task.agent.core.memory_prefetch"),
    # a private name (_COMPUTE_TOOLS): needs a public one before it can move to the facade
    ("tools/tool_disclosure.py", "agents.task.agent.core.tool_availability"),
})


def _module_of(target: str) -> str:
    return ".".join(target.split(".")[:5])


def _agent_core_edges():
    edges = set()
    for rel, module in _source_files():
        if rel.startswith("agents/"):
            continue
        for target, _level, _is_name in _imported_modules(rel, module):
            if target.startswith(_CORE):
                edges.add((rel, _module_of(target)))
        for ref in _string_refs(rel):
            if ref.startswith(_CORE):
                edges.add((rel, _module_of(ref)))
    return edges


def test_no_new_direct_agent_core_import_from_outside_agents():
    new = sorted(_agent_core_edges() - ALLOWLISTED_AGENT_CORE_EDGES)
    assert not new, ("code outside agents/ imports agents.task.agent.core.* directly: add the "
                     f"name to agents/task/public.py and import it from there: {new}")


def test_allowlist_rows_still_exist():
    stale = sorted(ALLOWLISTED_AGENT_CORE_EDGES - _agent_core_edges())
    assert not stale, f"these edges are gone — delete the rows: {stale}"


def test_the_facade_loads_no_agent_core_module_until_a_name_is_used():
    code = ("import sys, agents.task.public; "
            "hit = [m for m in sys.modules if m.startswith('agents.task.agent')]; "
            "assert not hit, hit; print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_facade_export_resolves():
    import agents.task.public as public
    for name in public.__all__:
        assert getattr(public, name) is not None, name


def test_the_facade_sees_a_host_monkeypatch(monkeypatch):
    import agents.task.agent.core.run_budget as rb
    import agents.task.public as public
    monkeypatch.setattr(rb, "run_spend_vs_cap", "patched")
    assert public.run_spend_vs_cap == "patched"
