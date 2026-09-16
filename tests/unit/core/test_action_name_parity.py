"""Every action name in a POLICY SET must be a name the runtime can actually emit.

This is the ratchet for a bug class that has now gone live four separate times:
a gate/approval set lists a BARE verb (`x402_request`, `run_code`, `send_email`)
while the runtime registers container-tool actions NAMESPACED as
``{tool_id}_{action}`` (tools/controller/tool_management.py). The listed name then
matches nothing, the gate silently never fires, and every unit test passes because
the tests assert the same fiction the policy set does.

Known casualties this file exists to prevent recurring:
  * ``PAYMENT_APPROVAL_TOOLS`` listed ``x402_request``; the runtime name is
    ``x402_invoice_x402_request`` — the owner payment-approval lane never fired.
  * ``correspondent_gate._REPLY_ACTIONS`` listed ``send_email``; the real action is
    ``email_send`` — the scoped tainted-reply exemption was dead for email.
  * several ``_HIGH_IMPACT_NAMES`` entries (``run_code``, ``deploy``, ``accounting``)
    were dead, leaving only tool-id resolution between a tainted session and the verb.

Rather than instantiating a live Controller (which needs a container, network-capable
services and every feature flag on), this derives the runtime name set STATICALLY from
the same two sources the Controller uses:
  1. container tools — every ``@BaseTool.action``-decorated method on a registered
     tool class, put through tool_management's exact namespacing rule;
  2. directly-registered actions — the ``@self.registry.action`` closures, whose
     runtime name is simply the function name.
"""
import ast
import inspect
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

# Modules that register actions directly on the registry (no owning tool_id), so
# their runtime name is the decorated closure's own name.
_DIRECT_ACTION_MODULES = (
    "tools/controller/action_registration.py",
    "tools/controller/tool_search_actions.py",
    "tools/controller/insights_action.py",
    "tools/controller/agent_status_action.py",  # extracted 2026-08-28 (status SSOT)
    "tools/controller/autonomy_control_action.py",  # 031: owner pause/resume (extracted)
    "tools/controller/doc_authoring.py",  # self_context_manage/owner_doc_manage (extracted 2026-09-08)
    "tools/controller/room_read_action.py",  # gated direct action; still a real runtime name
)


def _namespaced(tool_id: str, action_name: str) -> str:
    """tool_management.py's rule, verbatim: don't double-prefix a method that already
    carries the tool name."""
    if action_name == tool_id or action_name.startswith(f"{tool_id}_"):
        return action_name
    return f"{tool_id}_{action_name}"


def _register_every_optional_tool() -> None:
    """Force-register the flag-gated tools so TOOL_DESCRIPTORS is complete.

    Each registrar is idempotent and takes ``force=True`` to bypass its feature flag.
    A registrar that cannot import (optional dependency absent) is skipped — that
    only shrinks coverage, it never produces a false failure.
    """
    import tools  # noqa: F401 — registers the always-on tool classes

    registrars = (
        ("tools.coding", "register_coding_tool"),
        ("tools.code_exec", "register_code_exec_tool"),
        ("tools.git", "register_git_tool"),
        ("tools.github", "register_github_tool"),
        ("tools.defi", "register_defi_data_tool"),
        ("tools.defi", "register_defi_trade_tool"),
        ("tools.shell", "register_shell_tools"),
        ("tools.self_env", "register_self_env_tool"),
        ("tools.hf_deploy", "register_hf_deploy_tool"),
        ("tools.x402", "register_x402_tool"),
        ("tools.x402", "register_x402_invoice_tool"),
        ("tools.cronjob_tools", "register_cronjob_tool"),
        ("tools.goal_tools", "register_goal_tool"),
        ("tools.x_browser", "register_x_browser_tool"),
        ("tools.launchpad", "register_launchpad_tool"),
        ("tools.dapp_browser", "register_dapp_browser_tool"),
    )
    for mod_name, fn_name in registrars:
        try:
            mod = __import__(mod_name, fromlist=[fn_name])
            getattr(mod, fn_name)(force=True)
        except Exception:  # optional dep / import-time gate — shrinks coverage only
            continue


def _container_tool_action_names() -> set:
    """Runtime names for every action reachable through a registered tool class."""
    from tools.descriptors import TOOL_DESCRIPTORS
    from core.tool_capabilities import CATALOG_ALIASES

    names = set()
    for descriptor_id, desc in TOOL_DESCRIPTORS.items():
        cls = getattr(desc, "tool_class", None)
        if cls is None:
            continue
        # A tool's capability/gate id can differ from its descriptor id
        # (browser_manager -> browser); gates are keyed on the capability id.
        tool_id = CATALOG_ALIASES.get(descriptor_id, descriptor_id)
        for attr in dir(cls):
            if attr.startswith("_"):
                continue
            try:
                member = inspect.getattr_static(cls, attr)
            except Exception:
                continue
            if not callable(member):
                continue
            if not (hasattr(member, "action_info") or hasattr(member, "_description")):
                continue
            names.add(_namespaced(tool_id, attr))
            names.add(_namespaced(descriptor_id, attr))
    return names


def _direct_action_names() -> set:
    """Names of closures decorated with ``@...registry.action(...)``."""
    names = set()
    for rel in _DIRECT_ACTION_MODULES:
        tree = ast.parse((REPO / rel).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                call = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(call, ast.Attribute) and call.attr == "action":
                    names.add(node.name)
    return names


@pytest.fixture(scope="module")
def runtime_action_names() -> set:
    """Derive the runtime name set, then RESTORE the global tool registry.

    ``register_optional_tool(force=True)`` mutates process-global state
    (``TOOL_DESCRIPTORS`` gains rows, existing rows gain ``.tool_class``, and
    ``TOOL_COMPONENTS`` grows). Leaving that in place makes flag-gated tools look
    registered to every later test in the same process — it broke
    test_tool_registration_parity.py's vocabulary check when the two ran together.
    Snapshot and roll back so this file stays order-independent.
    """
    from tools.descriptors import TOOL_COMPONENTS, TOOL_DESCRIPTORS

    before_keys = set(TOOL_DESCRIPTORS)
    before_classes = {k: getattr(v, "tool_class", None) for k, v in TOOL_DESCRIPTORS.items()}
    before_components = list(TOOL_COMPONENTS)
    try:
        _register_every_optional_tool()
        names = _container_tool_action_names() | _direct_action_names()
        assert len(names) > 50, (
            f"only derived {len(names)} action names — the derivation broke, so every "
            "parity assertion below would vacuously pass"
        )
        yield names
    finally:
        for key in set(TOOL_DESCRIPTORS) - before_keys:
            TOOL_DESCRIPTORS.pop(key, None)
        for key, cls in before_classes.items():
            if key in TOOL_DESCRIPTORS:
                TOOL_DESCRIPTORS[key].tool_class = cls
        TOOL_COMPONENTS[:] = before_components


def test_derivation_finds_the_known_namespaced_and_direct_names(runtime_action_names):
    """Self-check: if this ever fails, the derivation is wrong and the parity
    assertions below are meaningless."""
    for expected in ("x402_invoice_x402_request", "email_send", "code_execution_run_code"):
        assert expected in runtime_action_names, f"derivation missed {expected}"
    for expected in ("message", "agent_status", "contact_history", "owner_doc_manage"):
        assert expected in runtime_action_names, f"derivation missed direct action {expected}"
    # A bare verb that the runtime never emits must NOT appear.
    assert "send_email" not in runtime_action_names
    assert "x402_request" not in runtime_action_names


def test_payment_approval_tools_are_real_action_names(runtime_action_names):
    """Every money verb gated by PAYMENT_APPROVAL_MODE must be dispatchable.

    make_approval_hook does an exact `action_name not in required` match, so a name
    the runtime never emits leaves that verb completely ungated.
    """
    from core.config_policy import PAYMENT_APPROVAL_TOOLS

    dead = sorted(set(PAYMENT_APPROVAL_TOOLS) - runtime_action_names)
    assert not dead, (
        f"PAYMENT_APPROVAL_TOOLS entries {dead} match no registered action — the "
        "owner approval lane never fires for them. Container-tool actions register "
        "as {tool_id}_{action}."
    )


def test_payment_receive_subset_is_real(runtime_action_names):
    from core.config_policy import PAYMENT_APPROVAL_TOOLS, PAYMENT_RECEIVE_APPROVAL_TOOLS

    dead = sorted(set(PAYMENT_RECEIVE_APPROVAL_TOOLS) - runtime_action_names)
    assert not dead, f"PAYMENT_RECEIVE_APPROVAL_TOOLS entries {dead} are not real actions"
    stray = sorted(set(PAYMENT_RECEIVE_APPROVAL_TOOLS) - set(PAYMENT_APPROVAL_TOOLS))
    assert not stray, (
        f"{stray} carve out a receive-side lane for verbs that are not payment-gated "
        "at all — the spend/receive split only means something inside the gated set"
    )


def test_correspondent_reply_exemption_actions_are_real(runtime_action_names):
    """The D1 scoped-reply exemption must name actions that exist, or the feature is
    silently dead for that surface."""
    from agents.task.agent.core.correspondent_gate import _REPLY_ACTIONS

    dead = sorted(set(_REPLY_ACTIONS) - runtime_action_names)
    assert not dead, (
        f"_REPLY_ACTIONS entries {dead} match no registered action — the scoped "
        "tainted-reply exemption can never fire for them"
    )


# Verbs for tools that do not exist yet. Listing them early is fine (they gate the
# day the tool lands), but they must be declared here so real typos can't hide among
# them. Shrink this set when the tool ships; never grow it to silence a failure.
ASPIRATIONAL_VERBS = frozenset({"self_modify", "mcp_install", "tool_manage"})


def test_default_approval_required_tools_are_real(runtime_action_names):
    from tools.controller.approval import (
        DEFAULT_APPROVAL_REQUIRED_TOOLS,
        POSTURE2_APPROVAL_REQUIRED_TOOLS,
    )

    for label, names in (
        ("DEFAULT_APPROVAL_REQUIRED_TOOLS", DEFAULT_APPROVAL_REQUIRED_TOOLS),
        ("POSTURE2_APPROVAL_REQUIRED_TOOLS", POSTURE2_APPROVAL_REQUIRED_TOOLS),
    ):
        dead = sorted(set(names) - runtime_action_names - ASPIRATIONAL_VERBS)
        assert not dead, (
            f"{label} entries {dead} match no registered action — those verbs are "
            "listed as approval-gated but the hook can never match them"
        )


    # The MONEY and EXEC verbs the gate module explicitly claims name-level parity for
    # ("so a resolver fault can't open …"). These are the verbs whose cost of slipping
    # is irreversible, so they must be blocked by the NAME layer with no tool_id
    # passed — not merely by tool-id resolution.
    #
    # This is deliberately NOT "every verb of every high_impact tool": Layer 2 exists
    # precisely so whole tools are covered without a 60-name hand-list that drifts.
    # Only the verbs the module singles out are pinned here.
_NAME_PARITY_VERBS = (
    "code_execution_run_code",
    "shell_run",
    "x402_invoice_x402_request",
    "x402_invoice_accounting",
    "x402_invoice_x402_invoices",
    "x402_pay_x402_fetch",
    "hf_deploy_deploy",
    "hf_deploy_undeploy",
    "email_send",
    "self_env_patch_source",
    "self_env_install_dep",
    # 023 T3/T4: the on-chain money verbs — irreversible, self-custodial.
    "defi_trade_transfer",
    "defi_trade_swap",
    "defi_trade_approve_token",
    "defi_trade_revoke_approval",
)


def test_money_and_exec_verbs_are_blocked_by_the_name_layer_alone(runtime_action_names):
    """A resolver fault must not open the irreversible verbs.

    ``is_high_impact`` is the name-only decision (no tool_id), i.e. exactly what the
    gate falls back to when ``get_action_details`` returns None or raises. Every verb
    below must survive that fallback. The bare-vs-namespaced bug broke this silently
    for most of them while the module's comments claimed the opposite.
    """
    from agents.task.agent.core.correspondent_gate import is_high_impact

    missing_from_runtime = [v for v in _NAME_PARITY_VERBS if v not in runtime_action_names]
    assert not missing_from_runtime, (
        f"{missing_from_runtime} are not derivable runtime names — either the tool was "
        "renamed or the derivation broke; this check would otherwise pass vacuously"
    )
    unprotected = [v for v in _NAME_PARITY_VERBS if not is_high_impact(v)]
    assert not unprotected, (
        f"{unprotected} are NOT blocked by the name layer — only tool-id resolution "
        "stands between a correspondent-tainted session and these verbs, which is the "
        "single point of failure the name entries exist to remove"
    )


def test_directly_registered_high_impact_actions_have_no_layer_2_to_fall_back_on():
    """Directly-registered actions carry no owning tool_id, so Layer 2 can never cover
    them: if the name layer misses one, it is simply ungated while tainted.

    This is the hole that let `owner_doc_manage` and `contact_history` through.
    """
    from agents.task.agent.core.correspondent_gate import is_high_impact_call

    for name in ("owner_doc_manage", "contact_history", "session_search",
                 "memory_search", "recent_activity", "load_tool", "agent_status",
                 "usage_summary", "preferences", "message", "skill_manage",
                 "self_context_manage", "memory"):
        assert is_high_impact_call(name, None) is True, (
            f"{name} is directly-registered (tool_id=None) and NOT blocked — a "
            "correspondent-tainted session can call it outright"
        )


def test_name_layer_blocks_every_trade_verb_under_its_venue_namespace(runtime_action_names):
    """Trading venues are readable-while-tainted by design, but their TRADE verbs
    must still be blocked under the venue-namespaced runtime name."""
    from agents.task.agent.core.correspondent_gate import (
        _HIGH_IMPACT_VERB_SUBSTRINGS, is_high_impact,
    )

    checked = 0
    for venue in ("hyperliquid", "polymarket"):
        for name in sorted(runtime_action_names):
            if not name.startswith(f"{venue}_"):
                continue
            if any(sub in name for sub in _HIGH_IMPACT_VERB_SUBSTRINGS):
                assert is_high_impact(name), f"trade verb {name} slips the gate"
                checked += 1
            elif name.startswith(f"{venue}_get_"):
                assert not is_high_impact(name), (
                    f"read verb {name} must stay available while tainted"
                )
    assert checked > 0, "no venue trade verbs derived — the check went vacuous"


def test_high_impact_names_has_no_unreachable_entries(runtime_action_names):
    """Every ``_HIGH_IMPACT_NAMES`` entry must be able to match something: a real
    runtime action, a legacy tool_id token, or a declared aspirational verb.

    An entry that can match nothing is not harmless — it reads as coverage in review
    and in the module's own comments while defending nothing.
    """
    from agents.task.agent.core.correspondent_gate import (
        _HIGH_IMPACT_NAMES, _HIGH_IMPACT_VERB_SUBSTRINGS,
    )
    from core.tool_capabilities import TOOL_CAPABILITIES

    tool_id_tokens = set(TOOL_CAPABILITIES) | ASPIRATIONAL_VERBS
    # A bare verb kept in sync with the substring layer is reachable: the substring
    # matcher catches every namespaced runtime form of it.
    substring_backed = set(_HIGH_IMPACT_VERB_SUBSTRINGS)

    dead = sorted(
        n for n in _HIGH_IMPACT_NAMES
        if n not in runtime_action_names
        and n not in tool_id_tokens
        and n not in substring_backed
    )
    assert not dead, (
        f"_HIGH_IMPACT_NAMES entries {dead} can never match anything — not a runtime "
        "action name, not a tool_id token, not substring-backed. Use the namespaced "
        "runtime name ({tool_id}_{action}), or drop the entry."
    )
