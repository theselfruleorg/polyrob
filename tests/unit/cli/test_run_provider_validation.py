"""`polyrob run -p <unknown>` fails with a named Known: list, not a traceback
(UX assessment 2026-08-07, Q10 — mirrors `model set-default`'s treatment)."""
from click.testing import CliRunner


def _output(res):
    out = res.output
    try:
        out += res.stderr or ""
    except (AttributeError, ValueError):
        pass
    return out


def test_run_unknown_provider_names_known_set(monkeypatch):
    monkeypatch.setattr("core.bootstrap.load_env", lambda **k: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "a" * 32)

    from cli.commands.run import run

    res = CliRunner().invoke(run, ["-p", "nonexist", "say hi"])
    out = _output(res)
    assert res.exit_code == 1, f"{out!r} exc={res.exception!r}"
    assert "Unknown provider 'nonexist'" in out
    assert "Known:" in out
    assert "openrouter" in out


def test_run_known_provider_passes_validation(monkeypatch):
    """A valid provider must get PAST the validation. The bootstrap-suppression
    hook (which runs after it) is patched to raise, so the test stops there —
    either way no 'Unknown provider' refusal may appear."""
    monkeypatch.setattr("core.bootstrap.load_env", lambda **k: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "a" * 32)

    def _boom(*a, **k):
        raise RuntimeError("stopped-after-validation")

    monkeypatch.setattr("cli.commands._bootstrap.suppress_bootstrap_output", _boom)
    from cli.commands.run import run

    res = CliRunner().invoke(run, ["-p", "openrouter", "say hi"])
    assert "Unknown provider" not in _output(res)
    assert isinstance(res.exception, (RuntimeError, SystemExit))
