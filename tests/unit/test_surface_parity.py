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
    "prefs-page":     (None,        None,       "/preferences",             None),
    "pending-review": ("owner",     "pending",  "/api/webgate/pending",     "/pending"),
    "pending-decide": ("owner",     "pending",  "/api/webgate/pending/{kind}/{item_id}/promote",
                       "/approve"),
    "finance":        ("finance",   "finance",  "/api/webgate/ledger",      None),
    "goals":          ("goals",     "goals",    "/api/webgate/goals",       "/goals"),
    "cron":           ("cron",      "cron",     "/api/webgate/cron",        None),
    "recap":          ("journey",   "journey",  None,                       "/recap"),
    "recap-alias":    (None,        "recap",    None,                       "/journey"),
    "status":         (None,        "status",   "/api/webgate/doctor",      "/status"),
    "memory-search":  (None,        "memory",   "/api/webgate/memory",      None),
    "knowledge":      ("knowledge", "kb",       "/knowledge",               None),
    "identity":       (None,        "self",     "/api/webgate/identity",    None),
    "wallet-caps":    ("wallet",    None,       None,                       None),
    "approval-gates": ("approvals", "approve",  None,                       None),
    "surfaces-admin": ("surface",   None,       None,                       None),
}


def _webview_paths():
    import webview.knowledge as knowledge
    import webview.pages as pages
    paths = set()
    for router in (pages.router, knowledge.router):
        for route in router.routes:
            paths.add(route.path)
    return paths


@pytest.fixture(scope="module")
def surfaces():
    from cli.polyrob import cli
    from cli.ui.commands.handlers import build_default_registry
    from core.surfaces.dispatcher import _COMMANDS
    from surfaces.telegram.harness import _OWNER_ADMIN_COMMANDS
    return {
        "cli": set(cli.commands.keys()),
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
