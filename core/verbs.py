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
it in ``core/``. This module imports NOTHING (not even the dispatcher) so it is
cheap to load and cannot open an import cycle; the drift guard is a contract
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


#: The one verb table. Ordered by :data:`GROUP_ORDER`, then by how a section
#: reads. Help lines are authored here (not imported from the CLI or Telegram
#: help bodies, which live in tiers ``core/`` may not reach) but are kept in
#: step with the Telegram ``_HELP_BODY`` SSOT so the seats do not describe one
#: verb differently.
VERB_TABLE: tuple[Verb, ...] = (
    # talk
    Verb("/start", "talk", "A welcome and a short tour of what I can do"),
    # needs you
    Verb("/inbox", "needs you",
         "Everything waiting on a decision from you, blocking first"),
    Verb("/pending", "needs you",
         "Proposals I have learned, waiting for your approval"),
    Verb("/approve", "needs you", "Approve something that is waiting on you"),
    Verb("/reject", "needs you", "Discard a pending proposal"),
    # REPL-local: the terminal is the seat where the owner sits while the agent
    # works, so it is where "what will stop and ask me" belongs.
    Verb("/gates", "needs you", "Which actions need your approval",
         seats=("repl",)),
    Verb("/asks", "needs you", "What I need from you to unblock work"),
    Verb("/missed", "needs you",
         "Messages I could not deliver live: capped, paused, or undelivered"),
    Verb("/fulfill", "needs you",
         "Mark an ask fulfilled so its work can continue"),
    # work
    Verb("/goal", "work", "Steer one goal or a whole stream"),
    Verb("/goals", "work", "The goal board summary"),
    Verb("/cron", "work", "Durable scheduled runs: list, add, or cancel"),
    Verb("/apps", "work",
         "Deployed apps: approve an address, check health, or kill one"),
    # money
    Verb("/book", "money",
         "My ledger against every money chain: one verdict, then what disagrees"),
    Verb("/wallet", "money", "Wallet addresses, network, and spend caps"),
    Verb("/invoices", "money", "What I have billed and who owes me"),
    Verb("/settle", "money", "Mark an invoice paid"),
    Verb("/trade", "money", "Start a run that carries the money verb"),
    Verb("/bridge", "money", "Move native value from one chain to another"),
    Verb("/launch", "money", "Launch a token on the launchpad"),
    Verb("/deploy", "money", "Deploy a fixed-supply token"),
    Verb("/lp", "money", "Liquidity positions: inspect, quote, add, remove, or collect fees"),
    Verb("/claim", "money", "Collect the creator fees a launchpad already owes me"),
    Verb("/nft", "money", "Collectibles I hold: look, send one, or revoke an approval"),
    Verb("/dapp", "money", "Web pages my wallet is connected to, and how to cut one off"),
    Verb("/paid", "money", "Paid room actions: status, pricing, and offers"),
    # control
    Verb("/cancel", "control", "Stop the task I am running now"),
    Verb("/pause", "control", "Stop autonomous work now, all of it or one scope"),
    Verb("/resume", "control", "Lift the pause"),
    Verb("/halt", "control", "Stop all autonomous work now"),
    Verb("/mode", "control",
         "The effective autonomy posture and how to change it"),
    Verb("/dev", "control", "Message the dev and ops loop directly"),
    # remember
    Verb("/recap", "remember", "What I have done over a recent window"),
    Verb("/journey", "remember", "What I have done over a recent window"),
    Verb("/kb", "remember", "Search and list my knowledge base"),
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
    Verb("/prefs", "set up", "The preferences you have set"),
    Verb("/mcp", "set up", "MCP servers I can use: add, remove, or test"),
    # display
    Verb("/avatar", "display", "This instance's face, traits, and voice signature"),
    Verb("/identity", "display",
         "My on-chain identity: register it, or update where it is published"),
    # leave
    Verb("/help", "leave", "This help, or the detail for one verb",
         aliases=("/h", "/?")),
)


#: Names DELIBERATELY not in :data:`VERB_TABLE`: the two verbs the console drops
#: from ``/help`` (``webview/console_commands.py`` ``CONSOLE_HELP_EXCLUDED``).
#: Named here so the contract test reads the exclusion from the SSOT, not a
#: literal it can drift from.
PALETTE_EXCLUDED: frozenset[str] = frozenset({"/task", "/new"})


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


#: Rows that are LOCAL to one seat and therefore absent from
#: ``dispatcher._COMMANDS``. Derived, never hand-listed, so a new seat-local
#: verb cannot be forgotten by the contract test.
SEAT_LOCAL: frozenset = frozenset(v.name for v in VERB_TABLE if v.seats)


__all__ = [
    "GROUP_ORDER",
    "SEAT_LOCAL",
    "Verb",
    "VERB_TABLE",
    "PALETTE_EXCLUDED",
    "seat_verbs",
    "verb_for",
    "grouped",
]
