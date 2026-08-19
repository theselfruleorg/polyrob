"""W2.3: now that `polyrob auth add` handles key-based providers, the two
discovery surfaces (doctor's "not configured" roll-up and init's
deferred-provider footer) must name it — otherwise the connect verb exists
but nothing points at it.
"""
import click

from cli.commands.doctor import doctor_report


def test_doctor_not_configured_line_names_auth_add():
    lines = doctor_report({"ZAI_API_KEY": "sk-zai-0123456789abcdefgh"})
    line = next(ln for ln in lines if "not configured:" in ln)
    assert "polyrob auth add" in line


def test_init_deferred_footer_names_auth_add(monkeypatch, capsys):
    from cli.commands.init import _prompt_provider_keys

    monkeypatch.setattr(click, "prompt", lambda *a, **k: "")
    _prompt_provider_keys({})
    out = capsys.readouterr().out
    assert "more providers are available" in out
    assert "polyrob auth add" in out
