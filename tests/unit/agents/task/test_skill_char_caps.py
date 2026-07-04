"""Task 7 - split the single flat skill char cap into two thresholds.

The old ``MAX_SKILL_CONTENT_CHARS = 12000`` was a HARD reject in
``validate_skill_content`` (used by the skill-writer's ``create_skill`` gate and
by import/validation paths), which silently rejected spec-valid agentskills.io
skills well within the ~5000-token injected-body recommendation - e.g. the real
anthropics/skills ``docx`` skill (20084 chars) and ``skill-creator`` (33168
chars) both bust the old 12000 cap.

This introduces:
- ``MAX_SKILL_FILE_CHARS`` (40000) - the on-disk ceiling (DoS guard); the only
  hard reject.
- ``MAX_SKILL_INJECT_CHARS`` (20000) - a soft, warn-only threshold for the body
  actually injected into the prompt.
- ``validate_skill_content_length(body) -> (bool, str)`` - the pure helper that
  encodes the (MIN, MAX_SKILL_FILE_CHARS) hard-reject window.
"""
from agents.task.agent import skill_manager as sm


def test_large_spec_valid_body_is_accepted():
    """~20k chars, like anthropics/skills docx (20084 chars) - previously
    rejected outright by the old flat 12000-char cap; must now be accepted
    since it is well under the new 40000-char on-disk ceiling."""
    big = "# Big\n" + ("x " * 10000)  # ~20006 chars
    assert len(big) > 12000, "test body should exceed the OLD cap to be meaningful"
    ok, msg = sm.validate_skill_content_length(big)
    assert ok, msg


def test_over_file_ceiling_is_rejected():
    """A body over MAX_SKILL_FILE_CHARS (40000) is still a hard reject - the
    on-disk DoS guard is not removed, just raised and separated from the
    injected-body recommendation."""
    huge = "# Huge\n" + ("x " * 21000)  # > 40000 chars
    assert len(huge) > sm.MAX_SKILL_FILE_CHARS
    ok, msg = sm.validate_skill_content_length(huge)
    assert not ok
    assert "too large" in msg


def test_under_min_is_rejected():
    """A body under MIN_SKILL_CONTENT_CHARS is rejected by the pure helper
    (MIN_SKILL_CONTENT_CHARS itself is unchanged - see the separate
    ``validate_skill_content`` behavior test below for how this composes with
    the existing warning-only handling at the method level)."""
    tiny = "hi"
    assert len(tiny) < sm.MIN_SKILL_CONTENT_CHARS
    ok, msg = sm.validate_skill_content_length(tiny)
    assert not ok
    assert "too short" in msg


def test_boundary_values_are_accepted():
    """Exact boundary values (MIN and MAX_SKILL_FILE_CHARS) are inclusive."""
    at_min = "x" * sm.MIN_SKILL_CONTENT_CHARS
    ok, msg = sm.validate_skill_content_length(at_min)
    assert ok, msg

    at_max = "x" * sm.MAX_SKILL_FILE_CHARS
    ok, msg = sm.validate_skill_content_length(at_max)
    assert ok, msg


def test_max_inject_chars_is_smaller_than_max_file_chars():
    """The two caps must actually be split (not collapsed back into one) -
    otherwise a large-but-spec-valid skill could still be silently truncated
    or over-warned at the wrong threshold."""
    assert sm.MAX_SKILL_INJECT_CHARS < sm.MAX_SKILL_FILE_CHARS


# --- Integration: the reject site in validate_skill_content actually swapped ---

def test_validate_skill_content_accepts_docx_sized_body():
    """The SkillManager.validate_skill_content method (the real gate used by
    SkillWriterMixin.create_skill for authored/imported skills) must accept
    the same ~20k docx-sized body, not just the standalone pure helper."""
    manager = sm.SkillManager()
    big = "# Big\n" + ("x " * 10000)
    result = manager.validate_skill_content("big-skill", big)
    assert result.is_valid, f"unexpected errors: {result.errors}"


def test_validate_skill_content_rejects_over_file_ceiling():
    """validate_skill_content still hard-rejects past the (raised) on-disk
    ceiling - the DoS guard is preserved, just at the new threshold."""
    manager = sm.SkillManager()
    huge = "# Huge\n" + ("x " * 21000)
    result = manager.validate_skill_content("huge-skill", huge)
    assert not result.is_valid
    assert any("too large" in e for e in result.errors)


def test_min_behavior_unchanged_short_content_is_warning_not_reject():
    """Task 7 only raises/splits the MAX side. MIN_SKILL_CONTENT_CHARS behavior
    at the validate_skill_content method level is unchanged: short (but
    non-empty) content is a WARNING, not a hard reject."""
    manager = sm.SkillManager()
    short = "# Hi"  # well under MIN_SKILL_CONTENT_CHARS (50), non-empty
    result = manager.validate_skill_content("short-skill", short)
    assert result.is_valid, f"short content should warn, not reject: {result.errors}"
    assert any("short" in w.lower() for w in result.warnings)


# --- Secondary: warn (not reject) at the injection-assembly site ---

def test_format_skills_for_prompt_warns_on_oversized_inject_body(caplog):
    """format_skills_for_prompt (where matched-skill bodies are assembled into
    the pinned prompt content) logs a warning for a body over
    MAX_SKILL_INJECT_CHARS, but still includes it in full - no truncation, no
    rejection."""
    manager = sm.SkillManager()
    big_body = "# Big\n" + ("x " * 10001)  # > MAX_SKILL_INJECT_CHARS (20000)
    assert len(big_body) > sm.MAX_SKILL_INJECT_CHARS
    matched = [sm.MatchedSkill(skill_id="big-skill", priority=5, match_reasons=[],
                                content=big_body)]

    with caplog.at_level("WARNING"):
        formatted = manager.format_skills_for_prompt(matched)

    assert big_body in formatted, "oversized body must still be injected in full"
    assert any("big-skill" in r.message for r in caplog.records), (
        "expected a warning mentioning the oversized skill id"
    )
