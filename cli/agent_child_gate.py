"""The owner-only CLI verbs refuse inside a process the agent started.

``core.security.agent_child`` marks every agent child; this module decides, from
the real Click tree, whether an invocation is a READ the agent may run for itself
(``doctor``, ``version``, ``--help``, ``goals list``, ``wallet`` overview, …) or an
owner verb (``wallet export``, ``owner pair``, ``config set``, ``approvals add``,
``run``, the REPL, every surface launcher, …), which it refuses with a plain
message. Allow-listed by READ shape, so a verb added later is owner-only until
someone names it a read.

Called from ``cli.polyrob.main`` (the console-script entry) only — never from the
group callback — so the repo's own ``CliRunner`` tests still run when the agent
runs this test suite under the marker.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import click

from core.security.agent_child import AGENT_CHILD_ENV, is_agent_child

#: Top-level commands that only read and report.
READ_TOP = frozenset({"version", "doctor", "finance", "journey"})

#: Leaf sub-commands that only read (``goals list``, ``owner groups list``, ``config get``
#: — masked —, ``session costs`` …). Anything else under a group is an owner verb.
READ_LEAVES = frozenset({
    "list", "show", "status", "info", "logs", "events", "tree", "search", "explain",
    "path", "check", "get", "pending", "show-pending", "inbox", "asks", "missed",
    "tail", "history", "artifacts", "costs", "tools", "pins", "submissions", "bridges",
    "overview", "templates", "grants", "stats", "doctor", "validate", "permissions",
    "allowlist", "correspondents", "needed", "offers",
})

#: Groups whose BARE call runs a read view (``polyrob wallet`` = the overview).
#: A bare group without ``invoke_without_command`` only prints help, which is a read.
BARE_READ_GROUPS = frozenset({"wallet", "rails", "avatar", "service"})

_VERSION_ONLY = (["-V"], ["--version"])


def _is_help_call(args: List[str]) -> bool:
    """``polyrob [word …] --help`` with NO option before it: then no option can
    take ``--help`` as its value (``run -m --help …``) and no ``--`` can make it a
    positional (``config set K -- --help``)."""
    if args in _VERSION_ONLY:
        return True
    return bool(args) and args[-1] == "--help" and not any(
        a.startswith("-") for a in args[:-1])


def _rest(ctx: click.Context) -> List[str]:
    protected = getattr(ctx, "_protected_args", None)
    if protected is None:
        protected = getattr(ctx, "protected_args", [])
    return [*protected, *ctx.args]


def resolve(argv: Sequence[str]) -> Tuple[Optional[List[str]], Optional[click.Command]]:
    """The canonical command path ``argv`` resolves to and its command, or
    ``(None, None)`` when it cannot be parsed."""
    from cli.polyrob import cli
    try:
        ctx = cli.make_context("polyrob", list(argv), resilient_parsing=True)
        cmd: click.Command = cli
        path: List[str] = []
        while isinstance(cmd, click.Group):
            rest = _rest(ctx)
            if not rest:
                break
            name, sub, tail = cmd.resolve_command(ctx, rest)
            if sub is None:
                return None, None
            path.append(sub.name or name or "")
            ctx = sub.make_context(name, list(tail), parent=ctx, resilient_parsing=True)
            cmd = sub
        return path, cmd
    except Exception:  # noqa: BLE001 — unparseable means "not a known read"
        return None, None


def is_read_invocation(argv: Sequence[str]) -> bool:
    """True when ``argv`` is a help/version call or resolves to a read-only verb."""
    args = list(argv)
    if _is_help_call(args):
        return True
    path, cmd = resolve(args)
    if not path or cmd is None:
        return False  # unparseable, or the bare REPL (an owner turn)
    if path[0] in READ_TOP:
        return True
    if isinstance(cmd, click.Group):
        return (not cmd.invoke_without_command) or (len(path) == 1 and path[0] in BARE_READ_GROUPS)
    return len(path) >= 2 and path[-1] in READ_LEAVES


def refusal(argv: Sequence[str], **probe) -> Optional[str]:
    """The refusal message when an agent-started process asks for an owner verb."""
    if not is_agent_child(**probe):
        return None  # the common case pays one env read (plus /proc on Linux)
    if is_read_invocation(argv):
        return None
    shown = " ".join(argv) or "(the interactive chat)"
    return (f"polyrob: refused `polyrob {shown}` — this process was started by the agent "
            f"({AGENT_CHILD_ENV}), and that is an owner-only command. Run it yourself in "
            f"your own terminal. Read commands (doctor, version, --help, `… list`, "
            f"`… show`, `… status`) stay available to the agent.")


__all__ = ["BARE_READ_GROUPS", "READ_LEAVES", "READ_TOP", "resolve",
           "is_read_invocation", "refusal"]
