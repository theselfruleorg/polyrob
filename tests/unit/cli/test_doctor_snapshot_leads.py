"""043 A13 — plain `polyrob doctor` leads with the status snapshot.

Before this change, `doctor` printed the raw check transcript (provider
credentials, memory backend, skill compliance, ...) first — the same
"is anything wrong" headline every other seat (`polyrob autonomy status`,
Telegram /status, the webview /system page) renders via
``core.status_render.render_status_lines`` was buried, if shown at all. Now
plain `doctor` prints that snapshot first, and the full check transcript only
prints under `--full` (default: replaced by one pointer line).
"""
from click.testing import CliRunner


def test_doctor_snapshot_leads(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.polyrob import cli

    result = CliRunner().invoke(cli, ["doctor"])
    assert result.exit_code == 0, result.output
    out = [ln for ln in result.output.splitlines() if ln.strip()]
    assert out, "doctor produced no output"
    assert out[0].startswith(("▶", "⏸")) or "Health:" in out[0]


def test_doctor_plain_hides_transcript_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.polyrob import cli

    result = CliRunner().invoke(cli, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "run `polyrob doctor --full` for every check" in result.output
    # The check-transcript detail (skill compliance line) stays out of the
    # default view.
    assert "skills:" not in result.output


def test_doctor_full_contains_the_check_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.polyrob import cli

    result = CliRunner().invoke(cli, ["doctor", "--full"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any(ln.startswith("provider credentials") for ln in lines)


def test_doctor_json_gains_a_status_key(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.polyrob import cli

    result = CliRunner().invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload.get("report"), list) and payload["report"]
    assert isinstance(payload.get("status"), list) and payload["status"]


def test_doctor_flags_mode_is_unaffected():
    from cli.polyrob import cli

    result = CliRunner().invoke(cli, ["doctor", "--flags"],
                                env={"ANTHROPIC_API_KEY": "sk-x"})
    assert result.exit_code == 0, result.output
    assert "resolved flags" in result.output
    assert "run `polyrob doctor --full`" not in result.output


# --------------------------------------------------------------------------- #
# 043 A13 fix round 1 (Important 1): with NO provider credential at all,
# core/status_snapshot.py's own CRIT (`_providers_section`, `if live is None
# and usable`) never fires — it only triggers when usable credentials exist
# but none is live (all credit-dead). A box with zero credentials has an
# empty `usable` set, so the snapshot alone reads a clean "Health: OK" lie.
# The default view must surface the SAME remedy doctor_report's transcript
# prints, even though that transcript itself now lives behind --full.
# --------------------------------------------------------------------------- #

def _isolate_home_and_cwd(tmp_path, monkeypatch):
    """`doctor()` calls `load_env(local_mode=True)`, which re-reads
    `./.polyrob`/`~/.polyrob/.env` after a bare ``monkeypatch.delenv`` — on a
    dev box with real credentials configured there, that re-populates the
    exact vars a test just cleared (reproduced live: a first attempt at this
    test leaked the operator's actual `anthropic`/`gemini`/`zai-coding`
    credentials from `~/.polyrob/.env` into the run). Point HOME and cwd at
    empty tmp dirs — the same isolation `test_doctor_command_sees_production_
    only_key` (test_doctor_cmd.py) already uses."""
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                "GOOGLE_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
                "NVIDIA_API_KEY", "PERPLEXITY_API_KEY", "CONFIG_ENV", "ENV",
                # ⚠️ A profile is a WHOLE isolated home (core/profiles.py): with
                # POLYROB_PROFILE set, `activate_profile` overwrites both
                # POLYROB_HOME and POLYROB_DATA_DIR, so the tmp isolation below
                # is silently discarded and the run reads the developer's own
                # profile. POLYROB_HOME does the same on its own.
                "POLYROB_HOME", "POLYROB_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)


def test_doctor_default_view_surfaces_missing_credential(tmp_path, monkeypatch):
    _isolate_home_and_cwd(tmp_path, monkeypatch)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.polyrob import cli

    result = CliRunner().invoke(cli, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "no provider credential found" in result.output
    assert "polyrob init" in result.output


def test_doctor_default_view_stays_quiet_with_a_usable_credential(tmp_path, monkeypatch):
    # The gate must not fire (no false-positive remedy line) once a usable
    # provider credential exists.
    _isolate_home_and_cwd(tmp_path, monkeypatch)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.polyrob import cli

    result = CliRunner().invoke(cli, ["doctor"], env={"ANTHROPIC_API_KEY": "sk-ant-" + "x" * 40})
    assert result.exit_code == 0, result.output
    assert "no provider credential found" not in result.output
