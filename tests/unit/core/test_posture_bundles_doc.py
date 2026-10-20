"""The doc's "Posture bundles" table (lifted into core.flags_catalog.DERIVED_FROM)
equals the bundle member sets the code applies. One list per bundle; a member
added in code without the table (or the reverse) fails here."""
from core.flags_catalog import CATALOG, DERIVED_FROM


def _members(label_prefix):
    return {n for n, labels in DERIVED_FROM.items()
            if any(l.startswith(label_prefix) for l in labels)}


def test_bundle_table_matches_code_sets():
    import core.config_policy as cp
    from core.config_policy.builder_mode import _BUILD_FLAGS, _SHIP_FLAGS

    assert _members("POLYROB_LOCAL truthy") == set(cp._SAFE_LOCAL_FLAGS)
    assert _members("POLYROB_LOCAL + AUTONOMY_ENABLED") == set(cp._AUTONOMY_LOCAL_FLAGS)
    assert _members("AUTONOMY_MODE autonomous") == set(cp._MODE_CAPABILITY_FLAGS)
    assert _members("AUTONOMY_POSTURE owner-visible") == set(cp._POSTURE_OWNER_VISIBLE_FLAGS)
    assert _members("AUTONOMY_POSTURE full") == set(cp._POSTURE_FULL_FLAGS)
    assert _members("AGENT_BUILDER_MODE build") == set(_BUILD_FLAGS)
    assert _members("AGENT_BUILDER_MODE ship") == set(_SHIP_FLAGS)


def test_compute_posture_members_match_the_table():
    from core.config_policy.members import COMPUTE_POSTURE_MEMBERS
    assert {n for n, lvl in COMPUTE_POSTURE_MEMBERS.items() if lvl == 1} == \
        _members("AGENT_COMPUTE_POSTURE 1")
    assert {n for n, lvl in COMPUTE_POSTURE_MEMBERS.items() if lvl == 2} == \
        _members("AGENT_COMPUTE_POSTURE 2")


def test_member_set_is_the_table():
    from core.config_policy.members import MEMBER_FLAGS
    assert set(DERIVED_FROM) == set(MEMBER_FLAGS)
    assert len(MEMBER_FLAGS) == 50


def test_no_member_is_a_catalog_row():
    """D7: a posture member has no flag row of its own (one path)."""
    names = {row[0] for row in CATALOG}
    assert not (set(DERIVED_FROM) & names)
    assert "POLYROB_OVERRIDES" in names
