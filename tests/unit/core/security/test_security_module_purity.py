"""R-4: core/security modules must be importable without dragging agents/tools/modules.

These are the tier-0 security primitives (secret_guard, untrusted_wrap,
forged_turns) promoted out of ``agents/task/agent/core/`` — the whole point of
the promotion is that ``import core.security.*`` pulls ZERO upper-tier modules,
and the old agents-tier paths stay working as re-export shims (existing
importers and monkeypatch targets are untouched).
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def _assert_pure(module: str):
    code = (
        f"import sys, {module}; "
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('agents', 'tools', 'modules')); "
        "print('LEAKED:' + ','.join(leaked)); "
        "raise SystemExit(1 if leaked else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"{module} leaked upper tiers:\n{r.stdout}\n{r.stderr}"


def test_secret_guard_is_pure():
    _assert_pure("core.security.secret_guard")


def test_old_secret_guard_path_still_works():
    from core.security.secret_guard import is_secret_path as canonical
    from agents.task.agent.core.secret_guard import is_secret_path as shimmed
    from agents.task.agent.core.secret_guard import is_credential_file  # noqa: F401
    assert canonical is shimmed


def test_untrusted_wrap_is_pure():
    _assert_pure("core.security.untrusted_wrap")


def test_old_untrusted_wrap_path_still_works():
    from core.security.untrusted_wrap import wrap_untrusted as canonical
    from agents.task.agent.core.untrusted_wrap import wrap_untrusted as shimmed
    from agents.task.agent.core.untrusted_wrap import maybe_wrap, is_untrusted_tool  # noqa: F401
    assert canonical is shimmed


def test_forged_turns_is_pure():
    _assert_pure("core.security.forged_turns")


def test_forged_kinds_identity_across_homes():
    from core.security.forged_turns import FORGED_TURN_KINDS as canonical
    from agents.task.agent.core.self_wake import FORGED_TURN_KINDS as via_self_wake
    assert canonical == via_self_wake == ("self_wake", "delegation_result")
