"""`core.owner_remedy.CLI_COMMAND_NAMES` must equal the live CLI command map.

`core` may not import `cli` — the layering ratchet allows upward tier edges only
to shrink, and it caught exactly that import when owner_remedy was written. So
the command NAMES are duplicated into core, and duplication without a contract
test is drift waiting to happen: the first hand-written copy of this list was
missing 10 real commands and invented 7 that do not exist.

This test lives in the cli tier, where importing both sides is legal.
"""
from cli.polyrob import _LAZY_SUBCOMMANDS
from core.owner_remedy import CLI_COMMAND_NAMES


def test_core_knows_every_real_cli_command():
    missing = sorted(set(_LAZY_SUBCOMMANDS) - CLI_COMMAND_NAMES)
    assert not missing, (
        "these commands exist but core does not know them, so an escalation "
        f"naming one would be reported as invented: {missing}")


def test_core_invents_no_cli_command():
    stale = sorted(CLI_COMMAND_NAMES - set(_LAZY_SUBCOMMANDS))
    assert not stale, (
        "core lists commands that do not exist, so the checker would wave "
        f"through an escalation telling the owner to run them: {stale}")
