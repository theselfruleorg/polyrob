"""030 WS-E5: the effective-posture card — one builder, all capability axes."""
import pytest

import agents.task.constants as constants
from core.config_policy import builder_mode
from core.config_policy.posture_card import build_posture_card, render_posture_card


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("POLYROB_LOCAL", "AUTONOMY_ENABLED", "AUTONOMY_MODE",
              "AUTONOMY_POSTURE", "AGENT_COMPUTE_POSTURE", "POLYROB_POSTURE",
              "AUTONOMY_HALT", "POLYROB_OWNER_USER_ID",
              "AGENT_BUILDER_MODE", "APP_SERVICE_BASE_DOMAIN", "APP_SERVICE_CERT_DIR"):
        monkeypatch.delenv(k, raising=False)
    constants._refreeze_compute_posture_for_tests()
    builder_mode.reset_builder_mode_warnings()
    yield
    constants._refreeze_compute_posture_for_tests()
    builder_mode.reset_builder_mode_warnings()


def _axes(rows):
    return {r["axis"]: r for r in rows}


def test_card_covers_all_five_bundles_plus_owner_pause():
    """058 WS-3: the card documents itself as covering ALL capability axes and
    carried four of the five named default bundles — AGENT_BUILDER_MODE, the
    one that decides whether the agent may put software on the internet, was
    absent from every seat that renders this."""
    axes = _axes(build_posture_card())
    for want in ("trust profile (local mode)", "autonomy master",
                 "capability mode", "loop posture", "compute posture",
                 "builder mode", "owner pause"):
        assert want in axes, f"missing axis: {want}"


def test_defaults_read_as_defaults():
    axes = _axes(build_posture_card())
    assert axes["capability mode"]["effective"] == "supervised"
    assert axes["capability mode"]["source"] == "default"
    assert axes["compute posture"]["effective"] == 0


def test_clamped_autonomous_mode_is_called_out(monkeypatch):
    # autonomous requested but no POLYROB_LOCAL/owner binding -> clamp is VISIBLE
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    axes = _axes(build_posture_card())
    assert axes["capability mode"]["effective"] == "supervised"
    assert "CLAMPED" in axes["capability mode"]["note"]


def test_inert_compute_posture_env_is_called_out(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    # frozen value stays whatever import froze (0 in the clean test env)
    axes = _axes(build_posture_card())
    if axes["compute posture"]["effective"] != 2:
        assert "INERT" in axes["compute posture"]["note"]


def test_render_is_one_line_per_axis():
    rows = build_posture_card()
    lines = render_posture_card(rows)
    assert len(lines) == len(rows)
    assert all(":" in ln and "[" in ln for ln in lines)


# --- 058 WS-3: builder mode + the knob count ------------------------------- #

def test_builder_mode_row_defaults_to_off():
    row = _axes(build_posture_card())["builder mode"]
    assert row["env"] == "AGENT_BUILDER_MODE"
    assert row["effective"] == "off"
    assert row["source"] == "default"
    assert row["note"] == ""


def test_builder_mode_reads_env(monkeypatch):
    monkeypatch.setenv("AGENT_BUILDER_MODE", "build")
    row = _axes(build_posture_card())["builder mode"]
    assert row["effective"] == "build"
    assert row["source"] == "env"


def test_ship_clamp_is_named(monkeypatch):
    """A ship instance clamped to build for want of a domain must SAY so. A card
    reporting `ship` while app_service refuses every call is the confident-and-
    wrong class the status SSOT exists to prevent."""
    monkeypatch.setenv("AGENT_BUILDER_MODE", "ship")
    row = _axes(build_posture_card())["builder mode"]
    assert row["effective"] == "build", "the clamp must show the EFFECTIVE mode"
    assert "CLAMPED" in row["note"] and "APP_SERVICE_BASE_DOMAIN" in row["note"]


def test_ship_granted_when_domain_and_cert_exist(monkeypatch, tmp_path):
    cert_dir = tmp_path / "live" / "example.test"
    cert_dir.mkdir(parents=True)
    (cert_dir / "fullchain.pem").write_text("x")
    monkeypatch.setenv("AGENT_BUILDER_MODE", "ship")
    monkeypatch.setenv("APP_SERVICE_BASE_DOMAIN", "example.test")
    monkeypatch.setenv("APP_SERVICE_CERT_DIR", str(cert_dir))
    row = _axes(build_posture_card())["builder mode"]
    assert row["effective"] == "ship" and row["note"] == ""


def test_knob_count_row_points_at_the_bundles():
    """The card's last row tells an operator who has never opened
    CONFIGURATION.md that the bundles above are the intended surface."""
    rows = build_posture_card()
    knobs = rows[-1]
    assert knobs["axis"] == "knobs"
    from core.flags_catalog import CATALOG
    assert str(len(CATALOG)) in knobs["effective"]
    assert "doctor --flags" in knobs["note"]


def test_a_broken_axis_degrades_rather_than_crashing(monkeypatch):
    """_row wraps every resolver; a raising builder-mode resolver must render
    'unknown', never take the whole card (and every status seat) down."""
    import core.config_policy.posture_card as pc

    def _boom():
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(pc, "effective_builder_mode", _boom, raising=False)
    row = _axes(build_posture_card())["builder mode"]
    assert row["effective"] == "unknown"
