"""Acceptance reproduction of the 8h battle-test "shared toolkit" goal.

The battle test queued goals that each said "read toolkit/INDEX.md, append a tool,
don't duplicate" — but every goal run got a FRESH per-session workspace, so 527
files scattered across 77 session folders and the read step always 404'd.

This reproduces that scenario at the filesystem level (no LLM): each "goal run" is a
distinct session_id resolving its workspace through pm().get_workspace_dir(), then
doing the read-append. With project-root mode ON (POLYROB_PROJECT_DIR) the three
runs CONVERGE on one folder and INDEX.md accumulates; with the legacy per-session
mode they FRAGMENT (the control = the original bug).

See docs/plans/2026-06-29-agent-working-directory-model-ANALYSIS.md (Model C).
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_pm():
    from agents.task.path import reset_path_manager
    reset_path_manager()
    yield
    reset_path_manager()


def _toolkit_goal_run(pm, session_id, tool_name, user_id="local"):
    """Simulate one goal run: read toolkit/INDEX.md, append a tool, don't duplicate.

    Returns the INDEX.md contents the run saw AFTER its append (or raises if the
    workspace read path is broken, mirroring the battle-test File-not-found).
    """
    import os
    ws = pm.get_workspace_dir(session_id, user_id)
    toolkit = os.path.join(str(ws), "toolkit")
    os.makedirs(toolkit, exist_ok=True)
    index = os.path.join(toolkit, "INDEX.md")
    # read (the step that 404'd in the battle test when each run had a fresh ws)
    existing = ""
    if os.path.exists(index):
        with open(index) as f:
            existing = f.read()
    # don't duplicate
    if tool_name not in existing:
        with open(index, "a") as f:
            f.write(f"- {tool_name}\n")
    # write the tool's own file
    with open(os.path.join(toolkit, f"{tool_name}.txt"), "w") as f:
        f.write(tool_name)
    with open(index) as f:
        return f.read()


def test_project_root_mode_accumulates_one_toolkit(tmp_path):
    # Model C: explicit project dir => all goal runs share ONE workspace folder.
    from agents.task.path import get_path_manager, set_path_manager
    project = tmp_path / "project"
    pm = get_path_manager(
        data_root=str(tmp_path / "data" / "sessions"),
        workspace_is_project_root=True,
        project_root=str(project),
    )
    set_path_manager(pm)

    # three "goal runs" with DISTINCT session ids (as fresh uuid4 goal runs would have)
    _toolkit_goal_run(pm, "sess-aaaaaaa1", "scraper")
    _toolkit_goal_run(pm, "sess-bbbbbbb2", "summarizer")
    final_index = _toolkit_goal_run(pm, "sess-ccccccc3", "ranker")

    # INDEX.md accumulated all three, in ONE folder, no duplicates, no 404.
    assert "scraper" in final_index
    assert "summarizer" in final_index
    assert "ranker" in final_index
    toolkit = project / "toolkit"
    names = sorted(p.name for p in toolkit.glob("*.txt"))
    assert names == ["ranker.txt", "scraper.txt", "summarizer.txt"]
    # exactly one INDEX.md, with exactly three tool lines (idempotent / de-duped)
    lines = [ln for ln in (project / "toolkit" / "INDEX.md").read_text().splitlines() if ln.strip()]
    assert len(lines) == 3


def test_per_session_mode_fragments_control(tmp_path):
    # Control: legacy per-session ephemeral workspaces => each run sees an EMPTY
    # toolkit, so INDEX.md never accumulates and the files scatter (the original bug).
    from agents.task.path import get_path_manager, set_path_manager
    pm = get_path_manager(data_root=str(tmp_path / "data" / "sessions"))
    set_path_manager(pm)

    idx1 = _toolkit_goal_run(pm, "sess-aaaaaaa1", "scraper")
    idx2 = _toolkit_goal_run(pm, "sess-bbbbbbb2", "summarizer")

    # Each run only ever saw its OWN tool — no accumulation (fragmentation reproduced).
    assert "summarizer" not in idx1
    assert "scraper" not in idx2
    # The two workspaces are physically different folders.
    ws1 = pm.get_workspace_dir("sess-aaaaaaa1", "local")
    ws2 = pm.get_workspace_dir("sess-bbbbbbb2", "local")
    assert ws1 != ws2
