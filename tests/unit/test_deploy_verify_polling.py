"""Deploy scripts must POLL the post-restart verify check, not fixed-sleep-then-check-once.

Finding (2026-08-18, docs/ops/inbox.md, LOW): scripts/deploy_prod.sh's `sleep 12` +
one-shot "autonomy loop started" journal check false-positive-rolled-back known-good
code when the service's boot legitimately took ~12-14s (this box's 472+ real sessions
push it right to the edge of the fixed window) — live-observed: the new process
reached "autonomy loop started" essentially the same instant the check ran, then got
killed 2s later by the rollback. Fixed by polling every 2s up to 30s total in both
deploy_prod.sh (local) and deploy_from_local.sh (remote, via one SSH round-trip
anchored to a remote-captured timestamp to avoid clock skew) instead of a single
sleep-then-check. Verified functionally (not just statically) against the live
service: an already-past RESTART_TS is found within one 2s poll; a RESTART_TS with no
matching new log line correctly times out at ~30s (not hung, not instant-fail).
"""
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    # scripts/ is publish-denylisted — on a public/pip tree these files don't
    # exist, and a shipped test must skip, not fail (the 0.10.0 CI lesson).
    path = _REPO / rel
    if not path.exists():
        pytest.skip(f"{rel} not shipped in this tree (private ops tooling)")
    return path.read_text()


def test_deploy_prod_polls_instead_of_fixed_sleep():
    text = _read("scripts/deploy_prod.sh")
    # A bare `sleep 12` *statement* (not a comment referencing the old behavior)
    # must be gone from the primary restart/verify path.
    code_lines = [l for l in text.splitlines() if not l.strip().startswith("#")]
    assert not any(l.strip() == "sleep 12" for l in code_lines), \
        "the old fixed-sleep verify window must be gone"
    assert "for _ in $(seq 1 15)" in text
    assert 'VERIFY_OK=1' in text and 'VERIFY_OK=0' in text


def test_deploy_from_local_polls_instead_of_fixed_sleep():
    text = _read("scripts/deploy_from_local.sh")
    # The restart line no longer bundles a fixed sleep before the is-active check.
    assert "sleep 12; systemctl is-active" not in text
    assert 'for i in \\$(seq 1 15)' in text
    assert "RESTART_TS" in text


def test_both_deploy_scripts_are_valid_bash():
    import subprocess
    for rel in ("scripts/deploy_prod.sh", "scripts/deploy_from_local.sh"):
        result = subprocess.run(
            ["bash", "-n", str(_REPO / rel)], capture_output=True, text=True
        )
        assert result.returncode == 0, f"{rel}: {result.stderr}"
