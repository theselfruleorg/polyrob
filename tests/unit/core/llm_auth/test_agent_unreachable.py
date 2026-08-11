"""024 §7.1 pins: the credential layer is agent-UNREACHABLE, permanently.

1. No module under tools/ or agents/ imports core.llm_auth (source-level AST
   scan, lazy imports included — same discipline as the layering ratchet).
2. The agent-callable config surface reaches ``config_service.explain`` ONLY —
   no path from an agent action to ``set_value``/``_set_flag`` (verified live
   2026-08-06; this test keeps it true by construction, not by accident).
3. The scrubber twins both redact JWT-shaped OAuth tokens (divergence guard).
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def _imports_of(py: Path):
    try:
        tree = ast.parse(py.read_text(errors="ignore"), filename=str(py))
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name


def test_no_agent_tier_module_imports_llm_auth():
    offenders = []
    for tier in ("tools", "agents"):
        for py in (ROOT / tier).rglob("*.py"):
            for mod in _imports_of(py):
                if mod == "core.llm_auth" or mod.startswith("core.llm_auth."):
                    offenders.append((py.relative_to(ROOT).as_posix(), mod))
    assert not offenders, (
        "Agent-tier module(s) import the credential layer — 024 §7.1 makes this "
        f"a PERMANENT non-goal (owner/CLI surface only):\n{offenders}"
    )


def test_agent_config_surface_is_read_only():
    src = (ROOT / "tools/controller/action_registration.py").read_text()
    assert "config_service" in src  # the read surface exists (explain)
    for forbidden in ("set_value", "_set_flag"):
        assert forbidden not in src, (
            f"action_registration.py references config_service.{forbidden} — the "
            "agent config surface must stay read-only (explain) per 024 §2.6/§7.1(4)."
        )


_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJyb2IiLCJleHAiOjF9.c2lnbmF0dXJlLXNlZ21lbnQ"


def test_both_scrubbers_redact_jwts():
    from cli.ui.secrets import scrub_secrets
    from core.secret_scrub import scrub_secret_shapes

    for scrub in (scrub_secret_shapes, scrub_secrets):
        out = scrub(f"token is {_JWT} ok")
        assert _JWT not in out, scrub.__module__
        assert "redacted" in out.lower()


def test_jwt_pattern_is_shared_not_forked():
    """The JWT rung must reach the CLI twin through the ONE shared battery
    (core.secret_patterns.apply_ssot_shapes) — a re-definition or a dropped
    rung is the documented divergence bug class."""
    import cli.ui.secrets as cli_secrets
    import core.secret_patterns as patterns

    assert cli_secrets.apply_ssot_shapes is patterns.apply_ssot_shapes
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJyb2IifQ.c2ln"
    assert jwt not in cli_secrets.scrub_secrets(f"t {jwt} x")
