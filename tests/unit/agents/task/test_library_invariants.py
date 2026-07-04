"""Library invariants — standing gate that every shipped skill must satisfy.

Three checks:
(a) Every auto_activate rule in rules.json has a SKILL.md body on disk.
    A bodiless auto_activate rule is a silent failure: the manager matches it,
    then drops it with only a WARNING log when the body is empty.
(b) No shipped SKILL.md uses the retired ``mcp_execute_tool`` verb.
    The current anysite shape is ``anysite_api(endpoint=..., params={...})``;
    see docs/SKILL_AUTHORING_STANDARD.md.
(c) No shipped SKILL.md contains an obvious hardcoded secret pattern
    (OpenAI key, AWS access key, or PEM private key header).
"""
from pathlib import Path
import json
import re

from agents.task.agent.skill_frontmatter import parse_frontmatter
from agents.task.agent.skill_validation import validate_authored

BASE = Path(__file__).resolve().parents[4] / "data" / "prompts" / "skills"

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),          # OpenAI / Anthropic API keys
    re.compile(r"AKIA[0-9A-Z]{16}"),              # AWS access key IDs
    re.compile(r"-----BEGIN .{0,30}PRIVATE KEY-----"),  # PEM private keys
]


def test_every_auto_activate_rule_has_a_body():
    rules = json.loads((BASE / "rules.json").read_text())
    for sid, r in rules.items():
        if r.get("auto_activate"):
            assert (BASE / sid / "SKILL.md").exists(), f"{sid} auto_activate but no body"


def test_no_dead_anysite_shape_in_bodies():
    for md in BASE.glob("*/SKILL.md"):
        t = md.read_text()
        assert "mcp_execute_tool" not in t, f"{md} uses retired mcp_execute_tool"


def test_no_obvious_secrets_in_bodies():
    for md in BASE.glob("*/SKILL.md"):
        t = md.read_text()
        for pattern in _SECRET_PATTERNS:
            match = pattern.search(t)
            assert match is None, (
                f"{md} appears to contain a hardcoded secret matching {pattern.pattern!r}: "
                f"...{match.group()[:12]}..."
            )


def test_every_bundled_skill_passes_strict_authored_validation():
    """(d) Standing CI gate (Task 4): every shipped SKILL.md must pass the strict
    ``validate_authored`` agentskills.io compliance check — not just the ad-hoc
    field assertions in ``test_skill_compliance_library.py``. Fail-closed (a plain
    assert, no try/except) so drift in the bundled library breaks CI immediately.
    """
    for md in BASE.glob("*/SKILL.md"):
        meta, _ = parse_frontmatter(md.read_text())
        errors = [i for i in validate_authored(meta, md.parent.name) if i.level == "error"]
        assert errors == [], f"{md.parent.name}: {errors}"
