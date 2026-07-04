"""Guard tests for the framework rename: server paths /opt/rob -> /opt/polyrob (doc 02, T4/T5).

These are grep-guard tests: they assert the forbidden ``/opt/rob`` literal is absent from the
deploy script, the env templates, and the deployment service units, and that the deploy dir + the
service cross-references are internally consistent. Mechanical-sweep regressions fail here rather
than in a live deploy.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Forbidden legacy path literal. Match /opt/rob as a whole path segment (not /opt/polyrob).
_OPT_ROB = re.compile(r"/opt/rob(?![\w-])")


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text()


def test_deploy_dir_is_polyrob():
    """(a) DEPLOY_DIR in deploy_unified.sh resolves to /opt/polyrob."""
    text = _read("deploy_unified.sh")
    m = re.search(r'^DEPLOY_DIR="([^"]*)"', text, re.MULTILINE)
    assert m is not None, "DEPLOY_DIR assignment not found in deploy_unified.sh"
    assert m.group(1) == "/opt/polyrob", f"DEPLOY_DIR is {m.group(1)!r}, expected /opt/polyrob"


def test_no_opt_rob_literal_in_deploy_and_env():
    """(b) No /opt/rob literal remains in the deploy script or the env templates."""
    for rel in (
        "deploy_unified.sh",
        "config/.env.production",  # gitignored — checked only when present locally
        "config/.env.development.template",
    ):
        if not (REPO_ROOT / rel).exists():
            continue  # gitignored secret file absent on a fresh checkout/CI
        text = _read(rel)
        hits = [
            f"{rel}:{i}: {line.strip()}"
            for i, line in enumerate(text.splitlines(), 1)
            if _OPT_ROB.search(line)
        ]
        assert not hits, "Forbidden /opt/rob literal(s):\n" + "\n".join(hits)


def test_deployment_service_units_have_no_opt_rob_and_valid_xrefs():
    """(c) Every deployment/*.service has no /opt/rob and references only existing sibling units."""
    deployment = REPO_ROOT / "deployment"
    services = sorted(deployment.glob("*.service"))
    assert services, "no *.service units found under deployment/"

    # Well-known systemd targets that are not files we ship.
    KNOWN_TARGETS = {
        "network.target",
        "network-online.target",
        "multi-user.target",
        "default.target",
    }
    present = {p.name for p in services}

    for svc in services:
        text = svc.read_text()
        hits = [
            f"{svc.name}:{i}: {line.strip()}"
            for i, line in enumerate(text.splitlines(), 1)
            if _OPT_ROB.search(line)
        ]
        assert not hits, "Forbidden /opt/rob literal(s):\n" + "\n".join(hits)

        # Collect unit cross-references from After=/Requires=/Wants=/BindsTo=/PartOf=.
        for m in re.finditer(
            r"^(?:After|Requires|Wants|BindsTo|PartOf|Before)=(.*)$", text, re.MULTILINE
        ):
            for ref in m.group(1).split():
                if ref.endswith(".service"):
                    assert (
                        ref in present
                    ), f"{svc.name} references missing sibling unit {ref!r}"
                elif ref.endswith(".target"):
                    assert ref in KNOWN_TARGETS, f"{svc.name} references unknown target {ref!r}"


def test_repo_root_rob_service_removed():
    """(d) The stale repo-root rob.service no longer exists."""
    assert not (REPO_ROOT / "rob.service").exists(), "repo-root rob.service should be deleted"
