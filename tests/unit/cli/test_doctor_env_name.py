"""Security audit 2026-11-21, L-24: `polyrob doctor` prints the environment name the ONE
predicates resolve (core.env.is_production_env / is_development_env), not the env-file
name (CONFIG_ENV/ENV, defaulting to "development"). A box named only by ENVIRONMENT or
PRODUCTION read "resolved env: development" while every production refusal was on."""
import pytest

from cli.commands.doctor import doctor_report

_NAMES = ("ENVIRONMENT", "POLYROB_ENV", "CONFIG_ENV", "ENV", "PRODUCTION")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for n in _NAMES:
        monkeypatch.delenv(n, raising=False)


def _head(env=None):
    return doctor_report(env or {})[0]


@pytest.mark.parametrize("name,value", [("ENVIRONMENT", "production"), ("PRODUCTION", "true"),
                                        ("POLYROB_ENV", "prod")])
def test_a_box_named_production_reads_production(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert _head().endswith("resolved env: production")


def test_the_mapping_config_env_still_counts():
    assert _head({"CONFIG_ENV": "production"}).endswith("resolved env: production")


def test_an_explicit_development_name_reads_development(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert _head().endswith("resolved env: development")


def test_an_unnamed_box_is_not_called_development():
    head = _head()
    assert "resolved env: unnamed" in head
    assert "development" not in head.split("resolved env:")[1].split("(")[0]
