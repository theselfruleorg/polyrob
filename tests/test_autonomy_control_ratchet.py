"""031 ratchet: an autonomous activity STARTER must consult the ONE pause
predicate (``core.autonomy_control.allows``). The list may only GROW (a new
starter is added here the day it is written); a listed file without an
``allows(`` call fails, and a listed file that reads a legacy sentinel name
directly (a second mechanism creeping back) fails too."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

STARTERS = [
    "agents/task/goals/dispatcher.py",              # dispatch + plan
    "agents/task/goals/streams.py",                 # seed_stream
    "cron/runner.py",                               # cron_run + digest
    "agents/task/task_agent_delivery.py",           # self_wake + resume_session
    "agents/task_agent_lite.py",                    # the async-delegation wake kick
    "agents/task/conversation_resume.py",           # resume_session
    "agents/task/agent/core/curator.py",            # curator
    "agents/task/agent/core/background_review.py",  # background_review
    "modules/x402/settlement_watcher.py",           # settlement_scan
    "core/surfaces/user_delivery.py",               # lifecycle_ping + escalate (rail)
    "core/autonomy_runtime.py",                     # cold_start (+ the transition hook)
    "core/config_policy/autonomy_config.py",        # the compat facets (money/dispatch consumers)
    "tools/app_service/tool.py",                    # 032: app_deploy (the agent verb)
    "core/app_service/supervisor.py",               # 032: app_serve (the supervisor tick)
    "tools/twitter_tool.py",                        # 033: social_post, on EVERY write verb
    "tools/controller/message_send.py",             # the `message` tool's send path
    "tools/email_tool.py",                          # D9: the email_send escape hatch
    "core/surfaces/outbound_dispatcher.py",         # the durable outbound drain (hold)
]

# `allows(` or the aliased `_allows(` — both must come from core.autonomy_control.
# (The module-qualified form `autonomy_control.allows(` is deliberately NOT accepted:
# every starter imports the predicate by name so the grep stays one shape.)
#
# `message_pause_refusal(` is the ONE documented wrapper (it is itself a direct
# `allows(kind)` call — see its docstring), the way `_KIND_CALL` below already
# accepts `pause_refusal_text`. A starter that reaches the predicate through it
# must NOT also open-code an `allows()` call: that would be the second
# mechanism this ratchet exists to prevent.
_CALL = re.compile(r"(?<![\w.])(?:_?allows|message_pause_refusal)\(")
_IMPORT = re.compile(
    r"from core\.autonomy_control import"
    r"|from tools\.controller\.message_send import [^\n]*message_pause_refusal")
_LEGACY = ("AUTONOMY_HALT", "STREAM_SEEDING_PAUSE", "TREASURY_ENTRY_PAUSE")


def test_every_starter_consults_the_pause_predicate():
    missing = []
    for f in STARTERS:
        src = (REPO / f).read_text(encoding="utf-8")
        if not (_CALL.search(src) and _IMPORT.search(src)):
            missing.append(f)
    assert missing == [], f"starters without an autonomy_control.allows( call: {missing}"


def test_no_starter_reads_a_legacy_sentinel_directly():
    """The record is the only state; a fresh os.path.exists('AUTONOMY_HALT') or
    bool_env('AUTONOMY_HALT') outside core/autonomy_control.py is a second
    mechanism creeping back."""
    bad = []
    for f in STARTERS:
        src = (REPO / f).read_text(encoding="utf-8")
        if any(name in src for name in _LEGACY):
            bad.append(f)
    assert bad == [], f"legacy sentinel names referenced outside core/autonomy_control.py: {bad}"


# --- 043 A8/A42: every declared kind has a caller, or is honestly dormant ---

#: the top-level trees a kind-caller may live in, matching the brief's scope.
_KIND_SEARCH_DIRS = ("core", "agents", "tools", "cron", "modules", "surfaces",
                     "cli", "webview")
#: the file that DECLARES the kinds (KIND_SCOPES / DORMANT_KINDS) — excluded
#: from the caller search, or every kind would trivially "have a caller"
#: (its own dict key, sitting next to the `allows(` in the same module).
_KIND_DECLARATION_FILE = "core/autonomy_control.py"
#: a direct `allows("<kind>")` / `_allows("<kind>")` call, OR the documented
#: wrapper `pause_refusal_text("<kind>", ...)` (itself a direct `allows(kind,
#: data_dir)` call — see its docstring) — OR a `_PAUSE_KIND_BY_SOURCE`-style
#: dict literal that feeds a variable into a nearby `allows(var)` call (the
#: literal and the call need only share a file, matching how this test's
#: sibling greps treat a docstring mention as "the file talks about allows(").
#: The negative lookbehind deliberately does NOT match a qualified
#: `autonomy_control.allows(`/`self._allows(` call (mirrors `_CALL` above) —
#: every real caller in the tree imports the predicate by name.
_KIND_CALL = re.compile(r"(?<![\w.])(?:_?allows|pause_refusal_text)\(")


def test_every_kind_has_a_caller_or_is_dormant():
    """A `KIND_SCOPES` row with zero callers anywhere in the tree must be named
    in `DORMANT_KINDS` — the plain-word table must not offer a scope whose
    kinds are all dormant. Bidirectional: a kind WITH a caller may not sit in
    DORMANT_KINDS either (a stale dormant entry hides a real gap the day a
    caller lands and nobody removes the label)."""
    from core.autonomy_control import DORMANT_KINDS, KIND_SCOPES

    files = []
    for d in _KIND_SEARCH_DIRS:
        for py in sorted((REPO / d).rglob("*.py")):
            rel = py.relative_to(REPO)
            if "tests" in rel.parts or str(rel) == _KIND_DECLARATION_FILE:
                continue
            files.append(py)
    sources = [py.read_text(encoding="utf-8", errors="replace") for py in files]

    has_caller = set()
    for kind in KIND_SCOPES:
        needle_d, needle_s = f'"{kind}"', f"'{kind}'"
        for src in sources:
            if (needle_d in src or needle_s in src) and _KIND_CALL.search(src):
                has_caller.add(kind)
                break

    no_caller = set(KIND_SCOPES) - has_caller
    assert no_caller == set(DORMANT_KINDS), (
        f"kinds with zero callers must equal DORMANT_KINDS exactly — "
        f"no_caller={sorted(no_caller)} dormant={sorted(DORMANT_KINDS)}")


def test_only_autonomy_control_and_owner_admin_probe_legacy_names_in_core():
    """Beyond the starters: in the core tier only the record module (reads the
    facets) and owner_admin (names them for the owner) may mention them."""
    allowed = {"core/autonomy_control.py", "core/surfaces/owner_admin.py"}
    bad = []
    for py in sorted((REPO / "core").rglob("*.py")):
        rel = str(py.relative_to(REPO))
        if rel in allowed or rel == "core/flags_catalog.py":
            continue
        src = py.read_text(encoding="utf-8", errors="replace")
        for name in _LEGACY:
            if re.search(rf"os\.path\.(exists|join|isfile)\([^)]*{name}|bool_env\(\s*['\"]{name}"
                         rf"|os\.getenv\(\s*['\"]{name}|os\.environ(\.get)?\(?\[?\s*['\"]{name}"
                         rf"|Path\([^)]*\)\s*/\s*['\"]{name}", src):
                bad.append((rel, name))
    assert bad == [], f"direct legacy sentinel probes in core: {bad}"
