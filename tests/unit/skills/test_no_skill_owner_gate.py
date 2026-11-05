"""103: a shipped skill does not add an owner gate the runtime does not know."""
import re
import subprocess

#: The runtime's own waits (money, a new domain, a tainted turn) have a card and may be
#: described. The gate this catches is a wait the skill itself invents.
_GATE = re.compile(r"approvals?\.jsonl|approval (?:file|log)\b"
                   r"|(?:post|publish|send) only after (?:the )?owner", re.IGNORECASE)


def _skills():
    out = subprocess.run(["git", "ls-files", "*SKILL.md"], capture_output=True, text=True,
                         check=True).stdout
    return out.split()


def test_there_are_skills_to_check():
    assert _skills()


def test_no_shipped_skill_makes_the_agent_wait_on_a_file_or_a_yes():
    hits = {}
    for path in _skills():
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        found = _GATE.findall(text)
        if found:
            hits[path] = found[:3]
    assert not hits, (f"owner gates in skills {hits}: raise a card with propose_action "
                      "(docs/SKILL_AUTHORING_STANDARD.md section 4a)")
