"""What can the OWNER actually do about this blocker, on the surface they use?

2026-09-08, live on prod: a blocked goal told the owner "One action unlocks it:
re-activate the standing Treasury goal with defi_trade granted — e.g.
``goal_update``/requeue on your side." There is no ``goal_update`` verb on any
surface. The owner, who runs this agent from Telegram and not the CLI, was
handed an instruction they could not follow, for a grant that was never
missing. The same day they were also told to run ``polyrob x capture-session``,
to "raise the catastrophic ceiling in guard config", and to hand-apply a patch
file to ``/opt/polyrob/tools/defi/trade_tool.py``.

Two failures, and they need different medicine:

* The agent invents remedies. Nothing checked that a named action was real
  before it reached a human — :func:`unknown_owner_actions` is that check.
* Remedies are written CLI-first for an owner who is not on the CLI. So a
  remedy here is keyed by BLOCKER and valued by SURFACE, and a blocker with no
  chat remedy says so honestly (``none_because``) rather than leaving a hole
  the model will fill with an invention.

Both halves resolve against the EXISTING single sources of truth — the
dispatcher's routing tuple and the CLI's lazy-subcommand map. This module adds
no second list to keep in sync; a verb that stops being routable stops being a
valid remedy in the same commit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import FrozenSet, List, Optional


@dataclass(frozen=True)
class Remedy:
    """What unblocks this, per surface. Empty string = not available there.

    ``none_because`` is REQUIRED when no surface offers a remedy: "nothing you
    can do" is a legitimate answer, "nothing, and I won't say why" is the gap
    the model fills with a fabrication.
    """
    telegram: str = ""
    cli: str = ""
    owner_side: str = ""
    none_because: str = ""


def chat_verbs() -> FrozenSet[str]:
    """Every token the router treats as a command.

    Deliberately the dispatcher's own tuple: a verb absent from it is not a
    command at all — it falls through to the agent as chat text — so a remedy
    naming it would be advice to type something that does nothing.
    """
    from core.surfaces.dispatcher import _COMMANDS

    return frozenset(_COMMANDS)


#: Every registered ``polyrob`` subcommand NAME.
#:
#: Held here rather than read from ``cli.polyrob._LAZY_SUBCOMMANDS`` because
#: ``core`` may not import ``cli`` — the layering ratchet
#: (``tests/test_layering_ratchet.py``) allows upward tier edges only to shrink,
#: and it caught exactly that import while this module was being written. The
#: map's VALUES are import paths and belong to the CLI; only the names are a
#: cross-tier fact. ``tests/unit/cli/test_cli_command_names_ssot.py`` asserts
#: this set equals the live map, so a new subcommand cannot drift out of the
#: remedy vocabulary without a red test.
CLI_COMMAND_NAMES: FrozenSet[str] = frozenset({
    "approvals", "apps", "auth", "autonomy", "browser", "config", "cron",
    "dashboard", "datagen", "discord", "doctor", "email", "finance",
    "gateway", "goals", "identity", "init", "journey", "kb", "keys", "knowledge",
    "model", "models", "owner", "persona", "pfp", "profile", "profiles",
    "run", "serve", "session", "sessions", "signal", "skill", "skills",
    "slack", "soul", "subagents", "surface", "telegram", "todos", "tools",
    "update", "wallet", "webgate", "whatsapp", "x", "x-account",
})


def cli_commands() -> FrozenSet[str]:
    """Every registered ``polyrob`` subcommand name."""
    return CLI_COMMAND_NAMES


def known_chat_verb(token: str) -> bool:
    """Is ``token`` something the router will act on?

    A TAPPABLE token (``/approve_p_1245c6``, ``/approve_all``) is a real verb
    with its argument folded in — `core.surfaces.tappable` is the grammar, and
    the router maps it back before dispatch. Without this the checker flagged
    the framework's OWN tappable remedies as invented and appended a correction
    to every message that offered one.
    """
    bare = token.split("@", 1)[0]
    if bare in chat_verbs():
        return True
    from core.surfaces.tappable import is_tappable_token
    return is_tappable_token(bare)


def known_cli_command(text: str) -> bool:
    """Is ``polyrob <sub> …`` a real command? Only the subcommand is checked;
    flags and arguments are the command's own business."""
    parts = text.strip().split()
    if len(parts) < 2 or parts[0] != "polyrob":
        return False
    return parts[1] in cli_commands()


#: A chat verb: a slash token at a word boundary. Anchored to start-or-space so
#: a filesystem path never reads as a command — "/var/lib/polyrob/reports/x.md"
#: and "/opt/polyrob" are the shapes an honest message uses constantly, and a
#: checker that flagged them would cry wolf until it was ignored.
_VERB_RE = re.compile(r"(?:^|(?<=[\s`(\[]))(/[a-z_]+)\b(?!/)")

#: Absolute-path roots a `/word` token can legitimately BE. The anchored regex
#: already drops "/opt/polyrob" (the trailing slash disqualifies it), but a
#: sentence ending on the root itself — "the tree lives under /opt" — matched,
#: and the checker would have called a real directory an invented verb. A
#: checker that cries wolf is one that gets ignored.
_PATH_ROOTS = frozenset({
    "/opt", "/etc", "/var", "/tmp", "/usr", "/home", "/root", "/srv", "/mnt",
    "/bin", "/sbin", "/lib", "/dev", "/proc", "/sys", "/app", "/data", "/media",
})
_CLI_RE = re.compile(r"(?<![/\w.-])polyrob\s+([a-z][a-z_-]*)")


def unknown_owner_actions(text: str) -> List[str]:
    """Owner actions named in ``text`` that do not exist. Empty = clean.

    Used two ways: as a contract test over shipped templates, and as a
    pre-send check on agent-authored owner messages — which is where the live
    ``goal_update`` came from. Order-preserving and de-duplicated so the caller
    can name the offenders back to the model.
    """
    if not text:
        return []
    bad: List[str] = []
    for verb in _VERB_RE.findall(text):
        if verb in _PATH_ROOTS:
            continue
        if not known_chat_verb(verb) and verb not in bad:
            bad.append(verb)
    known = cli_commands()
    for sub in _CLI_RE.findall(text):
        if sub not in known:
            token = f"polyrob {sub}"
            if token not in bad:
                bad.append(token)
    # The live message wrote the invented verb bare, in backticks, with no
    # slash and no `polyrob` prefix -- `goal_update`. Catch a backticked token
    # that reads like a verb but matches nothing we ship.
    #
    # A TOOL name is not an invented action. `defi_trade`, `web_fetch` and
    # friends are underscore-shaped nouns that an honest message names all the
    # time ("this run carries `defi_trade`"), and flagging them would make the
    # checker cry wolf on exactly the messages it is meant to improve. So the
    # real tool vocabulary is an exclusion, resolved from its own SSOT.
    try:
        from core.tool_capabilities import TOOL_CAPABILITIES
        tool_ids = set(TOOL_CAPABILITIES)
    except Exception:
        tool_ids = set()
    for token in re.findall(r"`([a-z][a-z_]{3,})`", text):
        if ("/" + token) in chat_verbs() or token in known or token in tool_ids:
            continue
        if "_" in token and token not in bad:      # verb_shaped, not prose
            bad.append(token)
    return bad


#: blocker -> what the OWNER does about it. Keys are the verdict kinds a refusal
#: can carry; see ``core.capability_verdict``.
BLOCKER_REMEDIES = {
    "needs_approval": Remedy(
        telegram="/pending, then /approve <id>",
        cli="polyrob owner pending",
        owner_side="",
    ),
    "over_cap": Remedy(
        # Deliberately NOT a chat verb. A compromised chat surface must not be
        # able to raise a spend ceiling; this stays a hands-on-the-box action.
        owner_side=("raise AGENT_WALLET_MAX_PER_TX_USD / WALLET_DAILY_CAP_USD "
                    "in the env file and restart the service"),
    ),
    "paused": Remedy(
        telegram="/resume",
        cli="polyrob autonomy resume",
    ),
    "tool_not_loaded": Remedy(
        owner_side="enable the tool's feature flag in the env file and restart",
    ),
    "not_in_toolset": Remedy(
        owner_side=("grant the tool to a stream leg in data/streams/streams.yaml "
                    "(git-tracked, deployed with the code — never editable from chat)"),
    ),
    "wrong_turn_kind": Remedy(
        none_because=("a forged, delegated or correspondent-tainted turn can never "
                      "reach this verb by design; nothing to unblock"),
    ),
    "credentials_missing": Remedy(
        cli="polyrob auth add <provider>",
        owner_side="top up the provider's credits",
    ),
}


#: The real, always-available places an owner closes a loop from chat. Used as
#: the default replacement when a message named actions that do not exist.
DEFAULT_REAL_OPTIONS = "/help · /pending · /inbox · /status"


def correction_line(text: str, *, real_options: str = "") -> str:
    """One appended line naming the invented actions in ``text``, or ``""``.

    2026-09-15: this check existed and guarded exactly ONE producer (the goal
    escalation). Every other owner-facing message — including the agent's own
    `message` tool, the single most common way it writes to a phone — went out
    unchecked, which is how `polyrob owner pending` kept reaching an owner with
    no shell.

    We do NOT delete the agent's own prose: it is often right about the WHAT and
    wrong only about the remedy, and hiding it would cost more than it saves. We
    label the invented actions and name real ones instead. Fail-open: a broken
    checker must never swallow an owner message.
    """
    try:
        invented = unknown_owner_actions(text)
    except Exception:
        return ""
    if not invented:
        return ""
    named = ", ".join(f"`{a}`" for a in invented)
    return (f"\n⚠️ I named {named} above — those don't exist, ignore them. "
            f"Real options: {real_options or DEFAULT_REAL_OPTIONS}")


#: `polyrob <sub> [verb]` -> the chat verb that does the same thing. Longest
#: match wins, so `owner pending` beats a bare `owner`.
#:
#: This exists because the checker above only asks whether a CLI command is
#: REAL. `polyrob owner pending` is perfectly real — and completely useless to
#: an owner reading a phone, which is the failure that actually shipped, over
#: and over, including in the framework's own pending notice.
CLI_TO_CHAT_VERB = {
    "owner pending": "/pending",
    "owner promote": "/approve",
    "owner reject": "/reject",
    "owner approve": "/approve",
    "owner settle": "/settle",
    "owner inbox": "/inbox",
    "autonomy pause": "/pause",
    "autonomy resume": "/resume",
    "autonomy status": "/status",
    "doctor": "/status",
    "goals": "/goals",
    "wallet": "/wallet",
    "apps": "/apps",
    "cron": "/cron",
    "kb": "/kb",
}

_CLI_CALL_RE = re.compile(r"(?<![/\w.-])polyrob((?:\s+[a-z][a-z_-]*){1,2})")


def cli_calls_named(text: str) -> List[str]:
    """Every ``polyrob …`` invocation named in ``text``, longest form first.

    Order-preserving and de-duplicated, so a caller can quote them back.
    """
    if not text:
        return []
    out: List[str] = []
    for tail in _CLI_CALL_RE.findall(text):
        call = "polyrob" + tail.rstrip()
        if call not in out:
            out.append(call)
    return out


def chat_equivalent(cli_call: str) -> str:
    """The chat verb that replaces ``cli_call``, or ``""`` when there is none.

    An empty answer is a real answer: some things genuinely need a shell, and
    inventing a chat verb for them would be the same defect one layer down.
    """
    tail = str(cli_call or "").strip()
    if tail.startswith("polyrob"):
        tail = tail[len("polyrob"):].strip()
    parts = tail.split()
    for n in (2, 1):
        if len(parts) >= n:
            hit = CLI_TO_CHAT_VERB.get(" ".join(parts[:n]))
            if hit:
                return hit
    return ""


def shell_free_correction(text: str) -> str:
    """One appended line rewriting CLI instructions for a shell-less reader.

    Returns ``""`` when the message named no CLI command.
    """
    calls = cli_calls_named(text)
    if not calls:
        return ""
    swaps, orphans = [], []
    for call in calls:
        chat = chat_equivalent(call)
        (swaps if chat else orphans).append((call, chat))
    parts = []
    if swaps:
        parts.append(", ".join(f"`{c}` → {v}" for c, v in swaps))
    if orphans:
        parts.append(", ".join(f"`{c}` (needs a shell on the box)"
                               for c, _ in orphans))
    return ("\n⚠️ You have no shell in this chat, so ignore the commands above: "
            + "; ".join(parts) + ".")
