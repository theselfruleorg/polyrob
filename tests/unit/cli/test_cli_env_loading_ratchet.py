"""026 P1.2 — every CLI command module must load the env-file ladder.

Failure mode C in proposal 026: 14 command modules never called ``load_env``,
so a value written by ``polyrob config set`` was invisible to them (`polyrob
cron` warned "CRON_ENABLED is off" right after the owner turned it on). The
seam is ``cli.commands._bootstrap.ensure_env_loaded`` — a memoized
``load_env(local_mode=True)`` called from each command group's callback.

This is a SHRINK-ONLY ratchet (house pattern): a module may satisfy it via any
of the accepted seams; the exempt set may only shrink, never grow.
"""
from pathlib import Path

COMMANDS_DIR = Path(__file__).resolve().parents[3] / "cli" / "commands"

#: Any one of these markers proves the module loads the env ladder before it
#: reads flags/credentials (directly, or via a helper that does).
SEAM_MARKERS = (
    "ensure_env_loaded",      # the 026 P1.2 seam
    "load_env(",              # direct core.bootstrap.load_env call
    "preflight_or_onboard",   # cli.keys preflight (loads env internally)
    "cli_container(",         # _bootstrap.cli_container (preflight inside)
    "run_surface(",           # _surface_runner envelope (preflight inside)
)

#: Modules exempt from the seam requirement. SHRINK-ONLY for command modules —
#: never add a row for a module that registers commands.
#: - __init__/_errors/_grouped: structural, no command entry points that read
#:   flags (_grouped is the GroupedGroup help-rendering class, 030 WS-C5 D7).
#: (auth.py was exempt while its parallel-session work was in flight on
#: 2026-08-14; wired + removed 2026-08-15.)
EXEMPT = {
    "__init__.py",
    "_errors.py",
    "_grouped.py",
}


def test_every_command_module_loads_env():
    missing = []
    for path in sorted(COMMANDS_DIR.glob("*.py")):
        if path.name in EXEMPT:
            continue
        src = path.read_text(encoding="utf-8")
        if not any(marker in src for marker in SEAM_MARKERS):
            missing.append(path.name)
    assert not missing, (
        "CLI command modules that never load the env-file ladder (their verbs "
        "cannot see `polyrob config set` values): "
        f"{missing} — call cli.commands._bootstrap.ensure_env_loaded() in the "
        "group callback."
    )


def test_exempt_rows_still_exist():
    # A deleted module must leave the ratchet (keeps EXEMPT honest).
    for name in EXEMPT:
        assert (COMMANDS_DIR / name).exists(), f"stale EXEMPT row: {name}"


def test_ensure_env_loaded_is_memoized(monkeypatch):
    import cli.commands._bootstrap as bootstrap
    calls = []
    monkeypatch.setattr(bootstrap, "_env_loaded", False)

    def _fake_load_env(*a, **k):
        calls.append((a, k))
        return "development"

    import core.bootstrap as core_bootstrap
    monkeypatch.setattr(core_bootstrap, "load_env", _fake_load_env)
    bootstrap.ensure_env_loaded()
    bootstrap.ensure_env_loaded()
    assert len(calls) == 1
    assert calls[0][1] == {"local_mode": True}
