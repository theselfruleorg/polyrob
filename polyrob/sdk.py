"""The pack SDK: the host names a pack may import (W11).

A pack (``packs/*``, or a third-party distribution) imports host code ONLY from
here — never a host module's private name. ``tests/test_pack_sdk_facade.py``
ratchets it: a pack may not import a private host name at all, and a direct
import of any other host module is a shrink-only allowlist row.

Resolution is LAZY and happens on every attribute access (PEP 562
``__getattr__``): importing this module imports no host module, so a pack pays
only for what it uses, and a test that monkeypatches the HOST attribute (e.g.
``tools.controller.action_registration._is_forged_or_autonomous_turn``) is seen
by a pack that imports the name at call time.

Add a name: one ``NAME: "module:attribute"`` row in ``_EXPORTS`` (and keep the
host attribute stable — this file is the contract third-party packs read).
This module sits at the TOP tier (beside the packs): it may name any host tier.
"""
import importlib
from typing import Any, Dict

#: public name -> "host.module:attribute"
_EXPORTS: Dict[str, str] = {
    # --- turn origin: the ownership / forged-turn security predicate ---------
    "is_forged_or_autonomous_turn":
        "tools.controller.action_registration:_is_forged_or_autonomous_turn",
    # W5: the ONE owner-turn definition and the ONE taint view
    "is_genuine_owner_turn": "core.security.owner_turn:is_genuine_owner_turn",
    "is_leaf_or_forged": "core.security.owner_turn:is_leaf_or_forged",
    "is_leaf": "core.security.owner_turn:is_leaf",
    "forged_kind": "core.security.owner_turn:forged_kind",
    # the ONE external-address key (dead targets, correspondents, conversations)
    "canonical_addr": "core.surfaces.address_key:canonical_addr",
    "is_room_turn": "core.security.owner_turn:is_room_turn",
    "is_owner_tenant": "core.security.owner_turn:is_owner_tenant",
    "turn_tainted": "core.security.taints:turn_tainted",
    "owner_seat_context": "core.security.turn_facts:owner_seat_context",
    # W5: an owner seat that calls a verb directly runs the policy chain's seat stages
    "run_seat_action": "tools.controller.policy_chain:run_seat_action",
    "SeatRefused": "tools.controller.policy_chain:SeatRefused",
    # --- flags and modes -----------------------------------------------------
    "bool_env": "core.env:bool_env",
    "int_env": "core.env:int_env",
    "float_env": "core.env:float_env",
    "mode_capability_default": "core.config_policy:_mode_capability_default",
    "member_flag": "core.config_policy.members:member_flag",
    # --- cron delivery (a pack's `cron.delivery_channel` hook) ---------------
    "delivery_config_and_container": "cron.delivery:_config_and_container",
    "delivery_context": "cron.delivery:_delivery_context",
    "record_delivery_write": "cron.delivery:_record_delivery_write",
    # --- chat surfaces (a pack's harness, launch and probe) ------------------
    "BaseHarness": "surfaces._shared:BaseHarness",
    "TextSink": "surfaces._shared:TextSink",
    "register_surface_and_sink": "surfaces._shared:register_surface_and_sink",
    "LaunchContext": "surfaces._launch:LaunchContext",
    "harness_launched": "surfaces._launch:harness_launched",
    "ProbeResult": "surfaces._probe:ProbeResult",
    "http_json": "surfaces._probe:http_json",
    "probe_missing": "surfaces._probe:missing",
    # --- CLI commands a pack contributes -------------------------------------
    "SurfaceJob": "cli.commands._surface_runner:SurfaceJob",
    "run_surface": "cli.commands._surface_runner:run_surface",
    "ensure_env_loaded": "cli.commands._bootstrap:ensure_env_loaded",
    "admin_data_dir": "cli._admin_home:admin_data_dir",
    "deployed_box": "cli._admin_home:_deployed_box",
    # --- workspace and execution ---------------------------------------------
    "resolve_session_workspace": "tools.controller.message_send:_resolve_session_workspace",
    "run_group": "tools.code_exec.backends._proc:run_group",
    # --- the pack contract ---------------------------------------------------
    # W4: a pack derives its verb lists from its own policy rows (pack.toml)
    "action_ids_where": "core.verb_policy:ids_where",
    "bare_action_name": "core.action_names:bare_action_name",
    "PackSpec": "core.packs.spec:PackSpec",
    "StatusSection": "core.packs.spec:StatusSection",
    "ToolContribution": "core.packs.spec:ToolContribution",
    "BaseTool": "tools.base_tool:BaseTool",
    "ToolStatus": "tools.base_tool:ToolStatus",
    "ToolDescriptor": "tools.descriptors:ToolDescriptor",
    "ToolCategory": "tools.descriptors:ToolCategory",
    "register_optional_tool": "tools.descriptors:register_optional_tool",
    "ActionResult": "agents.task.loop_types.controller_types:ActionResult",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'polyrob.sdk' has no attribute {name!r}")
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)


def __dir__():
    return __all__
