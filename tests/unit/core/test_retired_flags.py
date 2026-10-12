"""Retired flags (1.4): a stale env line is reported; an identity/money alias refuses."""
import pytest

from core.config_policy.retired_flags import (
    REFUSING_ALIASES, RETIRED_FLAGS, RetiredFlagError, refuse_retired_alias,
    retired_flag_lines,
)


def test_refusing_aliases_are_retired_flags():
    assert set(REFUSING_ALIASES) <= set(RETIRED_FLAGS)


@pytest.mark.parametrize("alias", sorted(REFUSING_ALIASES))
def test_alias_alone_refuses_and_names_the_rename(alias):
    with pytest.raises(RetiredFlagError) as exc:
        refuse_retired_alias(alias, {alias: "1"})
    assert REFUSING_ALIASES[alias] in str(exc.value)


@pytest.mark.parametrize("alias", sorted(REFUSING_ALIASES))
def test_alias_with_canonical_set_is_not_refused(alias):
    refuse_retired_alias(alias, {alias: "1", REFUSING_ALIASES[alias]: "x"})


def test_false_boolean_alias_is_not_refused():
    refuse_retired_alias("WEBGATE_MULTITENANT", {"WEBGATE_MULTITENANT": "false"})


def test_doctor_lines():
    lines = retired_flag_lines({"MASTER_SEED": "s", "TOOL_SCHEMA_STABLE_ORDER": "true"})
    assert any(l.startswith("FAIL") and "MASTER_SEED" in l for l in lines)
    assert any(l.startswith("WARN") and "TOOL_SCHEMA_STABLE_ORDER" in l for l in lines)
    assert all("s" != l for l in lines)  # never echoes a value
    assert retired_flag_lines({}) == []


def test_doctor_report_names_a_retired_flag():
    from cli.commands.doctor import doctor_report
    lines = doctor_report({"TOOL_SCHEMA_STABLE_ORDER": "true"})
    assert any("TOOL_SCHEMA_STABLE_ORDER" in l and l.startswith("WARN") for l in lines)


def test_retired_names_are_not_catalog_rows():
    from core.flags import REGISTRY
    assert not set(RETIRED_FLAGS) & set(REGISTRY)
