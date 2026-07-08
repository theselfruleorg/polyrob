"""`polyrob journey` — Click command shares the h_journey renderer."""
from click.testing import CliRunner

from cli.commands.journey import journey


def test_journey_cli_invokes_renderer(monkeypatch):
    import cli.ui.commands.h_journey as hj
    monkeypatch.setattr(hj, "render_journey",
                        lambda **kw: f"TL[{kw['user_id']}|{kw['since_label']}]")
    monkeypatch.setattr("cli.commands.journey.resolve_identity", lambda: "rob",
                        raising=False)
    r = CliRunner().invoke(journey, ["--since", "24h", "--user", "rob"])
    assert r.exit_code == 0
    assert "TL[rob|24h]" in r.output


def test_journey_cli_default_since(monkeypatch):
    captured = {}
    import cli.ui.commands.h_journey as hj
    monkeypatch.setattr(hj, "render_journey",
                        lambda **kw: captured.update(kw) or "ok")
    monkeypatch.setattr("cli.commands.journey.resolve_identity", lambda: "rob",
                        raising=False)
    r = CliRunner().invoke(journey, [])
    assert r.exit_code == 0
    assert captured["since_label"] == "7d"
