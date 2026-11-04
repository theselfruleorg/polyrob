"""`ci-ok` is the one required status check on `main`: it must need EVERY job.

Branch protection names a check by its exact name. A matrix job reports as
`test (core)`, `test (tools)`, ... and a required `test` then waits forever,
which blocked the release merges. So protection requires only `ci-ok`, and
`ci-ok` needs every other job in `ci.yml`. A job added to `ci.yml` but not to
`ci-ok.needs` would run without gating a merge; this test refuses that.
"""
import pathlib

import yaml

CI = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _jobs():
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def test_ci_ok_needs_every_job():
    jobs = _jobs()
    assert "ci-ok" in jobs, "ci.yml lost the ci-ok job that branch protection requires"
    needs = set(jobs["ci-ok"].get("needs") or [])
    others = set(jobs) - {"ci-ok"}
    assert needs == others, (
        f"ci-ok.needs must list every ci job: missing {sorted(others - needs)}, "
        f"unknown {sorted(needs - others)}")


def test_ci_ok_reports_even_when_a_job_fails():
    job = _jobs()["ci-ok"]
    assert str(job.get("if", "")).strip() == "always()", (
        "ci-ok must run with `if: always()` — a skipped required check reads as passed")
    script = " ".join(str(s.get("run", "")) for s in job.get("steps", []))
    assert "success" in script and "exit 1" in script
