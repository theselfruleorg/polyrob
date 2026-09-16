"""043 A31: `polyrob doctor` names the A2A agent-card mount and the OpenAI-compat
`/v1` state — honest over env. Both surfaces live on the API server (never the CLI
process), so doctor reports the CONFIGURED state, never a bare "reachable", and a
flag-off `/v1` must never render as reachable/enabled."""
import importlib

from cli.commands.doctor import a2a_v1_line, doctor_report

FLAG = "OPENAI_COMPAT_API_ENABLED"
CARD_PATH = "/.well-known/agent.json"


def _joined(env):
    return "\n".join(doctor_report(env))


def test_line_names_the_agent_card_path_and_v1():
    """The A2A agent-card path and the /v1 surface are both named."""
    line = a2a_v1_line({})
    assert CARD_PATH in line
    assert "/v1" in line


def test_doctor_report_includes_the_line():
    out = _joined({})
    assert CARD_PATH in out
    assert "/v1 OpenAI-compat" in out


def test_v1_flag_off_is_honest():
    """Flag off/unset: /v1 reads 'off' with its flag name and is NEVER enabled/reachable."""
    line = a2a_v1_line({})
    assert f"/v1 OpenAI-compat — off ({FLAG})" in line
    assert "enabled when serving" not in line
    assert "reachable" not in line.lower()


def test_v1_flag_on_reads_enabled():
    line = a2a_v1_line({FLAG: "true"})
    assert "/v1 OpenAI-compat — enabled when serving" in line
    # A2A card has no flag: it is always mounted-when-serving, never off.
    assert f"A2A agent card {CARD_PATH} — mounted when the API server runs" in line


def test_a2a_never_claims_bare_reachable():
    """Doctor is a static/env report, not a network probe — it must not assert the
    surfaces are live-reachable (only 'served when the API server runs')."""
    for env in ({}, {FLAG: "true"}):
        line = a2a_v1_line(env)
        assert "not this process" in line
        assert "reachable" not in line.lower()


def test_absent_when_server_extra_missing(monkeypatch):
    """Without the [server] extra the API app can't build → both surfaces 'absent',
    but the paths are still named so the check stays legible."""
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *a, **k):
        if name in ("fastapi", "uvicorn"):
            return None
        return real_find_spec(name, *a, **k)

    monkeypatch.setattr("cli.commands.doctor.importlib.util.find_spec", fake_find_spec)
    line = a2a_v1_line({FLAG: "true"})
    assert CARD_PATH in line
    assert "/v1" in line
    assert "absent" in line
    assert "[server] extra" in line
    # Flag on but unservable must NOT read as enabled/reachable.
    assert "enabled when serving" not in line
