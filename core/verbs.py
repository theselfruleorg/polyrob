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
    """

    name: str
    group: str
    help: str
    aliases: tuple[str, ...] = ()
    seats: tuple[str, ...] = ()

    def runs_on(self, seat: str) -> bool:
        """True when ``seat`` can run this verb (no ``seats`` = every seat)."""
        return not self.seats or seat in self.seats


#: The CORE rows. :data:`VERB_TABLE` is these plus every registered row
#: (:func:`register_verbs`). Ordered by :data:`GROUP_ORDER`, then by how a section
#: reads. Help lines are authored here (not imported from the CLI or Telegram
#: help bodies, which live in tiers ``core/`` may not reach) but are kept in
#: step with the Telegram ``_HELP_BODY`` SSOT so the seats do not describe one
#: verb differently.
_CORE_TABLE: tuple[Verb, ...] = (
    # talk
    Verb("/start", "talk", "A welcome and a short tour of what I can do"),
    # needs you
    Verb("/inbox", "needs you",
         "Everything waiting on a decision from you, blocking first"),
    Verb("/pending", "needs you",
         "Every approval waiting on you: spends, contacts, tokens, and proposals"),
    Verb("/approve", "needs you", "Approve something that is waiting on you"),
    Verb("/reject", "needs you", "Decline something that is waiting on you"),
    # REPL-local: the terminal is the seat where the owner sits while the agent
    # works, so it is where "what will stop and ask me" belongs.
    Verb("/gates", "needs you", "Which actions need your approval",
         seats=("repl",)),
    Verb("/asks", "needs you", "What I need from you to unblock work"),
    Verb("/missed", "needs you",
         "Messages I could not deliver live: capped, paused, or undelivered"),
    Verb("/fulfill", "needs you",
         "Mark an ask fulfilled so its work can continue"),
    Verb("/cards", "needs you",
         "Open action cards: confirm a quote, answer a choice, or cancel"),
    # work
    Verb("/goal", "work", "Steer one goal or a whole stream"),
    Verb("/goals", "work", "The goal board summary"),
    Verb("/rail", "work", "Standing work: list, create, switch, drop and grant rails"),
    Verb("/cron", "work", "Durable scheduled runs: list, add, edit, or cancel"),
    Verb("/apps", "work",
         "Deployed apps: approve an address, check health, or kill one"),
    # money: no core row. The 13 money verbs are CONTRIBUTED
    # (``register_verbs``) — today by ``core/money_verbs.py``, after 067 P5b by
    # the wallet pack. With none registered the group renders its install hint.
    # control
    Verb("/cancel", "control", "Stop the task I am running now"),
    Verb("/pause", "control", "Stop autonomous work now, all of it or one scope"),
    Verb("/resume", "control", "Lift the pause"),
    Verb("/halt", "control", "Stop all autonomous work now"),
    Verb("/run", "control", "Pause, resume or stop one background run"),
    Verb("/mode", "control",
         "The effective autonomy posture and how to change it"),
    Verb("/dev", "control", "Message the dev and ops loop directly"),
    # remember
    Verb("/recap", "remember", "The same as /journey"),
    Verb("/thread", "remember",
         "Our conversation across every session and rail, newest last"),
    Verb("/journey", "remember", "What I have done over a recent window"),
    Verb("/kb", "remember", "Search and list my knowledge base"),
    Verb("/memory", "remember", "Recall across sessions and inspect memory scopes",
         seats=("repl",)),
    # look
    Verb("/status", "look",
         "Health first, then session, goals, loops, and wallet"),
    Verb("/files", "look", "Recent files I produced"),
    Verb("/cwd", "look", "The directory I am working in right now"),
    # REPL-local: a per-TURN token meter. `/status` is the ONE snapshot every
    # seat renders, and this is the other thing the terminal used to show under
    # that name — separated so neither has to pretend to be the other.
    Verb("/meter", "look", "This turn's meter", seats=("repl",)),
    Verb("/contacts", "look",
         "Who I have been writing to, and the transcript with one of them"),
    Verb("/why", "look", "The last things I refused to do, and the reason for each"),
    # set up
    Verb("/allow", "set up", "Allow me to message a target"),
    Verb("/deny", "set up", "Revoke a message permission"),
    Verb("/allowlist", "set up", "Who I am allowed to message"),
    Verb("/groups", "set up",
         "Room presence admin: allow, deny, mode, roles, and more"),
    Verb("/mute", "set up", "Silence a room, or mute a member for a while"),
    Verb("/ban", "set up", "Ban a member for a while"),
    Verb("/unban", "set up", "Lift a member's ban"),
    Verb("/unmute", "set up", "End a member's mute early"),
    Verb("/config", "set up", "Read or set preferences"),
    # REPL-local (041 phase 2): the terminal holds the live session, so it is
    # where a RUNNING worker is stopped or steered; the store half is
    # `polyrob workers` on the same machine.
    Verb("/workers", "set up",
         "Named helpers: approve them, and stop or steer a running one",
         seats=("repl",)),
    Verb("/prefs", "set up", "The preferences you have set"),
    Verb("/mcp", "set up", "MCP servers I can use: add, remove, or test"),
    # display
    Verb("/avatar", "display", "This instance's avatar image — show, set, clear"),
    Verb("/identity", "display",
         "My on-chain identity: register it, or update where it is published (spends gas)"),
    # leave
    Verb("/help", "leave", "This help, or the detail for one verb",
         aliases=("/h", "/?")),
)


#: Names DELIBERATELY not in :data:`VERB_TABLE`: the two verbs the console drops
#: from ``/help`` (``webview/console_commands.py`` ``CONSOLE_HELP_EXCLUDED``).
#: Named here so the contract test reads the exclusion from the SSOT, not a
#: literal it can drift from.
PALETTE_EXCLUDED: frozenset[str] = frozenset({"/task", "/new"})


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
    """True for a contributed verb that must not run from inside a group room."""
    return name in routed_names() and name not in _ROOM_OK


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
    "routed_names",
    "unregister_verbs",
]


_load_in_core_providers()
