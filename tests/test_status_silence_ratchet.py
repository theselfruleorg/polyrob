"""Silent-omission ratchet for status / telemetry render paths (2026-08-28).

The 2026-08-28 /status incident: every section was wrapped in
``except Exception: logger.debug(...)`` and then OMITTED, so an unreadable
ledger, posture card or cron store rendered exactly like a healthy zero.
Rule: a status section that cannot be computed renders ``unavailable
(<reason>)`` — it never vanishes.

Mechanism (AST, mirrors tests/test_file_size_ratchet.py): in each designated
status-render module, count ``except`` handlers whose body is SILENT — only
``pass``, ``continue``, a bare ``return``/``return None``, and/or logging
calls (``logger.*`` / ``self.logger.*`` / ``logging.*``). The per-file
ceilings may only SHRINK; a new silent handler in one of these files fails.
The two new SSOT modules are pinned at ZERO.
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# file -> max allowed SILENT except-handlers. Shrink-only.
CEILINGS = {
    "core/status_snapshot.py": 0,
    "core/status_render.py": 0,
    "tools/controller/agent_status_action.py": 0,
    # the ONE outer fail-open guard around the injection itself (protects the
    # agent loop; the note it would carry is the thing that failed)
    "agents/task/agent/core/live_health.py": 1,
    "core/config_policy/posture_card.py": 1,
    "cron/digest.py": 2,
    "surfaces/telegram/harness.py": 34,
    # 2026-08-28: 10 -> 3. The three survivors are not status omissions:
    # a candidate-path scan `continue` (playwright probe), an env-file
    # candidate scan `continue`, and a float-parse fallback in
    # `_frozen_values_agree`. Every section-level handler now reports.
    "cli/commands/doctor.py": 3,
    "cli/commands/autonomy.py": 0,
    # WS-J3: _console_task_agent is a fail-open SERVICE accessor (returns None
    # when the in-process TaskAgent is absent), not a vanishing status section.
    "webview/pages.py": 8,
}

_LOG_NAMES = {"logger", "logging", "log"}


def _is_log_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    # logger.debug(...) / self.logger.debug(...) / logging.debug(...)
    if isinstance(func, ast.Attribute):
        base = func.value
        if isinstance(base, ast.Name) and base.id in _LOG_NAMES:
            return True
        if isinstance(base, ast.Attribute) and base.attr in _LOG_NAMES:
            return True
    return False


def _is_silent(handler: ast.ExceptHandler) -> bool:
    for stmt in handler.body:
        if isinstance(stmt, (ast.Pass, ast.Continue)):
            continue
        if isinstance(stmt, ast.Return) and (
                stmt.value is None or (isinstance(stmt.value, ast.Constant) and stmt.value.value is None)):
            continue
        if _is_log_call(stmt):
            continue
        return False
    return True


def silent_handlers(rel: str) -> int:
    tree = ast.parse((REPO / rel).read_text(), filename=rel)
    return sum(1 for n in ast.walk(tree)
               if isinstance(n, ast.ExceptHandler) and _is_silent(n))


def test_no_new_silent_except_in_status_paths():
    over = {rel: (silent_handlers(rel), cap) for rel, cap in CEILINGS.items()
            if (REPO / rel).exists() and silent_handlers(rel) > cap}
    assert not over, (
        "New SILENT except-handler(s) in a status/telemetry render path — a section "
        "that cannot be computed must render `unavailable (<reason>)`, never vanish:\n"
        + "\n".join(f"  {rel}: {n} silent handlers > ceiling {cap}" for rel, (n, cap) in over.items()))


def test_ceilings_track_actual():
    """A ceiling far above the real count means a cleanup forgot to tighten it."""
    slack = 3
    loose = {rel: (silent_handlers(rel), cap) for rel, cap in CEILINGS.items()
             if (REPO / rel).exists() and cap - silent_handlers(rel) > slack}
    assert not loose, (
        "Ceiling(s) sit far above the real silent-handler count — lower them:\n"
        + "\n".join(f"  {rel}: ceiling {cap} vs actual {n}" for rel, (n, cap) in loose.items()))
