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
    assert "polyrob-api.service" in steps
    assert "daemon-reload" in steps
    # honest about not knowing which unit runs on this box
    assert "list-unit-files" in steps


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
