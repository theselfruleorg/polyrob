"""3.10 (O2/O3/O4, 2026-07-14 review) — init wizard polish regressions."""
import inspect
import re


def test_wizard_section_numbering_is_consistent():
    """O2: sections number 1/6..6/6 — no stale /4 or /5 denominators, and the
    Owner pairing section is numbered like the rest."""
    import cli.commands.init as m
    src = inspect.getsource(m)
    denominators = set(re.findall(r"Section \d/(\d)", src))
    assert denominators == {"6"}, f"mixed wizard denominators: {denominators}"
    assert "Section 5/6: Owner pairing" in src


def test_closing_key_check_reads_all_env_layers():
    """O3: the closing 'no usable key' warning must consider every layer
    `polyrob run` honors (load_env), not just the file init wrote."""
    import cli.commands.init as m
    src = inspect.getsource(m.init_cmd.callback)
    assert "load_env" in src


def test_deepseek_prompt_carries_bootstrap_hint(monkeypatch):
    """O4: a non-initializable provider's key prompt says it can't bootstrap alone."""
    import click
    import cli.commands.init as m

    prompts = []
    answers = iter(["deepseek", ""])

    def _fake_prompt(text, **kwargs):
        prompts.append(text)
        return next(answers, "skip")

    monkeypatch.setattr(click, "prompt", _fake_prompt)
    monkeypatch.setattr(click, "confirm", lambda *a, **k: False)
    m._prompt_provider_keys({})

    from modules.llm.profiles import all_profiles
    # Only providers init actually PROMPTS for can carry a prompt hint. Some
    # non-initializable rows are never prompted (openai-codex: transport
    # RESPONSES has no client yet, and it is an OAuth seat with no key to ask
    # for), so requiring a prompt for every one of them tests nothing real.
    non_init = [p for p in all_profiles()
                if not p.initializable and p.env_key and p.prompt_in_init]
    assert non_init, "expected at least one prompted non-initializable profile (deepseek)"
    for prof in non_init:
        matching = [t for t in prompts if prof.display_name in t]
        assert matching and "can't bootstrap alone" in matching[0], (
            f"{prof.name}: prompt lacks the bootstrap hint")
    # initializable providers must NOT carry the hint
    init_prompts = [t for t in prompts
                    if all(prof.display_name not in t for prof in non_init)]
    assert all("can't bootstrap alone" not in t for t in init_prompts)
