"""Grant ratchet (036 §3.3): every production ``board.create(`` names its actor.

``GoalBoard.create`` runs ``core.tool_grants.assert_grantable`` on
``payload.tools`` for the ``actor`` it is given. ``actor=None`` is the
legacy/test path and passes unchanged — so the boundary holds only if every
production writer says who it is. This test converts that convention into a
boundary: a new ``board.create(`` call site (or ``self.create(`` inside the
board) without ``actor=`` fails CI.

AST scan over the committed production trees (``git ls-files`` so another
session's uncommitted files cannot flap it).
"""
import ast
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TREES = ("agents", "tools", "cli", "surfaces", "core", "cron", "api", "webview",
         "modules", "scripts")
BOARD_FILE = "agents/task/goals/board.py"


def _tracked():
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO, capture_output=True,
                         text=True, check=True).stdout.splitlines()
    return [p for p in out if p.split("/", 1)[0] in TREES and "/tests/" not in p
            and "node_modules" not in p]


def _receiver(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _receiver(node.value) + "." + node.attr
    if isinstance(node, ast.Call):
        return _receiver(node.func) + "()"
    return ""


def _create_sites():
    sites = []
    for rel in _tracked():
        path = REPO / rel
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "create"):
                continue
            recv = _receiver(node.func.value).lower()
            is_board = "board" in recv or (rel == BOARD_FILE and recv == "self")
            if is_board:
                sites.append((rel, node.lineno, {k.arg for k in node.keywords}))
    return sites


def test_every_board_create_names_its_actor():
    sites = _create_sites()
    assert sites, "the scan found no board.create call sites — the scanner is broken"
    missing = [f"{rel}:{line}" for rel, line, kws in sites if "actor" not in kws]
    assert not missing, (
        "board.create(...) without actor= — name the writer (owner_seat / agent / rail / "
        "none) so core.tool_grants.assert_grantable can judge payload.tools:\n"
        + "\n".join(missing))


def test_known_writers_are_covered():
    """The writers 036 §3.3 lists are all in the scan (the scanner sees them)."""
    files = {rel for rel, _l, _k in _create_sites()}
    for rel in ("tools/goal_tools.py", "cli/commands/goals.py",
                "surfaces/telegram/owner_ops.py", "core/owner_create.py",
                "agents/task/goals/rails.py", BOARD_FILE):
        assert rel in files, rel
