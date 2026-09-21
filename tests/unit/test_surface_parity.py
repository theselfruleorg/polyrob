"""Surface-parity contract — owner capabilities stay reachable on every surface.

The 2026-07-12 UI-surface review found the agent core sprinting while each
user surface absorbed a different partial slice (webview missing prefs/
approvals, ledger webview-only, cron creatable nowhere, recap under two
names). This test pins the capability→surface exposure matrix the same way
``tests/unit/core/test_flags.py`` pins the flags catalog: a rename or removal
on any single surface fails CI instead of silently reopening the gap.

Matrix semantics: ``None`` = deliberately not exposed on that surface (a
DOCUMENTED decision, not an accident). To drop a capability from a surface,
change the matrix row in the same commit — that is the audit trail.
"""
import pytest

# capability -> (cli_command, repl_slash, webview_route, telegram_verb)
# cli_command: name in the click group (cli.polyrob.cli.commands)
# repl_slash:  name/alias resolvable in build_default_registry()
# webview_route: path registered on the webgate routers (pages/knowledge)
# telegram_verb: routable command in core.surfaces.dispatcher._COMMANDS AND
#                owner-gated in surfaces.telegram.harness._OWNER_ADMIN_COMMANDS
CAPABILITY_MATRIX = {
    "prefs-view":     ("config",    "config",   "/api/webgate/preferences", "/prefs"),
    # 043 §9 phase 4: the dedicated /preferences PAGE is deleted; its panel is now
    # the Agent destination's Settings tab (page /agent).
    "prefs-page":     (None,        None,       "/agent",                   None),
    # 2026-09-21 audit E2/A8: the ONE composer (core.surfaces.inbox) backs the
    # console reader; /api/webgate/pending delegates to it and is kept as the
    # legacy alias. The Inbox route is what the row pins.
    "pending-review": ("owner",     "pending",  "/api/webgate/inbox",       "/pending"),
    "pending-decide": ("owner",     "approve",  "/api/webgate/inbox/{kind}/{item_id}/decide",
                       "/approve"),
    "finance":        ("finance",   "finance",  "/api/webgate/ledger",      None),
    "goals":          ("goals",     "goals",    "/api/webgate/goals",       "/goals"),
    "cron":           ("cron",      "cron",     "/api/webgate/cron",        "/cron"),
    "recap":          ("journey",   "journey",  None,                       "/recap"),
    "recap-alias":    (None,        "recap",    None,                       "/journey"),
    "status":         (None,        "status",   "/api/webgate/doctor",      "/status"),
    "memory-search":  (None,        "memory",   "/api/webgate/memory/search", None),
    "knowledge":      ("knowledge", "kb",       "/agent",                   None),
    "identity":       ("identity",  "self",     "/api/webgate/identity",    None),
    # caps are live prefs since 2026-09-18 (budget.wallet_*): /wallet renders
    # them on both chat seats and Agent › Settings › Limits on the console.
    "wallet-caps":    ("wallet",    "wallet",   "/api/webgate/wallet",      "/wallet"),
    # C5: /approve DECIDES on every seat; the gate manager is /gates.
    "approval-gates": ("approvals", "gates",    None,                       None),
    "surfaces-admin": ("surface",   None,       None,                       None),
    # 030 WS-C C2 (finding G1): the owner kill switch + money/admin verbs are
    # reachable from every interactive owner seat. The webview column stays
    # None here deliberately until G2/C3 ships its control surface.
    "kill-switch-halt":   ("owner", "halt",      "/api/webgate/halt",       "/halt"),
    "kill-switch-resume": ("owner", "resume",    "/api/webgate/resume",     "/resume"),
    # 031: the owner pause record — /pause on every seat (halt stays an alias)
    "owner-pause":        ("autonomy", "pause",  "/api/webgate/pause",      "/pause"),
    "asks":               ("owner", "asks",      None,                      "/asks"),
    "asks-fulfill":       ("owner", "fulfill",   "/api/webgate/inbox/{kind}/{item_id}/fulfill", "/fulfill"),
    "outbound-allow":     ("owner", "allow",     None,                      "/allow"),
    "outbound-deny":      ("owner", "deny",      None,                      "/deny"),
    "outbound-allowlist": ("owner", "allowlist", None,                      "/allowlist"),
    "invoices":           ("owner", "invoices",  "/api/webgate/invoices",   "/invoices"),
    "invoices-settle":    ("owner", "settle",    "/api/webgate/invoices/{request_id}/settle", "/settle"),
    # 032: the durable app service — approve an address / kill on every owner seat
    "apps":               ("apps",  "apps",      "/api/webgate/apps",       "/apps"),
    # Capability inventory: every standing surface must remain represented here.
    # every row below is a seat that EXISTS in the tree today. A row pins reach,
    # never policy — the gates (tx_guard, caps, owner-only, the 031 pause) are
    # pinned by their own tests. `None` means "no seat today", not "not wanted";
    # the 043 home for each row is in CAPABILITY_HOME.
    "wallet":             ("wallet",   "wallet",    "/api/webgate/wallet",      "/wallet"),
    "trade-launch":       (None,       "trade",     None,                       "/trade"),
    # the console WATCHES a bridge (Money › Moves); starting one is a
    # terminal/chat verb — the pane says so (A25). Reach decision, not a gap.
    "bridge":             ("wallet",   "bridge",    "/api/webgate/bridges",     "/bridge"),
    "token-launch":       ("wallet",   "launch",    None,                       "/launch"),
    "token-deploy":       ("wallet",   "deploy",    None,                       "/deploy"),
    # the console reads every chain through /api/webgate/book (worst-chain-wins);
    # /api/webgate/positions is the per-chain read with no screen behind it.
    "book-positions":     ("wallet",   "lp",        "/api/webgate/book",        "/lp"),
    # 2026-09-15: the instance's own face/voice. The identity exists on every
    # seat now -- CLI, REPL, the console's live canvas and the phone -- so a
    # surface dropping it fails here instead of going quietly dark, which is
    # exactly what it did for two months.
    "avatar":             ("pfp",      "avatar",    "/pfp.json",                "/avatar"),
    "goal-steer":         ("goals",    "goal",      "/api/webgate/goals/{goal_id}/{verb}", "/goal"),
    "cron-cancel":        ("cron",     "cron",      "/api/webgate/cron/{job_id}/cancel",   "/cron"),
    "pending-reject":     ("owner",    "reject",    "/api/webgate/pending/{kind}/{item_id}/reject", "/reject"),
    "mcp-admin":          (None,       "mcp",       None,                       "/mcp"),
    "config":             ("config",   "config",    "/api/webgate/config",      "/config"),
    "kb":                 ("kb",       "kb",        "/api/webgate/knowledge/kb", "/kb"),
    "skills":             ("skills",   "skills",    "/api/webgate/knowledge/skills", None),
    "self-context":       ("soul",     "self",      "/api/webgate/identity",    None),
    "doctor":             ("doctor",   "doctor",    "/api/webgate/doctor",      None),
    "autonomy-status":    ("autonomy", "autonomy",  "/api/webgate/pause",       "/mode"),
    "missed":             ("owner",    "missed",    None,                       "/missed"),
    "files":              ("session",  "files",     "/api/webgate/artifacts",   "/files"),
    "dev-rail":           (None,       "dev",       None,                       "/dev"),
    "telemetry":          (None,       "telemetry", "/api/webgate/log",         None),
    # WS-K deleted /activity; the classified stream is now Work › Log (page
    # /work in pages_new, reader /api/webgate/log). The scanned routers include
    # pages_new but not worklog_api, so the page /work is the registered seat.
    "activity-log":       (None,       None,        "/work",                    None),
    "system-page":        (None,       None,        "/agent",                   None),
    "session-admin":      ("session",  "session",   None,                       None),
    "subagents":          ("subagents","subagents", None,                       None),
    "todos":              ("todos",    "todos",     None,                       None),
    "tools-catalog":      ("tools",    "tools",     None,                       None),
    "profile-home":       ("profile",  "profile",   None,                       None),
    "x-account":          ("x-account",None,        None,                       None),
    "surface-status":     ("surface",  None,        None,                       None),
    "memory-page":        (None,       "memory",    "/agent",                   None),
    # 043 D1: the ONE list of what is waiting on an owner decision, and the ONE
    # read of the ledger against every money chain. Both reach all four seats,
    # and all four render from the same composer + the same renderer
    # (core.surfaces.inbox / core.surfaces.inbox_render), which is what the row
    # is really pinning: not that a verb exists, but that a seat cannot quietly
    # stop having it.
    "inbox":              ("owner",    "inbox",     "/api/webgate/inbox",       "/inbox"),
    "book":               ("wallet",   "book",      "/api/webgate/book",        "/book"),
    # 2026-09-21 interface audit: rows for every capability that had a seat and
    # no row, and rows whose
    # every cell is None ON PURPOSE — an agent-only capability is a documented
    # fact here, not an absence the guard cannot see.
    "liquidity":          ("wallet",   "lp",        "/api/webgate/liquidity",   "/lp"),
    "groups":             ("owner",    "groups",    None,                       "/groups"),
    "room-mute":          ("owner",    "mute",      None,                       "/mute"),
    "room-ban":           (None,       "ban",       None,                       "/ban"),
    "paid-rooms":         ("owner",    "paid",      None,                       "/paid"),
    "bridges-inflight":   ("wallet",   None,        "/api/webgate/bridges",     None),
    "creations":          (None,       None,        "/api/webgate/creations",   "/status"),
    "moves":              (None,       None,        "/api/webgate/moves",       None),
    "running":            ("session",  "subagents", "/api/webgate/running",     None),
    "work-log":           (None,       "telemetry", "/api/webgate/log",         None),
    "capabilities":       ("tools",    "tools",     "/api/webgate/capabilities", None),
    "flags":              ("config",   "config",    "/api/webgate/flags",       "/config"),
    "chats":              ("session",  "sessions",  "/api/webgate/chats",       None),
    "cron-add":           ("cron",     "cron",      "/api/webgate/cron",        "/cron"),
    "session-meter":      (None,       "meter",     None,                       None),
    "subscriptions":      ("owner",    None,        None,                       None),
    "api-keys":           ("keys",     None,        None,                       None),
    "persona":            ("persona",  "persona",   None,                       None),
    "update":             ("update",   None,        None,                       None),
    # E6-E10: agent-only until 2026-09-21; now on the terminal, the REPL and the phone.
    "claim":              ("wallet",   "claim",     None,                       "/claim"),
    "nft":                ("wallet",   "nft",       None,                       "/nft"),
    "eip8004":            ("identity", "identity",  None,                       "/identity"),
    "dapp-sessions":      ("wallet",   "dapp",      None,                       "/dapp"),
    "contacts":           ("owner",    "contacts",  None,                       "/contacts"),
    # Agent-only by design (the agent bills; the owner reads/settles): all None.
    "invoice-create":     (None,       None,        None,                       None),
}

# The destination for every capability row
# §2.3/§2.4 + the 2026-09-14 inventory §2). A destination is one of the five
# console slots, a CLI group, or "shell" (frame furniture). R7 of 043 §10: every
# capability has a home, and the home is one of these.
CAPABILITY_HOME = {
    "prefs-view": "Agent › Settings", "prefs-page": "Agent › Settings",
    "pending-review": "Inbox", "pending-decide": "Inbox", "pending-reject": "Inbox",
    "finance": "Money › Cash", "invoices": "Money › Invoices", "invoices-settle": "Money › Invoices",
    "goals": "Work › Now & next", "goal-steer": "Work › Now & next", "cron": "Work › On a clock",
    "cron-cancel": "Work › On a clock", "recap": "Work › Log", "recap-alias": "Work › Log",
    "avatar": "Agent › Overview",
    "status": "Agent › Overview", "doctor": "Agent › Advanced › Diagnostics",
    "system-page": "Agent › Advanced › Diagnostics", "memory-search": "Agent › Memory",
    "memory-page": "Agent › Memory", "knowledge": "Agent › Memory", "kb": "Agent › Memory",
    "identity": "Agent › Identity", "self-context": "Agent › Identity",
    "wallet-caps": "Agent › Settings › Limits", "wallet": "Money › Book",
    "approval-gates": "Agent › Settings", "surfaces-admin": "Agent › Overview",
    "surface-status": "Agent › Overview", "kill-switch-halt": "shell", "kill-switch-resume": "shell",
    "owner-pause": "shell", "asks": "Inbox", "asks-fulfill": "Inbox",
    "outbound-allow": "Agent › Settings", "outbound-deny": "Agent › Settings",
    "outbound-allowlist": "Agent › Settings", "apps": "Work › Apps",
    "trade-launch": "Money › Book", "bridge": "Money › Moves", "token-launch": "Money › Moves",
    "token-deploy": "Money › Moves", "book-positions": "Money › Book",
    "mcp-admin": "Agent › Capabilities", "config": "Agent › Settings › Advanced",
    "skills": "Agent › Capabilities", "autonomy-status": "Agent › Overview",
    "missed": "Work › Log", "files": "Work › Apps", "dev-rail": "Work › Log",
    "telemetry": "Work › Log", "activity-log": "Work › Log",
    "session-admin": "Chat", "subagents": "Agent › Capabilities", "todos": "Work › Now & next",
    "tools-catalog": "Agent › Capabilities", "profile-home": "CLI › Owner", "x-account": "CLI › Surfaces",
    "inbox": "Inbox", "book": "Money › Book",
    "liquidity": "Money › Book", "groups": "Agent › Settings", "room-mute": "Agent › Settings",
    "room-ban": "Agent › Settings", "paid-rooms": "Money › Invoices",
    "bridges-inflight": "Money › Moves", "creations": "Money › Moves", "moves": "Money › Moves",
    "running": "Work › Now & next", "work-log": "Work › Log",
    "capabilities": "Agent › Capabilities", "flags": "Agent › Settings › Advanced",
    "chats": "Chat", "cron-add": "Work › On a clock", "session-meter": "Chat",
    "subscriptions": "Money › Invoices", "api-keys": "CLI › Owner",
    "persona": "Agent › Identity", "update": "CLI › Owner",
    "claim": "Money › Moves", "nft": "Money › Book", "eip8004": "Agent › Identity",
    "dapp-sessions": "Money › Moves", "contacts": "Inbox", "invoice-create": "Money › Invoices",
}
DESTINATIONS = ("Chat", "Inbox", "Work", "Money", "Agent", "shell", "CLI")


def _webview_paths():
    """Every path the console's routers register.

    ``webview.inbox`` and ``webview.pages_new`` are the 043 console; they are
    registered from ``pages_new.mount`` rather than from ``server.py``'s
    include block, so a scan that only knew the four v1 routers would report
    the Inbox as having no web seat.
    """
    import webview.activity as activity
    import webview.apps_routes as apps_routes
    import webview.artifacts_api as artifacts_api
    import webview.inbox as inbox
    import webview.knowledge as knowledge
    import webview.pages as pages
    import webview.pages_new as pages_new
    import webview.worklog_api as worklog_api
    paths = set()
    # 2026-09-21: pages_new.api_router, worklog_api and artifacts_api were
    # registered but unscanned, so twelve readers were invisible to this guard.
    for router in (pages.router, knowledge.router, apps_routes.router,
                   activity.router, inbox.router, pages_new.router,
                   pages_new.api_router, worklog_api.router, artifacts_api.router):
        for route in router.routes:
            paths.add(route.path)
    # 043 phase 5: the WEBVIEW_UI legacy switch and webview.legacy are removed.
    # /sessions is deleted; /pending is a normal route on pages.router now (so it
    # is already in the set above). The memory/knowledge/preferences/config/
    # identity/system/settings pages were DELETED (043 §9) and their capability
    # rows point at the new Agent destination (/agent, from pages_new).
    return paths


@pytest.fixture(scope="module")
def surfaces():
    from cli.polyrob import cli
    from cli.ui.commands.handlers import build_default_registry
    from core.surfaces.dispatcher import _COMMANDS
    from surfaces.telegram.harness import _OWNER_ADMIN_COMMANDS
    return {
        # list_commands is the public click API — it includes the lazy-loaded
        # subcommand names (cli.commands only holds the eagerly-defined ones).
        "cli": set(cli.list_commands(None)),
        "repl": build_default_registry(),
        "web": _webview_paths(),
        "tg_routable": set(_COMMANDS),
        "tg_owner": set(_OWNER_ADMIN_COMMANDS),
    }


@pytest.mark.parametrize("capability", sorted(CAPABILITY_MATRIX))
def test_capability_exposed_where_promised(capability, surfaces):
    cli_cmd, slash, web_route, tg_verb = CAPABILITY_MATRIX[capability]
    problems = []
    if cli_cmd is not None and cli_cmd not in surfaces["cli"]:
        problems.append(f"CLI command {cli_cmd!r} missing from the click group")
    if slash is not None and surfaces["repl"].lookup(slash) is None:
        problems.append(f"REPL slash /{slash} not registered")
    if web_route is not None and web_route not in surfaces["web"]:
        problems.append(f"webview route {web_route!r} not registered")
    if tg_verb is not None:
        if tg_verb not in surfaces["tg_routable"]:
            problems.append(f"telegram verb {tg_verb} not routable (dispatcher._COMMANDS)")
        if tg_verb not in surfaces["tg_owner"]:
            problems.append(f"telegram verb {tg_verb} not owner-gated "
                            f"(harness._OWNER_ADMIN_COMMANDS)")
    assert not problems, f"{capability}: " + "; ".join(problems)


def test_matrix_covers_the_review_gaps():
    """The specific 2026-07-12 review gaps stay pinned by name — if a row is
    ever deleted wholesale, this fails and points at the review doc."""
    for cap in ("prefs-page", "pending-decide", "finance", "cron", "recap-alias"):
        assert cap in CAPABILITY_MATRIX


def test_every_capability_has_one_home():
    """043 §10 R7 — the capability × seat × destination matrix: every row in
    CAPABILITY_MATRIX names exactly one 043 destination, and that destination is
    one of the five console slots, a CLI group, or the shell. A capability with
    no home is the failure mode the 2026-09-14 inventory was built to catch."""
    missing = sorted(set(CAPABILITY_MATRIX) - set(CAPABILITY_HOME))
    assert not missing, f"capabilities with NO 043 home: {missing}"
    stray = sorted(set(CAPABILITY_HOME) - set(CAPABILITY_MATRIX))
    assert not stray, f"homes for capabilities that are not in the matrix: {stray}"
    bad = {c: h for c, h in CAPABILITY_HOME.items()
           if not h.startswith(DESTINATIONS)}
    assert not bad, f"homes outside the five destinations / CLI / shell: {bad}"


# Telegram verbs that are routable but deliberately have no REPL twin: `/task`
# is "start a task" — in the REPL free text IS the task.
_REPL_EXCEPTIONS = frozenset({"/task"})


def test_repl_covers_every_telegram_owner_verb(surfaces):
    """Owner decision (2026-09-13): the REPL reaches FULL Telegram parity. 044
    and 046 shipped six owner verbs after the parity port and nothing caught
    it; this does, for every future verb."""
    repl = surfaces["repl"]
    missing = sorted(v for v in (surfaces["tg_owner"] | surfaces["tg_routable"])
                     if v not in _REPL_EXCEPTIONS and repl.lookup(v.lstrip("/")) is None)
    assert not missing, f"Telegram verbs with no REPL twin: {missing}"


def test_board_view_never_calls_list():
    """`GoalBoard.list` is the dispatcher's claim order (priority DESC,
    created_at ASC, LIMIT). Used as a VIEW it showed the owner the oldest 100
    rows and zero live legs (2026-08-29); used as an ID LOOKUP it 404'd a real
    goal past the window (2026-09-21 audit A5/D11/C14). No owner seat may call
    it — `list_recent`/`status_counts`/`get` are the readers."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    offenders = []
    # A call site only: a mention inside a comment or a backticked docstring
    # ("this used ``board.list(...)``") is the history, not an offender.
    pat = re.compile(r"(?<![`#])\bboard\.list\(")
    for base in ("cli", "webview", "surfaces", "api", "core/goal_board_render.py"):
        p = root / base
        files = [p] if p.is_file() else p.rglob("*.py")
        for f in files:
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or "``" in line or "`board" in line:
                    continue
                if pat.search(line) and "list_recent" not in line:
                    offenders.append(f"{f.relative_to(root)}:{i}")
    assert not offenders, f"board.list used as a view/lookup on an owner seat: {offenders}"


def test_repl_help_group_matches_core_verbs(surfaces):
    """One verb, ONE `/help` section — on the terminal and on the phone.

    ``core.verbs.VERB_TABLE`` is the SSOT for a verb's name, group and one-line
    meaning, and Telegram's ``/help`` is RENDERED from it. The REPL registry
    declares its own ``group=`` per command, so the two drifted silently: on
    2026-09-21 six verbs sat in a different section on each seat (``/gates``
    under control vs "needs you", ``/contacts`` under "needs you" vs look,
    ``/identity`` under money vs display, ``/mode``, ``/dev``, ``/files``).
    An owner who learns where a verb lives on one seat must find it in the same
    place on the other, so the group is part of the contract the name is.

    REPL-ONLY commands (``/verbose``, ``/model``, …) carry no ``core.verbs``
    row and are deliberately unconstrained.
    """
    from core.verbs import VERB_TABLE
    repl = surfaces["repl"]
    wrong = []
    for verb in VERB_TABLE:
        if verb.seats and "repl" not in verb.seats:
            continue
        cmd = repl.lookup(verb.name.lstrip("/"))
        if cmd is None:          # reach is pinned by the test above
            continue
        if cmd.group != verb.group:
            wrong.append(f"{verb.name}: core.verbs={verb.group!r} repl={cmd.group!r}")
    assert not wrong, ("REPL /help sections disagree with core.verbs "
                       f"(the SSOT Telegram renders): {wrong}")
