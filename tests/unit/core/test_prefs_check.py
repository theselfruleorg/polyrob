"""core.prefs catalog cross-checking: check_env_files + find_invalid_preferences
(owner-UX P1 T7). Reusable core-layer checkers behind ``/config check`` and the
planned ``polyrob config check`` (P1 T8)."""

from core.prefs import (
    catalog_lookup,
    check_env_files,
    find_invalid_preferences,
    shape_of_default,
    value_matches_shape,
    write_preference,
)


# ---------------------------------------------------------------------------
# shape_of_default / value_matches_shape
# ---------------------------------------------------------------------------


def test_shape_of_default_bool():
    assert shape_of_default("OFF") == "bool"
    assert shape_of_default("**ON**") == "bool"
    assert shape_of_default("ON (`\"1\"`)") == "bool"  # bool checked before numeric


def test_shape_of_default_numeric():
    assert shape_of_default("`30`") == "numeric"
    assert shape_of_default("`0.20`") == "numeric"


def test_shape_of_default_free():
    assert shape_of_default("unset") == "free"
    assert shape_of_default("`local_subprocess`") == "free"


def test_value_matches_shape():
    assert value_matches_shape("true", "bool")
    assert value_matches_shape("0", "bool")
    assert not value_matches_shape("soon", "numeric")
    assert value_matches_shape("30", "numeric")
    assert value_matches_shape("anything at all", "free")


# ---------------------------------------------------------------------------
# catalog_lookup
# ---------------------------------------------------------------------------


def test_catalog_lookup_exact_match():
    hit = catalog_lookup("GOAL_DAILY_QUOTA")
    assert hit is not None
    group, documented_default = hit
    assert documented_default == "`6`"


def test_catalog_lookup_unknown_returns_none():
    assert catalog_lookup("TOTALLY_MADE_UP_FLAG_XYZ") is None


def test_catalog_lookup_pattern_match(monkeypatch):
    """Dynamic `<...>` catalog entries are matched by regex, not equality."""
    monkeypatch.setattr(
        "core.prefs.CATALOG",
        [("POLYROB_<PROVIDER>_MODEL", "LLM / providers", "unset")],
    )
    hit = catalog_lookup("POLYROB_OPENAI_MODEL")
    assert hit is not None
    assert hit[0] == "LLM / providers"
    assert catalog_lookup("POLYROB_MODEL") is None  # doesn't degenerately match


# ---------------------------------------------------------------------------
# check_env_files
# ---------------------------------------------------------------------------


def test_check_env_files_missing_paths_is_empty(tmp_path):
    assert check_env_files([tmp_path / "nope.env"]) == []


def test_check_env_files_flags_typo_with_suggestion(tmp_path):
    envfile = tmp_path / ".env"
    envfile.write_text("GOAL_DAILY_QOUTA=4\n")
    findings = check_env_files([envfile])
    assert len(findings) == 1
    assert "GOAL_DAILY_QOUTA" in findings[0]
    assert "GOAL_DAILY_QUOTA" in findings[0]


def test_check_env_files_flags_shape_mismatch(tmp_path):
    envfile = tmp_path / ".env"
    # SUB_AGENTS_ENABLED documents an ON/OFF-shaped default.
    envfile.write_text("SUB_AGENTS_ENABLED=banana\n")
    findings = check_env_files([envfile])
    assert len(findings) == 1
    assert "SUB_AGENTS_ENABLED" in findings[0]
    assert "shape" in findings[0]


def test_check_env_files_accepts_valid_values(tmp_path):
    envfile = tmp_path / ".env"
    envfile.write_text("SUB_AGENTS_ENABLED=true\nGOAL_DAILY_QUOTA=6\n")
    assert check_env_files([envfile]) == []


def test_check_env_files_unknown_with_no_suggestion_is_info_level(tmp_path):
    envfile = tmp_path / ".env"
    envfile.write_text("MY_PROVIDER_SECRET_TOKEN_XYZ=sk-abc123\n")
    findings = check_env_files([envfile])
    assert len(findings) == 1
    assert "unknown (not in catalog)" in findings[0]
    assert "sk-abc123" not in findings[0]  # never print secret VALUES


def test_check_env_files_later_path_overrides_earlier(tmp_path):
    """Merging semantics mirror project-overrides-global elsewhere: the same
    key in two files is only reported once (from the later file's value)."""
    global_env = tmp_path / "global.env"
    project_env = tmp_path / "project.env"
    global_env.write_text("GOAL_DAILY_QUOTA=6\n")       # valid
    project_env.write_text("GOAL_DAILY_QUOTA=oops\n")   # invalid, wins
    findings = check_env_files([global_env, project_env])
    assert len(findings) == 1
    assert "GOAL_DAILY_QUOTA" in findings[0]


# ---------------------------------------------------------------------------
# find_invalid_preferences
# ---------------------------------------------------------------------------


def test_find_invalid_preferences_empty_when_no_file(tmp_path):
    assert find_invalid_preferences(tmp_path, "u1") == []


def test_find_invalid_preferences_reports_unknown_key(tmp_path):
    from core.prefs import preferences_path
    write_preference(tmp_path, "u1", "goals.daily_quota", 4)
    p = preferences_path(tmp_path, "u1")
    p.write_text(p.read_text() + "\n[junk]\nnope = 1\n")
    invalid = find_invalid_preferences(tmp_path, "u1")
    assert any(key == "junk.nope" for key, _err in invalid)


def test_find_invalid_preferences_reports_bad_value(tmp_path):
    from core.prefs import preferences_path
    p = preferences_path(tmp_path, "u1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[style]\nverbosity = "shouty"\n')
    invalid = find_invalid_preferences(tmp_path, "u1")
    assert any(key == "style.verbosity" for key, _err in invalid)


def test_find_invalid_preferences_redacts_long_value(tmp_path):
    """owner-UX P1 final review (item 6): a hand-pasted long value (e.g. a
    secret mistakenly written to a bool-typed key) must never be echoed in
    full via the validation error `/config check`/`find_invalid_preferences`
    surfaces — only a short prefix."""
    from core.prefs import preferences_path
    p = preferences_path(tmp_path, "u1")
    p.parent.mkdir(parents=True, exist_ok=True)
    long_secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    p.write_text(f'[digest]\nenabled = "{long_secret}"\n')
    invalid = find_invalid_preferences(tmp_path, "u1")
    assert len(invalid) == 1
    key, err = invalid[0]
    assert key == "digest.enabled"
    assert long_secret not in err
    assert "sk-a" in err
