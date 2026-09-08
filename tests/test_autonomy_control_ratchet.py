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
    "core/surfaces/outbound_dispatcher.py",         # the durable outbound drain (hold)
]

# `allows(` or the aliased `_allows(` — both must come from core.autonomy_control.
# (The module-qualified form `autonomy_control.allows(` is deliberately NOT accepted:
# every starter imports the predicate by name so the grep stays one shape.)
_CALL = re.compile(r"(?<![\w.])_?allows\(")
_IMPORT = re.compile(r"from core\.autonomy_control import")
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
