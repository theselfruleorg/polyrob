"""verbs.py — the ONE core-tier verb table (name -> group -> one-line help).

043 palette. Every owner-reachable slash verb lives here ONCE, in the core
tier, so the two seats that must describe it read the SAME words:

  * the CLI REPL (``cli/ui/commands/registry.py`` + the ``h_*.py`` handlers)
  * the console command palette (``webview/static/app/palette.js``, fed a
    generated ``verbs.en.json`` emitted from this table)

Why core-tier and not the CLI registry: the console MUST NOT import the CLI
package (``tests/test_layering_ratchet.py`` — and the palette lives in the
browser, which imports nothing Python at all). The membership set
``core.surfaces.dispatcher._COMMANDS`` is names-only — it carries no group and
no words — so a table that adds the two missing dimensions has to live beside
it in ``core/``. This module imports NOTHING above it (not even the
dispatcher) so it is cheap to load and cannot open an import cycle — the only
import is its own in-core contributor, ``core.money_verbs`` (067 P5a), loaded
at the end; the drift guard is a contract
TEST (``tests/unit/core/test_verbs.py``) that pins this table against
``_COMMANDS`` in BOTH directions.

Invariants (pinned by the test):
  * every name in ``_COMMANDS`` minus ``/task`` and ``/new`` has exactly one
    row here, and every row's name is in ``_COMMANDS`` (no stale rows);
  * ``/task`` and ``/new`` are DELIBERATELY absent — they are the two verbs the
    console excludes from ``/help`` (``webview/console_commands.py``);
  * every ``group`` is one of :data:`GROUP_ORDER`;
  * every ``help`` is one non-empty line and names no config flag.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


#: The ten grouped-``/help`` sections, in render order (043 A13/A24). The
#: ONE order: the CLI registry (``cli/ui/commands/registry.py``) imports it
#: from here (``core/`` may not import the CLI package, so the SSOT sits
#: below). A row whose group is outside this tuple fails the contract test,
#: so a verb can never silently fall out of a rendered section.
GROUP_ORDER: tuple[str, ...] = (
    "talk",
    "needs you",
    "work",
    "money",
    "control",
    "remember",
    "look",
    "set up",
    "display",
    "leave",
)


@dataclass(frozen=True)
class Verb:
    """One owner-reachable slash verb.

    Attributes:
        name:    Canonical name WITH the leading slash, e.g. ``"/inbox"`` — the
                 same shape as ``core.surfaces.dispatcher._COMMANDS``.
        group:   One of :data:`GROUP_ORDER`.
        help:    One line, plain prose. No config-flag name, no ``--`` flag, no
                 usage grammar — the palette and ``/help`` render that from the
                 handler, not from this summary.
        aliases: Alternative invocation names (with the leading slash), for
                 names that are NOT themselves a ``_COMMANDS`` row.
        seats:   Which seats can actually RUN this verb. Empty (the default and
                 the overwhelming majority) means EVERY seat — the verb routes
                 through ``_COMMANDS`` and every surface handles it. A non-empty
                 tuple names the only seats that do, and such a row is
                 deliberately NOT in ``_COMMANDS``: it is local to one seat.

                 ⚠️ A seat-local row must be filtered OUT by any renderer for a
                 different seat. Listing a verb the reader cannot run is a
                 promise the surface does not keep — which is why this is a
                 field rather than a convention. Seat names are the surface ids
                 the tree already uses (``repl``, ``telegram``, ``console``).
        room_refused: Never runs from inside a group room (044 I10: an owner
                 verb a member talked him into). A CONTRIBUTED verb is refused
                 there anyway unless its contributor named it room-safe
                 (``register_verbs(room_verbs=...)``); the flag is for the
                 core rows. Seats read :func:`room_refused`, never a literal.
        needs:   A surface capability (``core.surfaces.catalog.SurfaceSpec.
                 capabilities``) the verb's executor needs, e.g. ``moderation``.
                 A chat surface without it cannot run the row; an owner control
                 seat (the terminal, the console) runs it through the surface
                 that has it (113 W4 item 6, F-07). Seats read :meth:`runs_on`.
        background: Non-empty for a money verb whose executing (``go``) form
                 waits on a chain receipt or the owner's own tap: a chat seat
                 runs it off its poll loop and names it with this word in the
                 "started" reply (TG2; :func:`background_word`).
        agent:   120 W9 (the parity ratchet). The action or classified tool id
                 that gives the AGENT the same reach — a string only (core may
                 not import ``tools``). Exactly one of ``agent`` and
                 ``owner_only`` is set on every row
                 (``tests/test_verb_agent_parity_ratchet.py``).
        owner_only: One line: why the agent has no twin (proposal 120 §8, or
                 :data:`SEAT` for a seat control). A missing twin with no stated
                 reason reads ``"gap: …"``, so it stays visible and counted.
        agent_partial: With ``agent`` only: what the twin does NOT reach. A part
                 with no twin and no stated reason starts ``"gap:"``.
    """

    name: str
    group: str
    help: str
    aliases: tuple[str, ...] = ()
    seats: tuple[str, ...] = ()
    room_refused: bool = False
    background: str = ""
    needs: str = ""
    agent: str = ""
    owner_only: str = ""
    agent_partial: str = ""

    def runs_on(self, seat: str) -> bool:
        """True when ``seat`` can run this verb (no ``seats`` = every seat; a
        ``needs`` row = a seat that is not a chat surface, or one that has it)."""
        if self.seats and seat not in self.seats:
            return False
        if self.needs:
            from core.surfaces.catalog import get
            spec = get(seat)
            return spec is None or self.needs in spec.capabilities
        return True

    def run_seats(self) -> tuple:
        """The seat ids that run this row, for an answer that names them;
        ``()`` = every seat."""
        if self.seats:
            return self.seats
        if self.needs:
            from core.surfaces.catalog import capability_ids
            return capability_ids(self.needs) + ("repl", "console")
        return ()


#: 120 W9: the ``owner_only`` reason of a seat control (proposal §3.9): the
#: verb steers the seat the owner sits at, not the agent.
SEAT = "a seat control"

#: 120 §8 reasons, each used by more than one row.
_INGRESS = "ingress and trust settings (120 §8; chat-access-model.md)"
_DECIDES = "the owner's decision on what the agent proposed (120 §1 rule 3)"


#: The CORE rows. :data:`VERB_TABLE` is these plus every registered row
#: (:func:`register_verbs`). Ordered by :data:`GROUP_ORDER`, then by how a section
#: reads. Help lines are authored here (not imported from the CLI or Telegram
#: help bodies, which live in tiers ``core/`` may not reach) but are kept in
#: step with the Telegram ``_HELP_BODY`` SSOT so the seats do not describe one
#: verb differently.
#: ``room_refused`` (044 I10) — the reasons that sat in the Telegram literal:
#: /pause /halt /resume and /run (070) control autonomy; /job /goal /fulfill
#: (D43) mutate durable work;
#: /thread (061) is the one owner transcript; /contacts is private
#: correspondence; /why (O10) names tools, amounts and addresses; /cards
#: confirms a money line; /approve /reject /allow /deny /config /prefs /mcp
#: /apps /identity are owner writes. A contributed verb (the money verbs,
#: /writeoff, /check …) is refused unless its contributor names it
#: room-safe.
#: ``agent`` / ``owner_only`` / ``agent_partial`` (120 W9): see :class:`Verb`.
_CORE_TABLE: tuple[Verb, ...] = (
    # talk
    Verb("/start", "talk", "A welcome and a short tour of what I can do", owner_only=SEAT),
    # needs you
    Verb("/inbox", "needs you",
         "Everything waiting on a decision from you, blocking first; /inbox all lists every item",
         agent="agent_status",
         agent_partial="the health lines and the approvals count; not the composed list "
                       "(cards_read and goal_list(kind='ask') read two of its sources)"),
    Verb("/pending", "needs you",
         "Every approval waiting on you: spends, contacts, tokens, and proposals",
         agent="agent_status",
         agent_partial="the count and the kinds (approvals section); the decision is the owner's"),
    Verb("/approve", "needs you", "Approve something that is waiting on you", room_refused=True,
         owner_only=_DECIDES),
    Verb("/reject", "needs you", "Decline something that is waiting on you", room_refused=True,
         owner_only=_DECIDES),
    # REPL-local: the terminal is the seat where the owner sits while the agent
    # works, so it is where "what will stop and ask me" belongs.
    Verb("/gates", "needs you", "Which actions need your approval",
         seats=("repl",), agent="preferences",
         agent_partial="the approvals.require preference; removing a gate is a guarded "
                       "proposal the owner approves"),
    Verb("/asks", "needs you", "What I need from you to unblock work; /asks <id> opens one in full",
         agent="goal_list"),
    Verb("/missed", "needs you",
         "Messages I could not deliver live: capped, paused, or undelivered",
         agent="agent_status",
         agent_partial="the newest few in the delivery section, not /missed <n>"),
    Verb("/fulfill", "needs you",
         "Mark an ask fulfilled so its work can continue", room_refused=True,
         agent="owner_ask",
         agent_partial="answer= decides an ask on a genuine owner turn, in the owner's own words"),
    Verb("/cards", "needs you",
         "Open action cards: confirm, get a quote, answer, or cancel; /cards <id> reopens one in full", room_refused=True,
         agent="cards_read",
         agent_partial="reads every card and cancels one it raised; confirm and answer are the owner's"),
    # 096 W5: every cut is one tap away. A read of text this seat already
    # rendered for you (``core.surfaces.more_store``), never a decision.
    Verb("/more", "look", "The rest of a list or text I cut short", room_refused=True,
         owner_only=SEAT),
    # work
    Verb("/goal", "work", "Add, show, retry, grant, pause, or stop one goal", room_refused=True,
         agent="goal",
         agent_partial="a grant (/goal allow) and the lift of an owner hold are the owner's (120 §8)"),
    Verb("/goals", "work", "The goal board summary", agent="goal_list"),
    Verb("/job", "work",
         "Scheduled jobs: the list, or add, change, grant, pause, resume, run, or stop one",
         aliases=("/jobs",), room_refused=True, agent="cronjob",
         agent_partial="a grant (/job allow), the lift of an owner pause and a stop for good "
                       "are the owner's (120 §8)"),
    Verb("/apps", "work",
         "Deployed apps: approve an address, check health, stop, start or remove one", room_refused=True,
         agent="app_service",
         agent_partial="approving an app is a signed owner decision (120 §8)"),
    # money: no core row. Every money verb is CONTRIBUTED
    # (``register_verbs``) — today by ``core/money_verbs.py``, after 067 P5b by
    # the wallet pack. With none registered the group renders its install hint.
    # control
    Verb("/cancel", "control",
         "Stop the task I am running now; /cancel <card id> or /cancel cards cancels cards",
         aliases=("/stop",), owner_only=SEAT),
    Verb("/pause", "control", "Stop autonomous work now, all of it or one scope",
         aliases=("/halt",), room_refused=True, agent="autonomy_control"),
    Verb("/resume", "control", "Lift the pause", room_refused=True,
         owner_only="only the owner lifts a pause (120 §8)"),
    Verb("/run", "control", "Pause, resume or stop one background run", room_refused=True,
         agent="delegation_status",
         agent_partial="this session's helpers; a goal or a job is paused with goal_pause or "
                       "cronjob_pause; a live run's mailbox has no agent writer"),
    Verb("/mode", "control",
         "What I may do without you, what I do when you are away, and how to change it",
         agent="agent_status",
         agent_partial="the posture answers (posture section), not every row of /mode all"),
    # 077: confirmed by a card; the restart drains first and reports back here.
    Verb("/restart", "control", "Restart me once my current work is done", room_refused=True,
         agent="self_env_restart_service",
         agent_partial="a request the owner approves"),
    # 093 D6: `/update now` lets the operator's waiting deploy skip the owner-quiet window.
    Verb("/update", "control", "Update me to the latest release, then report back "
         "(/update now: a waiting deploy stops waiting for your chat to go quiet)",
         room_refused=True, agent="self_env_request_update",
         agent_partial="a request the owner approves; /update now is the owner's"),
    # remember
    Verb("/thread", "remember",
         "Our conversation across every session and rail, newest last", room_refused=True,
         agent="session_search",
         agent_partial="recall of past sessions; the recent owner thread is in its context; "
                       "no read of the whole transcript"),
    Verb("/journey", "remember", "What I have done over a recent window",
         aliases=("/recap",), agent="recent_activity"),
    # 096 W5: read only — the rules you gave me and my operating contract.
    Verb("/rules", "remember", "Your rules for me and my operating contract, numbered",
         room_refused=True, agent="owner_doc_manage",
         agent_partial="reads owner.md; contract.md is loaded in its context"),
    Verb("/kb", "remember", "Search and list my knowledge base", agent="knowledge"),
    # 113 W6 G-6: owner reads on every seat (a phone could not read either).
    Verb("/memory", "remember", "Recall across sessions and inspect memory scopes",
         room_refused=True, agent="session_search",
         agent_partial="the scope list is not shown; a scope promotion is the owner's (120 §8)"),
    # look
    Verb("/status", "look",
         "Health first, then session, goals, loops, delivery, wallet and money "
         "(`/status all`: every section)", agent="agent_status"),
    Verb("/files", "look", "Recent files I produced", agent="recent_activity",
         agent_partial="up to three files on each run line"),
    Verb("/cwd", "look", "The directory I am working in right now", owner_only=SEAT),
    # REPL-local: a per-TURN token meter. `/status` is the ONE snapshot every
    # seat renders, and this is the other thing the terminal used to show under
    # that name — separated so neither has to pretend to be the other.
    Verb("/meter", "look", "This turn's meter", seats=("repl",), owner_only=SEAT),
    Verb("/contacts", "look",
         "Who I have been writing to, and the transcript with one of them", room_refused=True,
         agent="contact_history"),
    Verb("/why", "look", "The last things I refused to do, and the reason for each",
         room_refused=True, agent="agent_status"),
    # set up
    Verb("/allow", "set up", "Allow me to message a target", room_refused=True,
         owner_only=_INGRESS),
    Verb("/deny", "set up", "Revoke a message permission", room_refused=True,
         owner_only=_INGRESS),
    Verb("/allowlist", "set up", "Who I am allowed to message", agent="message"),
    Verb("/pair", "set up", "Strangers who asked to talk to me: list, approve or revoke",
         room_refused=True, owner_only=_INGRESS),
    Verb("/groups", "set up",
         "Room presence admin: allow, deny, mode, roles, and more", agent="room_read",
         agent_partial="lists the rooms; allow, deny and roles are the owner's (120 §8); "
                       "a mode change is a propose_setting card"),
    Verb("/mute", "set up", "Silence a room, or mute a member for a while",
         agent="room_moderate",
         agent_partial="the owner alone lifts a room silence early"),
    # F-07: the member verbs need a platform that mutes and bans (`moderation`).
    # /mute stays on every seat: silencing a room is the agent's own presence.
    Verb("/ban", "set up", "Ban a member for a while", needs="moderation",
         agent="room_moderate"),
    Verb("/unban", "set up", "Lift a member's ban", needs="moderation", agent="room_moderate"),
    Verb("/unmute", "set up", "End a member's mute early", needs="moderation",
         agent="room_moderate"),
    Verb("/config", "set up", "Read or set preferences", room_refused=True, agent="preferences",
         agent_partial="an env flag only through a propose_setting card; the agent unsets a "
                       "SAFE preference, the seat's /config unset refuses a preference today"),
    # REPL-local (041 phase 2): the terminal holds the live session, so it is
    # where a RUNNING worker is stopped or steered; the store half is
    # `polyrob workers` on the same machine.
    Verb("/workers", "set up",
         "Named helpers: approve them, and stop or steer a running one",
         seats=("repl",), agent="worker_manage",
         agent_partial="lists, shows and proposes; approve and steer are the owner's"),
    Verb("/prefs", "set up", "The preferences you have set", room_refused=True,
         aliases=("/settings",), agent="preferences"),
    Verb("/mcp", "set up", "MCP servers I can use: add, remove, or test", room_refused=True,
         agent="mcp",
         agent_partial="an added server is a propose_setting card the owner confirms; the "
                       "removal of a configured server stays on the owner seat"),
    # display
    Verb("/avatar", "display",
         "This instance's avatar image — show, set, clear, or push it to my account pictures",
         agent="agent_avatar"),
    Verb("/identity", "display",
         "My on-chain identity: register it, or update where it is published (spends gas)",
              room_refused=True, background="Identity registration",
         agent="defi_trade_register_agent",
         agent_partial="the update of where it is published is defi_trade_set_agent_uri"),
    # 080 D8: the model and the skills, on every seat (Telegram had neither).
    Verb("/model", "set up",
         "The model this chat runs on — switch it live, set your default, pick reasoning; set "
         "the heavy and light models and what the agent may choose",
         room_refused=True,
         owner_only="unattended spend never moves by a model's choice (120 §8)"),
    # I-1: one help line for every seat, so it says where the writes run.
    Verb("/skills", "work",
         "The skills I can load, and what each one does; /skills <id> shows one "
         "skill's text. You install or approve a skill in the terminal",
         room_refused=True, agent="load_skill",
         agent_partial="the catalog is in its context; install and approve are the owner's (120 §8)"),
    # 080 WS-2: the REPL-only verbs, so this ONE table is complete. The terminal
    # holds the live session, its trace and the local machine; no other seat can
    # run these, so every other seat's help and palette leave them out.
    # The chat model: one main chat and the separate chats beside it, on every
    # seat (core/surfaces/chats.py). `/new` in a group restarts the room, so it
    # is the one of the five a room runs.
    Verb("/new", "talk", "Open a separate chat beside the main one", owner_only=SEAT),
    Verb("/chats", "talk", "Your chats: the main one and each separate one",
         aliases=("/sessions",), room_refused=True, owner_only=SEAT),
    Verb("/chat", "talk", "Switch to another of your chats", room_refused=True, owner_only=SEAT),
    Verb("/rename", "talk", "Name the chat you are in", room_refused=True, owner_only=SEAT),
    Verb("/close", "talk", "Close a separate chat and go back to the main one",
         room_refused=True, owner_only=SEAT),
    Verb("/clear", "talk", "Clear this chat and keep my instructions",
         seats=("repl",), owner_only=SEAT),
    Verb("/compact", "talk", "Shorten this chat's history in the background",
         aliases=("/compress",), seats=("repl",), owner_only=SEAT),
    Verb("/history", "talk", "The turns of this chat", seats=("repl",), owner_only=SEAT),
    Verb("/attach", "talk", "Attach local files or images to my next step",
         seats=("repl",), owner_only=SEAT),
    # I-5: every chat app steers with a plain message; the verb is the terminal's.
    Verb("/steer", "talk", "Give the running turn one more instruction (in a chat app, "
         "just send it)", seats=("repl",), owner_only=SEAT),
    Verb("/session", "talk", "Who and where this chat is: owner, model, memory, files",
         aliases=("/info",), seats=("repl",), owner_only=SEAT),
    Verb("/replay", "talk", "Replay a chat's feed", seats=("repl",), owner_only=SEAT),
    Verb("/export", "talk", "Export this chat's data", seats=("repl",), owner_only=SEAT),
    Verb("/learn", "work", "Describe how to do something and I save it as a skill",
         seats=("repl",), agent="skill_manage"),
    Verb("/todos", "work", "The todo list in my working files", seats=("repl",),
         agent="task_todo_list"),
    Verb("/subagents", "work", "What I can hand to a helper", seats=("repl",),
         agent="delegation_status",
         agent_partial="the live helpers; the delegation limits are not shown"),
    Verb("/autonomy", "control", "My background loops and what they hold; switch them on or off",
         seats=("repl",), agent="agent_status",
         agent_partial="reads the loops; a switch off is autonomy_control(pause), a switch on "
                       "is the lift of a pause (120 §8)"),
    Verb("/self", "remember", "My identity documents, read-only", aliases=("/soul",),
         room_refused=True, agent="self_context_manage",
         agent_partial="reads self.md; SOUL is loaded in its context"),
    Verb("/doctor", "look", "The health report", seats=("repl",), agent="agent_status",
         agent_partial="view='health': the credential lines stay in the terminal (llm-auth.md)"),
    Verb("/logs", "look", "Recent log lines of this chat", seats=("repl",), agent="agent_status",
         agent_partial="view='log' reads the event log, not this chat's log files"),
    Verb("/context", "look", "What fills my context, slot by slot", seats=("repl",),
         agent="agent_status"),
    Verb("/steps", "look", "The steps of the last turn", seats=("repl",), owner_only=SEAT),
    Verb("/tools", "look", "My abilities, built in and optional", seats=("repl",),
         agent="tool_search"),
    Verb("/telemetry", "look", "Event counts and wallet spend across chats",
         seats=("repl",), agent="agent_status"),
    Verb("/usage", "look", "What my AI use cost: by kind of work, job, model and day",
         aliases=("/cost",), agent="usage_summary"),
    Verb("/finance", "look", "Income, spend, money still due and net, with my AI cost",
         seats=("repl",), agent="x402_invoice_accounting"),
    Verb("/auth", "set up", "The provider keys I have, and how to connect one",
         seats=("repl",),
         owner_only="provider keys: no agent action can reach them (120 §8; llm-auth.md)"),
    Verb("/persona", "set up", "The personas, and the one new chats use", seats=("repl",),
         agent="preferences",
         agent_partial="a guarded session.persona proposal the owner approves; the persona "
                       "list is not shown"),
    Verb("/profile", "set up", "Which agent this terminal runs, and where its files live", seats=("repl",),
         owner_only="operator data: a profile is selected before the environment loads (120 §8)"),
    Verb("/toolset", "set up", "The ability sets, and the one new chats use", seats=("repl",),
         agent="preferences",
         agent_partial="a guarded session.toolset proposal the owner approves; the named sets "
                       "are not listed"),
    # I-3: "quiet" means quiet hours only; /quiet is the old name for one release.
    Verb("/brief", "display", "Hide or show the step transcript", seats=("repl",),
         aliases=("/quiet",), owner_only=SEAT),
    Verb("/verbose", "display", "Show or hide the live trace", seats=("repl",), owner_only=SEAT),
    Verb("/exit", "leave", "Leave the terminal", aliases=("/quit", "/q"),
         seats=("repl",), owner_only=SEAT),
    # leave
    Verb("/help", "leave", "This help, or the detail for one verb",
         aliases=("/h", "/?"), owner_only=SEAT),
)


#: Names DELIBERATELY not in :data:`VERB_TABLE`: `/task`, the old form of `/new`
#: (``webview/console_commands.py`` ``CONSOLE_HELP_EXCLUDED``). Named here so the
#: contract test reads the exclusion from the SSOT, not a literal it can drift from.
PALETTE_EXCLUDED: frozenset[str] = frozenset({"/task"})


#: 067 P5a — contributed verbs. A pack contributes ROWS (words, group) and, per
#: seat, the HANDLER that runs the verb there ("module:attr", resolved by the
#: seat, never imported by core). Core keeps the group order; a contributed
#: row must sit in a :data:`GROUP_ORDER` group.
#:
#: Seat conventions (the seat resolves and calls the reference):
#:   * ``telegram`` — ``async def handler(*, user_id, data_dir, args,
#:     task_agent, result, board) -> str``, run after the owner gate;
#:   * ``repl`` — a REPL ``Command`` handler ``(ctx: CommandContext)``;
#:   * ``console`` — reserved (the console routes through the dispatcher).
#: A seat whose built-in code already handles a verb ignores the reference
#: (today's money verbs have NO reference: the seats' own branches run them —
#: 067 P5b moves those branches into the wallet pack as references).
KNOWN_SEATS: tuple[str, ...] = ("telegram", "repl", "console")

#: The line a group renders when every row it could hold is uncontributed.
EMPTY_GROUP_HINTS: dict[str, str] = {
    "money": "No money verbs: install the wallet pack (polyrob pack install wallet)",
}

_REGISTERED: list[tuple[str, Verb]] = []          # (source, row), registration order
_HANDLERS: dict[tuple[str, str], str] = {}        # (seat, name) -> "module:attr"
_ROOM_OK: set = set()                             # contributed names a room may run


def _union() -> tuple[Verb, ...]:
    """Core rows then contributed rows, stably grouped by :data:`GROUP_ORDER`
    (within a group core rows keep their place and come first)."""
    rows = list(_CORE_TABLE) + [row for _src, row in _REGISTERED]
    rank = {g: i for i, g in enumerate(GROUP_ORDER)}
    return tuple(sorted(rows, key=lambda v: rank.get(v.group, len(rank))))


def register_verbs(rows, handlers_by_seat: Optional[dict] = None, *,
                   source: str = "core", room_verbs=()) -> None:
    """Contribute owner verbs: ``rows`` (:class:`Verb`) and, per seat in
    :data:`KNOWN_SEATS`, ``{verb name: "module:attr"}`` handler references.

    ``room_verbs``: the contributed names that may run from inside a group
    room. Every other contributed verb is refused there (044 I10: an owner verb
    executed in a room is an action a member talked him into).

    All or nothing: a row outside :data:`GROUP_ORDER`, a name (or alias) that
    another source already owns, a handler for a seat core does not know or
    for a verb not in ``rows``, or a reference not shaped ``module:attr`` is
    refused (``ValueError``) and nothing registers. Re-registering the SAME
    rows from the same source is a no-op (idempotent pack load)."""
    rows = tuple(rows)
    handlers_by_seat = dict(handlers_by_seat or {})
    own = {row.name for s, row in _REGISTERED if s == source}
    taken = {v.name for v in _CORE_TABLE} | {
        a for v in _CORE_TABLE for a in v.aliases}
    taken |= {n for s, row in _REGISTERED if s != source
              for n in (row.name, *row.aliases)}
    names = set()
    for row in rows:
        if not isinstance(row, Verb):
            raise ValueError(f"verb row {row!r} from {source} is not a Verb")
        if row.group not in GROUP_ORDER:
            raise ValueError(f"verb {row.name} from {source}: group {row.group!r} "
                             f"is not one of GROUP_ORDER")
        if not row.name.startswith("/") or not row.help.strip() or "\n" in row.help:
            raise ValueError(f"verb {row.name!r} from {source}: needs a /name and "
                             f"one help line")
        for n in (row.name, *row.aliases):
            if n in taken or n in names:
                raise ValueError(f"verb {n} from {source}: the name is already taken")
            names.add(n)
    row_names = {row.name for row in rows}
    stray = set(room_verbs) - row_names
    if stray:
        raise ValueError(f"room verbs from {source} not in its rows: {sorted(stray)}")
    for seat, table in handlers_by_seat.items():
        if seat not in KNOWN_SEATS:
            raise ValueError(f"verb handlers from {source}: unknown seat {seat!r}")
        for name, ref in dict(table).items():
            if name not in row_names:
                raise ValueError(f"verb handler {seat}:{name} from {source}: "
                                 f"not one of the rows it registers")
            mod, _, attr = str(ref).partition(":")
            if not mod or not attr:
                raise ValueError(f"verb handler {seat}:{name} from {source}: "
                                 f"{ref!r} is not 'module:attr'")
    if own and own == row_names:
        return                                       # idempotent re-registration
    if own:
        raise ValueError(f"verbs from {source} are already registered")
    _REGISTERED.extend((source, row) for row in rows)
    _ROOM_OK.update(room_verbs)
    for seat, table in handlers_by_seat.items():
        for name, ref in dict(table).items():
            _HANDLERS[(seat, name)] = str(ref)
    _rebuild()


def unregister_verbs(source: str) -> None:
    """Drop every row, handler and room grant ``source`` registered. The pack
    loader's rollback when a later hook of the same pack fails (a refused pack
    must not leave a live verb behind). Unknown source = no-op."""
    names = {row.name for s, row in _REGISTERED if s == source}
    if not names:
        return
    _REGISTERED[:] = [(s, row) for s, row in _REGISTERED if s != source]
    for key in [k for k in _HANDLERS if k[1] in names]:
        del _HANDLERS[key]
    _ROOM_OK.difference_update(names)
    _rebuild()


#: The source prefix of a verb a PACK registered (``core.packs.loader.verb_source``).
PACK_SOURCE_PREFIX = "pack:"


def pack_verb_names() -> frozenset:
    """Names of the rows a pack registered at runtime (phase 2). They exist only
    in a process that loaded that pack, so a checked-in artifact generated from
    this table (``scripts/gen_verbs_json.py``) leaves them out."""
    return frozenset(row.name for s, row in _REGISTERED if s.startswith(PACK_SOURCE_PREFIX))


def registered_verbs(source: Optional[str] = None) -> tuple[Verb, ...]:
    """Contributed rows (optionally of one source), in registration order."""
    return tuple(row for s, row in _REGISTERED if source is None or s == source)


def routed_names() -> frozenset:
    """Contributed verbs every seat routes as a COMMAND (not seat-local). The
    dispatcher unions these with its own names."""
    return frozenset(row.name for _s, row in _REGISTERED if not row.seats)


def room_refused(name: str) -> bool:
    """True for a verb that must not run from inside a group room: a core row
    flagged ``room_refused``, or a contributed verb its contributor did not
    name room-safe."""
    row = verb_for(name)
    if row is not None and row.room_refused:
        return True
    return name in routed_names() and name not in _ROOM_OK


def background_word(name: str) -> str:
    """The word a seat's "started" reply names for a background money verb,
    or ``""`` for a verb that answers inline (:attr:`Verb.background`)."""
    row = verb_for(name)
    return row.background if row is not None else ""


def handler_ref(seat: str, name: str) -> Optional[str]:
    """The ``module:attr`` a pack registered for ``name`` on ``seat``, or None."""
    return _HANDLERS.get((seat, name))


def empty_group_hints(seat: Optional[str] = None) -> list[tuple[str, str]]:
    """``(group, hint)`` for each hinted group with no row ``seat`` can run."""
    live = {g for g, _rows in grouped(seat)}
    return [(g, h) for g, h in EMPTY_GROUP_HINTS.items() if g not in live]


def _build_index() -> dict[str, Verb]:
    """Canonical names AND aliases -> the owning :class:`Verb`, so ``verb_for``
    resolves either. Aliases never collide with a canonical name (pinned by the
    contract test), so one flat map is unambiguous."""
    index: dict[str, Verb] = {}
    for verb in VERB_TABLE:
        index[verb.name] = verb
        for alias in verb.aliases:
            index[alias] = verb
    return index


VERB_TABLE: tuple[Verb, ...] = _union()
_BY_NAME: dict[str, Verb] = _build_index()


def verb_for(name: str) -> Optional[Verb]:
    """Return the :class:`Verb` for *name* or one of its aliases (leading slash
    optional), or ``None``."""
    if not name:
        return None
    key = name if name.startswith("/") else "/" + name
    return _BY_NAME.get(key.lower())


def grouped(seat: Optional[str] = None) -> list[tuple[str, tuple[Verb, ...]]]:
    """The table as ``(group, verbs)`` pairs in :data:`GROUP_ORDER`.

    Each group's verbs keep :data:`VERB_TABLE` order. A group with no rows is
    omitted, so a caller can render the sections directly.

    ``seat`` filters to the verbs that seat can actually RUN (see
    :attr:`Verb.seats`). ``None`` returns every row — right for a contract test
    or an inventory, and wrong for a rendered help body, which must never list
    a verb its reader cannot run.
    """
    out: list[tuple[str, tuple[Verb, ...]]] = []
    for group in GROUP_ORDER:
        rows = tuple(v for v in VERB_TABLE
                     if v.group == group
                     and (seat is None or v.runs_on(seat)))
        if rows:
            out.append((group, rows))
    return out


def seat_verbs(seat: str) -> tuple[Verb, ...]:
    """Every verb ``seat`` can run, in :data:`VERB_TABLE` order."""
    return tuple(v for v in VERB_TABLE if v.runs_on(seat))


def menu_entries(seat: str, local_rows: Optional[dict] = None, *,
                 desc_max: int = 256) -> list[tuple[str, str]]:
    """A chat platform's native command menu for ``seat`` (064 F2): pure.

    ``(name without slash, description)`` in :data:`GROUP_ORDER`, each group's
    table rows first, then the seat's own ``local_rows`` for that group
    (``{group: ((name, usage, text), …)}``). Only names a menu can carry
    (lowercase ``a-z0-9_``, ≤ 32) and each name once — Telegram's
    ``setMyCommands`` rejects the WHOLE call on one duplicate. Telegram feeds
    it to ``setMyCommands``; a Discord/Slack command registration reads the same.
    """
    import re
    ok = re.compile(r"^[a-z0-9_]{1,32}$")
    local_rows = local_rows or {}
    out: list[tuple[str, str]] = []
    seen: set = set()

    def _add(name: str, desc: str) -> None:
        bare = name.lstrip("/")
        if bare in seen or not ok.match(bare) or not desc.strip():
            return
        seen.add(bare)
        out.append((bare, desc.strip()[:desc_max]))

    for group in GROUP_ORDER:
        for verb in VERB_TABLE:
            if verb.group == group and verb.runs_on(seat):
                _add(verb.name, verb.help)
        for name, _usage, text in local_rows.get(group, ()):
            _add(name, text)
    return out


#: Rows that are LOCAL to one seat and therefore absent from
#: ``dispatcher._COMMANDS``. Derived, never hand-listed, so a new seat-local
#: verb cannot be forgotten by the contract test.
SEAT_LOCAL: frozenset = frozenset(v.name for v in VERB_TABLE if v.seats)


def _rebuild() -> None:
    """Recompute the union views after a registration."""
    global VERB_TABLE, _BY_NAME, SEAT_LOCAL
    VERB_TABLE = _union()
    _BY_NAME = _build_index()
    SEAT_LOCAL = frozenset(v.name for v in VERB_TABLE if v.seats)


#: 067 P5a: modules still IN core that contribute verbs on import. P5b deletes
#: the row when the wallet pack registers the money verbs instead.
_IN_CORE_PROVIDERS: tuple[str, ...] = ("core.money_verbs",)


def _load_in_core_providers() -> None:
    import importlib
    for name in _IN_CORE_PROVIDERS:
        importlib.import_module(name)


__all__ = [
    "EMPTY_GROUP_HINTS",
    "GROUP_ORDER",
    "KNOWN_SEATS",
    "SEAT_LOCAL",
    "Verb",
    "VERB_TABLE",
    "PALETTE_EXCLUDED",
    "seat_verbs",
    "verb_for",
    "grouped",
    "menu_entries",
    "empty_group_hints",
    "handler_ref",
    "pack_verb_names",
    "register_verbs",
    "registered_verbs",
    "room_refused",
    "background_word",
    "routed_names",
    "unregister_verbs",
]


_load_in_core_providers()
