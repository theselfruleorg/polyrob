"""057 WS-E — the x-engagement skill's completion rules.

Two defects this pins:

1. A corrupted duplicate bullet (`SKILL.md:96` was a TRUNCATED fragment of the
   line that replaced it — "A saved draft file, a" with no predicate). It had
   been shipped to the model that way.
2. The skill was the ONLY place a proof rule existed, and it covered X alone —
   so Telegram, the room and email had no rule anywhere, which is how a
   delivered channel post came to be reported as unconfirmed.
"""
import pathlib

from core.rails.verification import VERIFICATION

SKILL = (pathlib.Path(__file__).resolve().parents[3]
         / "data" / "prompts" / "skills" / "x-engagement" / "SKILL.md")


def _text():
    return SKILL.read_text(encoding="utf-8")


def test_the_truncated_duplicate_bullet_is_gone():
    text = _text()
    assert "A saved draft file, a\n" not in text
    assert text.count("- **Done =") == 1


def test_no_bullet_is_cut_off():
    """The corruption shape: a bullet ending on a dangling article/conjunction
    with NO continuation line after it. A bullet that merely WRAPS is fine — its
    next line is indented — so the check is about the pair, not the line."""
    lines = _text().splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("- "):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        continued = nxt.startswith(("  ", "\t"))
        if continued:
            continue
        assert not line.rstrip().endswith((" a", " an", " the", " and", " or", ",")), line


def test_a_proof_per_rail_section_cites_the_table():
    text = _text()
    assert "## Proof per rail" in text
    assert "docs/guide/rails-verification.md" in text
    assert "core/rails/verification.py" in text


def test_the_section_names_every_rail_the_agent_can_write_to():
    text = _text().lower()
    for word in ("x post", "telegram channel", "telegram group", "email", "on-chain"):
        assert word in text, word
    # and it carries the channel trap, which is the one that cost a turn
    assert "never delivers them as updates" in text


def test_the_skill_does_not_restate_a_rule_the_table_lacks():
    """The skill is a POINTER, not a second table: every rail it names must have
    a row, so the two cannot drift into disagreement."""
    text = _text()
    assert "Proof per rail" in text
    assert set(VERIFICATION) >= {"x_post", "x_reply", "telegram_channel",
                                 "telegram_group", "email", "onchain"}
