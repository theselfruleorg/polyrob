"""U3 (2026-07-14 review) — systemd manual update steps must target a real unit.

The printed steps hardcoded `polyrob-api` while the live box runs `polyrob.service`
(headless agent) — following them updated code but restarted nothing, leaving old code
running. Steps are now posture-aware: detect which polyrob* units exist and print those
(+ daemon-reload); when detection fails, print both candidates with a caveat.
"""
from cli.commands.update import _parse_unit_files, _systemd_manual_steps


def test_steps_use_detected_unit():
    steps = _systemd_manual_steps(["polyrob.service"])
    assert "polyrob.service" in steps
    assert "daemon-reload" in steps
    assert "polyrob-api" not in steps
    assert "migrations.migrate upgrade" in steps


def test_steps_cover_multiple_detected_units():
    steps = _systemd_manual_steps(["polyrob.service", "polyrob-email.service"])
    assert "polyrob.service" in steps and "polyrob-email.service" in steps
    assert "daemon-reload" in steps


def test_fallback_names_both_candidates_with_caveat():
    steps = _systemd_manual_steps([])
    assert "polyrob.service" in steps
    assert "polyrob-x402-api.service" in steps   # the LIVE api unit, not the retired polyrob-api
    assert "polyrob-api.service" not in steps
    assert "daemon-reload" in steps
    # honest about not knowing which unit runs on this box
    assert "list-unit-files" in steps


def test_steps_never_git_pull_in_the_rsync_target():
    """A deployed /opt/polyrob has no .git; the path is the on-box deployer."""
    steps = _systemd_manual_steps(["polyrob.service"])
    assert "scripts/deploy_prod.sh" in steps
    assert "git pull --ff-only && pip install ." not in steps


def test_parse_unit_files_extracts_service_names():
    out = (
        "polyrob.service            enabled  enabled\n"
        "polyrob-webview.service    enabled  enabled\n"
        "polyrob-email.timer        static   -\n"
        "\n"
    )
    assert _parse_unit_files(out) == ["polyrob.service", "polyrob-webview.service"]


def test_parse_unit_files_empty_output():
    assert _parse_unit_files("") == []


def test_parse_unit_files_skips_bare_template_keeps_instances():
    # `systemctl stop polyrob@.service` is invalid (no instance) and would
    # abort the && chain BEFORE `git pull` — the update would silently no-op.
    out = (
        "polyrob.service     enabled  enabled\n"
        "polyrob@.service    disabled -\n"
        "polyrob@rob.service enabled  enabled\n"
    )
    assert _parse_unit_files(out) == ["polyrob.service", "polyrob@rob.service"]
