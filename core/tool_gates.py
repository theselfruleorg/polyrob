"""Tool enable gates — the ONE gate module (067 P1).

Two halves, one module:

1. **Static declaration** — :class:`ToolGate`, carried as the ``gate`` field of the
   per-tool rows in ``core/tool_capabilities.py``. It names the flag and the
   agent-facing disclosure (label, tier ``loadable|disabled|reserved``, remedy).
   ``tools.controller.tool_load_report.TOOL_GATE_FLAGS`` and
   ``agents.task.agent.core.tool_availability.GATED_TOOL_REGISTRY`` are VIEWS of it.
2. **Runtime predicate** — ``tool_id -> () -> bool``. ``core`` and ``agents`` may not
   import ``tools`` (``tests/test_layering_ratchet.py``), yet several readers there
   need a tool's live enable gate (is ``anysite`` on? is ``hf_deploy`` on?). The gate
   function stays next to its tool; the tool module registers it here at import
   (:func:`register_gate`), and the lower tiers look it up by tool id.
   ``core.tool_capabilities`` re-exports these functions; the store lives here.

3. **Remedy text** (081 B-1) — :data:`REMEDIES` is the ONE home of the text that
   tells the agent how a gated tool is unblocked. A gate row (here or in a
   ``pack.toml``) names a KEY, never prose, and every reader renders the text
   with :func:`remedy_text`: the ``<tool-availability>`` block
   (``agents/task/agent/core/tool_availability.py``) and the ``<tool-catalog>`` /
   ``load_tool`` / not-loaded refusals (``tools/tool_disclosure.py``). So one tool
   has one gate text on every surface.

``import tools`` loads every registrar (``tools/__init__.py``). A reader that runs
with the tool tier not loaded gets ``None`` from :func:`gate_for`; it must treat
that as "the tool is not available", never as enabled.
"""
import re
from dataclasses import dataclass
from typing import Callable, Dict, Optional

Gate = Callable[[], bool]

#: ``ToolGate.tier`` values (the <tool-availability> vocabulary).
GATE_TIERS = frozenset({"loadable", "disabled", "reserved"})


#: key -> the remedy text. ``{tool}``, ``{flag}`` and ``{set_flag}`` (the env line
#: the owner writes for ``{flag}``) are filled by :func:`remedy_text`.
REMEDIES: Dict[str, str] = {
    # -- a gate row's key (the <tool-availability> block and the catalog) --------
    "set_flag": "operator runs {set_flag}",
    "load_tool": 'call load_tool("{tool}") to use it',
    "compute_posture": ("operator raises AGENT_COMPUTE_POSTURE "
                        "(host axis; not part of AUTONOMY_MODE)"),
    "compute_flag": "operator runs {set_flag} — raising the posture changes nothing here",
    "compute_session": ("not in this session's toolset — goal/cron runs carry the compute "
                        "tools only at posture>=1 with the flag on; otherwise run it in an "
                        "interactive (owner) session or raise ONE ask"),
    "cron_posture": ("operator raises AUTONOMY_POSTURE or sets "
                     "`polyrob config set CRON_ENABLED true`"),
    "mcp_servers": ("operator runs {set_flag}, then the owner adds a server with "
                    "/mcp add <id> <https-url>"),
    # 093 A1/B2: the flag is on but no server is configured — MCPTool is off.
    "no-mcp-server": ("MCP_ENABLED is on, but no MCP server is configured, so the tool "
                      "is off. The owner adds one with /mcp add <id> <https-url> (it "
                      "works from the next turn); the agent cannot add it itself, and a "
                      "restart alone changes nothing"),
    # 093 A3: the exec backend cannot start for this process (prod: docker socket).
    "backend-unavailable": ("the exec backend ({flag}) cannot start here, so every call "
                            "fails. The operator sets a working CODE_EXEC_BACKEND "
                            "(`polyrob doctor` run as the agent's user lists the working "
                            "ones); a retry or a restart alone "
                            "changes nothing"),
    "email_credentials": "operator configures email creds; sends follow outbound.policy",
    "x402_invoice": "operator runs {set_flag} (auto-ON in autonomous mode)",
    "x_credentials": ("operator configures X user credentials; "
                      "`polyrob config set TWITTER_ENABLED true` unlocks writes"),
    "browser_install": ("ask the owner to install the browser service on this host "
                        "(the browser step of the self-hosting guide)"),
    "browser_service": ("ask the owner to check the browser service "
                        "(polyrob-browser.service) on this host"),
    "x_browser_session": ("operator enables X_BROWSER_ENABLED and runs "
                          "`polyrob x-account capture-session`"),
    # ⚠️ Do NOT reintroduce "request it on the goal payload" in "money". A goal the
    # agent creates itself can never carry a money tool: goal_create filters the
    # whole money-SPEND set out (tools/goal_tools.py), deliberately, so an injected
    # goal cannot launder itself into a trade. Naming that as the remedy sent the
    # prod agent round a loop ~50 times in two weeks. ⚠️ ...unless the money
    # regime is ARMED ("money_armed"): then the self-goal ceiling holds the tool
    # and goal_create keeps it (prod 2026-10-12: four trading goals written without
    # defi_trade, every one blocked). tools/tool_disclosure.py picks the key with
    # the one predicate, ``allowed_self_goal_tools``.
    "money": (
        "these tools are explicit-grant-only: load_tool never loads one. A goal you "
        "create yourself can NEVER carry one — goal_create strips them by "
        "design, so do not retry by adding it to your own goal payload. Only the "
        "owner/operator grants it: on a goal THEY seeded, or at session creation. If "
        "you have a candidate and no grant, record it where the granted run will read "
        "it and say so plainly — then escalate ONCE, not every run."),
    "money_armed": (
        "these tools never load mid-run (load_tool never loads one). On this instance "
        "the money regime is ARMED, so a goal you create CAN carry it: "
        "goal_create(..., rig='money_rail') or tools=[..., '{tool}']. This run lacks "
        "it because its goal did not list it — create the goal with it (caps, "
        "simulation and the pause still bound every spend)."),
    # Appended by the <tool-catalog> / load_tool / not-loaded refusal to a money
    # gate, never by the <tool-availability> block. A room or correspondent seat
    # never sees it: its seat probe gates every money tool first (095 W2).
    "money_owner_move": ("When the OWNER asks for a money move, call propose_action: "
                         "the move runs from their seat."),
    # -- the catalog's own statuses (tools/tool_disclosure.py) -------------------
    "leaf-blocked": ("not available to delegated sub-agents — report back and let "
                     "the parent agent run this itself"),
    "autonomous-ceiling": ("not in this autonomous run's self-service tool ceiling; "
                           "the owner must grant it explicitly"),
    "needs-config": "needs config: {flag} — message the owner / file an ops ask",
    "not-constructed": ("not constructed in this deploy shape — a deploy that builds "
                        "it is needed; tell the owner / file an ops ask"),
    # -- 095 W2: load-path reasons the catalog and the load report share --------
    "pack-not-loaded": ("provided by the pack '{pack}', which is {state}; operator "
                        "enables it with `polyrob pack enable {pack}`, the agent cannot"),
    "disabled-by-flag": ("operator runs {set_flag}: the tool is not registered on this "
                         "deploy, and the agent cannot enable it"),
    "load-failed": ("the container has it but it did not initialize — tell the owner "
                    "the error; a retry changes nothing"),
    "wallet_owner": "only the bound owner may load a wallet tool on this instance",
    "owner_only": ("this tool acts as the instance's own identity (account, mailbox, "
                   "credentials or public URL); only the owner tenant may load it"),
    "autonomous_wait": ("an autonomous run cannot wait for an answer; the message was "
                        "sent and the run continues"),
    "wallet-off": ("operator turns the agent wallet on (AGENT_WALLET_ENABLED): it is off "
                   "on this instance, so every call of this tool fails"),
    "tool-off": ("operator checks why the tool did not start (`polyrob doctor`): a "
                 "required service is missing or its setup failed, so every call fails"),
    "seat-room": ("not on this seat: a public room only reads and replies; the owner "
                  "does it from their private chat"),
    "seat-correspondent": ("not on this seat: a correspondent's session takes no "
                           "high-impact action; the owner does it from their own chat"),
    # 057 WS-A credential verdict texts (``tools.tool_disclosure.CREDENTIAL_GATED_TOOLS``
    # rows and a pack's ``register_credential_gate`` name these keys).
    "smtp-rejected": ("operator fixes the SMTP credentials (GMAIL_* / EMAIL_*) or "
                      "switches EMAIL_PROVIDER to agentmail: the server rejected them "
                      "(535). Do not retry this rail until then — say so and use "
                      "another channel."),
    "api-credits": ("operator tops up the API credits: the API refused this account "
                    "(402 — credits). Until then another rail (a browser rail) carries "
                    "the post."),
    "unknown-tool": "not a known tool id — see <tool-catalog> for the valid ids",
    # -- the tail of an approval refusal with no owner ask (095 W2) --------------
    "approval_money": ("this is a money verb: the owner decides it — the live caps, and "
                       "whether any spend may skip the owner at all, are in the Spend "
                       "limits block of <live-health> — do not tell the owner a "
                       "preference will unblock it"),
    "approval_pref": "the owner can loosen it via the approvals.require pref",
}


#: 093 C1 — WHO can act on a gate. ``agent`` (the agent itself), ``owner-verb``
#: (the owner types or taps a seat verb), ``operator-env`` (the person who runs
#: the host: the env file, a backend, a unit), ``deploy`` (a code/deploy change).
#: ⚠️ The actor word of a :data:`REMEDIES` text agrees with its row: a text that
#: names an env-only fix says "operator", never "owner" (a seat's ``/config set``
#: refuses every flag those texts name). Ratcheted.
ACTORS = ("agent", "owner-verb", "operator-env", "deploy")
#: 093 C1 — WHEN a fix applies once it is made.
APPLIES = ("now", "next-turn", "new-session", "restart", "deploy")


@dataclass(frozen=True)
class RemedyActor:
    """Who fixes a gate, when the fix applies, and the verb or line they use."""
    who: str
    when: str
    verb: str = ""

    def __post_init__(self):
        if self.who not in ACTORS:
            raise ValueError(f"who={self.who!r} not in {ACTORS}")
        if self.when not in APPLIES:
            raise ValueError(f"when={self.when!r} not in {APPLIES}")


#: 093 C1 — the ONE who/when table, next to the remedy text it explains. Keyed
#: by every :data:`REMEDIES` key AND every gate reason
#: ``tools.tool_disclosure.resolve_tool_status`` can return (a reason with no
#: REMEDIES key of its own: its remedy text comes from the gate). Ratcheted by
#: ``tests/unit/core/test_093_remedy_actors.py``. The operations-manual skill
#: (``data/prompts/skills/harness-manual``) is generated from this table.
REMEDY_ACTORS: Dict[str, RemedyActor] = {
    # -- REMEDIES keys --------------------------------------------------------
    "set_flag": RemedyActor("operator-env", "restart", "polyrob config set <FLAG> true"),
    "load_tool": RemedyActor("agent", "now", 'load_tool("<id>")'),
    "compute_posture": RemedyActor("operator-env", "restart", "AGENT_COMPUTE_POSTURE=<n>"),
    "compute_flag": RemedyActor("operator-env", "restart", "polyrob config set <FLAG> true"),
    "compute_session": RemedyActor("owner-verb", "new-session",
                                   "run it from an owner chat, or raise ONE owner_ask"),
    "cron_posture": RemedyActor("operator-env", "restart", "polyrob config set CRON_ENABLED true"),
    # MCP_ENABLED is a posture member: a direct MCP_ENABLED= line is ignored.
    "mcp_servers": RemedyActor("operator-env", "restart", "polyrob config set MCP_ENABLED true"),
    "no-mcp-server": RemedyActor("owner-verb", "next-turn", "/mcp add <id> <https-url>"),
    "backend-unavailable": RemedyActor("operator-env", "restart", "CODE_EXEC_BACKEND=<backend>"),
    "email_credentials": RemedyActor("operator-env", "restart", "the email credentials"),
    "x402_invoice": RemedyActor("operator-env", "restart", "polyrob config set <FLAG> true"),
    "x_credentials": RemedyActor("operator-env", "restart", "the X user credentials"),
    "browser_install": RemedyActor("operator-env", "restart", "install the browser service"),
    "browser_service": RemedyActor("operator-env", "restart", "polyrob-browser.service"),
    "x_browser_session": RemedyActor("operator-env", "new-session",
                                     "polyrob x-account capture-session"),
    "money": RemedyActor("owner-verb", "new-session", "/goal grant <id> tools=<tool>"),
    "money_armed": RemedyActor("agent", "new-session", "goal_create(..., rig='money_rail')"),
    "money_owner_move": RemedyActor("owner-verb", "now", "propose_action"),
    "leaf-blocked": RemedyActor("agent", "now", "report to the parent agent"),
    "autonomous-ceiling": RemedyActor("owner-verb", "new-session",
                                      "/goal grant <id> tools=<tool>"),
    "needs-config": RemedyActor("operator-env", "restart", "the named config"),
    "not-constructed": RemedyActor("deploy", "deploy", "a deploy shape that builds it"),
    "unknown-tool": RemedyActor("agent", "now", "a valid id from <tool-catalog>"),
    "pack-not-loaded": RemedyActor("operator-env", "restart", "polyrob pack enable <pack>"),
    "disabled-by-flag": RemedyActor("operator-env", "restart", "polyrob config set <FLAG> true"),
    "load-failed": RemedyActor("deploy", "deploy", "a fix for the init error"),
    "wallet_owner": RemedyActor("owner-verb", "now", "the owner's own session"),
    "owner_only": RemedyActor("owner-verb", "now", "the owner's own session"),
    "autonomous_wait": RemedyActor("agent", "now", "continue; read the answer next run"),
    "wallet-off": RemedyActor("operator-env", "restart", "AGENT_WALLET_ENABLED=true"),
    "tool-off": RemedyActor("operator-env", "restart", "polyrob doctor"),
    "seat-room": RemedyActor("owner-verb", "now", "the owner's private chat"),
    "seat-correspondent": RemedyActor("owner-verb", "now", "the owner's own chat"),
    "smtp-rejected": RemedyActor("operator-env", "restart", "the SMTP credentials"),
    "api-credits": RemedyActor("operator-env", "restart", "the API credits"),
    "approval_money": RemedyActor("owner-verb", "now", "the owner's one-shot approval"),
    "approval_pref": RemedyActor("owner-verb", "next-turn",
                                  "the approvals.require preference (/prefs)"),
    # -- gate reasons with no REMEDIES key of their own -----------------------
    "unavailable-on-this-deploy": RemedyActor("operator-env", "restart", "the named config"),
    "custody-no-browser": RemedyActor("operator-env", "restart", "the remote browser rail"),
    "credentials-rejected": RemedyActor("operator-env", "restart", "the rejected credentials"),
}


#: 093 C1 — every ``gated:<reason>`` the ONE resolver
#: (``tools.tool_disclosure.resolve_tool_status``) can return, including a
#: constructed tool's own ``availability_gap`` reason. Ratcheted against the
#: resolver's literals; every one has a :data:`REMEDY_ACTORS` row.
STATUS_REASONS = ("money", "leaf-blocked", "autonomous-ceiling", "unavailable-on-this-deploy",
                  "not-constructed", "custody-no-browser", "credentials-rejected",
                  "unknown-tool", "no-mcp-server", "backend-unavailable",
                  "pack-not-loaded", "disabled-by-flag", "load-failed", "wallet_owner",
                  "owner_only", "autonomous_wait", "wallet-off", "tool-off", "seat-room",
                  "seat-correspondent")


def _template_pattern(template: str) -> "re.Pattern":
    parts = re.split(r"\{[a-z_]+\}", template)
    return re.compile(".*?".join(re.escape(p) for p in parts), re.DOTALL)


def remedy_key_for_text(text: str) -> Optional[str]:
    """The :data:`REMEDIES` key whose rendered text appears in *text*, else None.

    A gate status carries the rendered remedy, not its key (the catalog line
    shows prose); this reads the key back from the ONE text home, so no second
    status field exists. The longest template wins (``money`` vs a shorter
    prefix)."""
    text = str(text or "")
    if not text:
        return None
    for key in sorted(REMEDIES, key=lambda k: -len(REMEDIES[k])):
        if _template_pattern(REMEDIES[key]).search(text):
            return key
    return None


def remedy_actor(reason: str = "", remedy: str = "") -> Optional[RemedyActor]:
    """Who/when for a gate: by the remedy text's key first (the most specific
    fact), then by the gate reason. None for an unknown reason."""
    key = remedy_key_for_text(remedy)
    if key is not None and key in REMEDY_ACTORS:
        return REMEDY_ACTORS[key]
    return REMEDY_ACTORS.get(str(reason or ""))


class _Facts(dict):
    def __missing__(self, name):
        return f"<{name}>"


def remedy_text(key: str, *, tool: str = "", flag: str = "",
                set_flag: Optional[str] = None, **facts) -> str:
    """The remedy text for *key*. ``KeyError`` on an unknown key: a gate row is
    validated at construction, so a miss here is a code bug, not a config.

    ``set_flag`` is the command for ``flag``: ``polyrob config set FLAG true``,
    which MERGES a posture member into ``POLYROB_OVERRIDES`` (095 W2 — a bare
    ``POLYROB_OVERRIDES=FLAG=true`` line would replace every other member)."""
    if set_flag is None:
        set_flag = f"`polyrob config set {flag} true`" if flag else ""
    return REMEDIES[key].format_map(_Facts(tool=tool, flag=flag, set_flag=set_flag, **facts))


@dataclass(frozen=True)
class RefusalKind:
    """A RUN-TIME refusal (095 W2): what a pre-tool hook answers when it says no.

    ``slug`` is the ``core.security.refusals`` reason it counts under (``""`` =
    not counted); the kind itself is the typed cause, recorded as a detail, so
    one slug never merges several causes. ``actor`` is the same who/when/lever
    row the load-time table uses. ``text`` is what the model reads (it says who
    can act and what the agent does now — never an owner-only act as the
    agent's own); ``owner_text`` is what a person reads. Both take facts by
    ``{name}``; a text that names ``/pending`` must take ``{pending_id}``."""
    slug: str
    actor: RemedyActor
    retryable: bool
    text: str
    owner_text: str = ""


def _k(slug, who, when, lever, retryable, text, owner_text=""):
    return RefusalKind(slug, RemedyActor(who, when, lever), retryable, text, owner_text)


_RUN_ANOTHER_WAY = "Do it another way, or leave it for an owner turn"
_AP = "approval denied for '{action}'"
_AP_NO_ASK = "the owner was not asked"

#: 095 W2 — the ONE run-time refusal table, beside the load-time one. Every
#: pre-tool hook renders its refusal from here with :func:`refusal_text`;
#: ``tests/unit/core/test_refusal_kinds_ratchet.py`` holds the rules.
REFUSAL_KINDS: Dict[str, RefusalKind] = {
    # -- the approval hook (tools/controller/approval.py) ----------------------
    "approval:denied_by_owner": _k(
        "approval_denied", "owner-verb", "now", "none — final", False,
        _AP + ": the owner declined; do not retry this call",
        "You declined '{action}'."),
    "approval:pending": _k(
        "approval_denied", "owner-verb", "now", "/pending {pending_id}", True,
        _AP + " (pending): the owner has not answered yet; the ask {pending_id} is in "
        "/pending — an approval recorded later applies to the next identical attempt "
        "(one-shot grant)",
        "'{action}' waits for your answer: /pending {pending_id}."),
    "approval:timeout": _k(
        "approval_timeout", "owner-verb", "now", "/pending {pending_id}", True,
        _AP + " (timeout): the owner did not answer in time; the ask {pending_id} is in "
        "/pending — an approval recorded later applies to the next identical attempt "
        "(one-shot grant)",
        "'{action}' timed out; the ask is still in /pending {pending_id}."),
    "approval:timeout_no_ask": _k(
        "approval_timeout", "owner-verb", "now", "{lever}", True,
        _AP + " (timeout): nobody answered and no owner ask was filed; {remedy}"),
    "approval:crashed": _k(
        "approval_denied", "operator-env", "restart", "the approval provider", True,
        _AP + " (error: approval provider crashed): " + _AP_NO_ASK + "; {remedy}"),
    "approval:refused": _k(
        "approval_denied", "owner-verb", "now", "{lever}", False,
        _AP + ": the approval provider refused without asking the owner; {remedy}"),
    "approval_not_created:forged": _k(
        "approval_denied", "owner-verb", "now", "the owner runs it from their chat", False,
        _AP + ": this turn may not ask the owner (not the owner's own turn); only the "
        "owner, from their own chat, can run it"),
    "approval_not_created:tainted": _k(
        "approval_denied", "owner-verb", "now", "the owner runs it from their chat", False,
        _AP + ": this turn carries a correspondent's message, so it may not ask the "
        "owner; only the owner, from their own chat, can run it"),
    "approval_not_created:store_down": _k(
        "approval_denied", "agent", "now", "retry later", True,
        _AP + ": the approvals store could not be read, so " + _AP_NO_ASK + "; retry later"),
    "approval_not_created:no_provider": _k(
        "approval_denied", "operator-env", "restart", "APPROVAL_PROVIDER", False,
        _AP + ": this deploy's approval provider asks no one and denies every gated "
        "call; the operator sets APPROVAL_PROVIDER"),
    "approval_not_created:money_owner": _k(
        "approval_denied", "owner-verb", "now", "the owner runs it from their chat", False,
        _AP + ": a money verb needs the bound owner identity and this caller is not "
        "the owner"),
    "approval_not_created:busy": _k(
        "approval_denied", "owner-verb", "now", "the owner answers the open prompt", True,
        _AP + ": another approval prompt is already open; it needs an answer first, "
        "then retry"),
    "approval_not_created:duplicate": _k(
        "approval_denied", "agent", "now", "none — final", False,
        _AP + ": the owner's one approval was already used by an identical call; "
        "nothing more ran"),
    "approval_not_created:closed": _k(
        "approval_denied", "agent", "now", "retry to ask again", True,
        _AP + ": the ask was closed without an approval; retry to ask again"),
    # -- the session gates ------------------------------------------------------
    "seat:room": _k(
        "room_toolset", "owner-verb", "now", "the owner runs it from their private chat",
        False,
        "'{action}' is not available in a group chat: this session speaks into a "
        "public room and can only read and reply. Only the owner, from their private "
        "chat with me, can do it"),
    "seat:room_grant": _k(
        "room_toolset", "owner-verb", "now", "the owner changes this room's grants", False,
        "'{action}' refused in this group chat: {detail}. Nothing was done"),
    "seat:correspondent": _k(
        "correspondent_taint", "owner-verb", "next-turn",
        "the owner's own message (or they run it from their chat)", False,
        "'{action}' is blocked: the latest input is untrusted correspondent DATA, so no "
        "high-impact action runs on this turn; it needs the owner's own message"),
    # -- the money kernel (core/money/authorize.py) -----------------------------
    "money:forged_text": _k(
        "forged_turn", "owner-verb", "now", "the owner runs it from their chat", False,
        "refused: a forged/autonomous turn (self-wake, delegation-result, leaf, or an "
        "autonomous run this instance does not admit) cannot move funds"),
    "money:autonomous_unarmed": _k(
        "forged_turn", "operator-env", "restart",
        "the money regime and DEFI_AUTONOMOUS_TURN_TRADING", False,
        "refused: a forged/autonomous turn — this autonomous run cannot move funds "
        "because unattended money is not armed on this instance"),
    "money:gate": _k(
        "money_gate", "owner-verb", "now", "the owner's caps or their one-shot approval",
        False, "refused by the money gate: {why}"),
    # -- the controller hooks ---------------------------------------------------
    "denylist": _k(
        "", "owner-verb", "next-turn",
        "the owner's approvals.deny preference (/prefs) or POLYROB_TOOL_DENYLIST", False,
        "'{action}' is on the tool denylist"),
    "paused": _k(
        "pause", "owner-verb", "now", "/resume", False,
        "'{action}' is an autonomous {effect} write and autonomy is {state}; only the "
        "owner resumes it"),
    "strict_code_change": _k(
        "", "deploy", "deploy", "a row in core/effects.py WRITE_ACTIONS or "
        "NON_WRITE_ACTIONS", False,
        "'{action}' is an unclassified {effect} write (EXTERNAL_WRITE_STRICT), so it "
        "does not run until a code change classifies it"),
    "retry_wall": _k(
        "", "agent", "next-turn", "change the call, or the owner's next message", False,
        "you made the SAME call {attempts} times in this turn and it failed each time. "
        "Last error: {last_error} — The tool is not broken, locked or blocked: this "
        "limit counts only your identical call, and only the owner's next message "
        "resets it. Change the parameter the last error names, or stop and tell the "
        "owner that last error. Do not repeat this call"),
    "unattended:goal": _k(
        "approval_denied", "owner-verb", "now", "an owner turn", False,
        "refused: this command {reason} in an unattended goal run; {why}. No owner is "
        "present on a goal run to approve it. " + _RUN_ANOTHER_WAY),
    "unattended:cron": _k(
        "approval_denied", "owner-verb", "now", "an owner turn", False,
        "refused: this command {reason} in an unattended cron run; {why}. No owner is "
        "present on a cron run to approve it. " + _RUN_ANOTHER_WAY),
    "unattended:run": _k(
        "approval_denied", "owner-verb", "now", "an owner turn", False,
        "refused: this command {reason} in an unattended run; {why}. "
        + _RUN_ANOTHER_WAY),
}

_WHO_WORDS = {"agent": "you", "owner-verb": "the owner", "operator-env": "the operator",
              "deploy": "a code change"}


def refusal_text(kind: str, *, for_owner: bool = False, **facts) -> str:
    """The text of one run-time refusal. The model text ends with the ONE
    structured tail — who can act, the lever, whether a retry can help — so
    every refusal names them the same way. ``for_owner`` gives the person's
    text (no tail). ``KeyError`` on an unknown kind or a missing fact."""
    row = REFUSAL_KINDS[kind]
    if for_owner:
        return (row.owner_text or row.text).format(**facts)
    lever = row.actor.verb.format(**facts)
    return (row.text.format(**facts)
            + f" [who: {_WHO_WORDS[row.actor.who]} · lever: {lever} · "
              f"retry: {'yes' if row.retryable else 'no'}]")


@dataclass(frozen=True)
class ToolGate:
    """A tool's static gate declaration (the ``gate`` field of its row).

    - ``flag`` — the env flag whose OFF value explains "this tool is not
      registered" (the ``TOOL_GATE_FLAGS`` view; checked against the flags catalog).
    - ``label``/``tier``/``remedy`` — the line disclosed to the agent in the
      <tool-availability> block (the ``GATED_TOOL_REGISTRY`` view); a gate with no
      ``tier`` is not disclosed. ``remedy`` is a KEY of :data:`REMEDIES`, never
      prose (081 B-1); the text is rendered from there.
    """

    flag: Optional[str] = None
    label: Optional[str] = None
    tier: Optional[str] = None
    remedy: Optional[str] = None

    def __post_init__(self):
        if self.tier is not None and self.tier not in GATE_TIERS:
            raise ValueError(f"tier={self.tier!r} not in {sorted(GATE_TIERS)}")
        if (self.tier is None) != (self.label is None) or (self.tier is None) != (self.remedy is None):
            raise ValueError("label, tier and remedy are declared together")
        if self.remedy is not None and self.remedy not in REMEDIES:
            raise ValueError(f"remedy={self.remedy!r} is not a key of core.tool_gates.REMEDIES "
                             f"(a gate row names a key, never prose)")

    def disclosure(self, tool_id: str = "") -> Optional[tuple]:
        """``(label, tier, remedy text)`` or ``None`` when the gate is not disclosed."""
        if self.tier is None:
            return None
        return (self.label, self.tier,
                remedy_text(self.remedy, tool=tool_id, flag=self.flag or ""))

_GATES: Dict[str, Gate] = {}


def register_gate(tool_id: str, fn: Gate) -> None:
    """Called from the tool tier at import. Last registration wins."""
    _GATES[tool_id] = fn


def gate_for(tool_id: str) -> Optional[Gate]:
    """The registered gate for ``tool_id``, or ``None`` when none is registered."""
    return _GATES.get(tool_id)


def gate_on(tool_id: str) -> bool:
    """``True`` only when a gate is registered AND it returns true.

    Fail-closed: no gate or a gate that raises is ``False``.
    """
    fn = _GATES.get(tool_id)
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        return False


__all__ = ["ACTORS", "APPLIES", "GATE_TIERS", "Gate", "REMEDIES", "REMEDY_ACTORS", "RemedyActor",
           "STATUS_REASONS",
           "ToolGate", "register_gate", "gate_for", "gate_on", "remedy_actor",
           "remedy_key_for_text", "remedy_text"]
