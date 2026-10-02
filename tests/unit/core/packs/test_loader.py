"""The pack loader (067 P2): phase 1 registers policy DATA, phase 2 loads code;
every refusal is per pack and named."""
import pytest

from core.packs import state
from tests.unit.core.packs.conftest import write_pack

ECHO_LIKE = '''id = "{id}"
version = "0.1.0"
pack_api = 1
tier = "{tier}"
requires_core = "{core}"
requires_packs = {deps}
capabilities = ["tools"]

[tools.{id}t]
capabilities = []

[tools.{id}t.verbs.{id}t_read]
effect = "none"
'''


def _toml(pid, tier="first-party", core="", deps="[]"):
    return ECHO_LIKE.format(id=pid, tier=tier, core=core, deps=deps)


def _init(pid, extra=""):
    return ("from core.packs.spec import PackSpec, ToolContribution\n"
            f"def pack():\n    return PackSpec(id={pid!r}, tools=(ToolContribution("
            f"id={pid + 't'!r}, registrar=lambda: True),){extra})\n")


def test_phase_one_registers_rows_without_importing_the_pack(use_packs):
    import sys
    from core.tool_capabilities import is_classified
    from core.verb_policy import policy_for
    loader = use_packs("echo")
    loader.register_policies()
    rec = state.record("echo")
    assert rec.status == state.INSTALLED, rec.reason
    assert is_classified("echo") and policy_for("echo_say").effect == "none"
    assert state.pack_of_tool("echo") == "echo"
    assert "polyrob_echo" not in sys.modules


def test_phase_two_loads_the_enabled_first_party_pack(use_packs):
    from core.tool_gates import gate_on
    from tools.descriptors import get_tool_class
    loader = use_packs("echo")
    loader.load_packs()
    rec = state.record("echo")
    assert rec.status == state.LOADED, rec.reason
    assert get_tool_class("echo").__name__ == "EchoTool"
    assert gate_on("echo")
    assert set(rec.commands) == {"echo"}
    assert state.skill_dirs() and state.skill_dirs()[0][0] == "echo"
    assert state.action_refusal("echo", "echo_say") is None
    assert "no policy row" in state.action_refusal("echo", "echo_unlisted")
    assert state.action_refusal("filesystem", "anything") is None


def test_the_emitted_actions_all_have_rows(use_packs):
    """Every action the pack's tool class declares has a policy row."""
    import inspect
    from core.verb_policy import policy_for
    from tools.descriptors import get_tool_class
    use_packs("echo").load_packs()
    cls = get_tool_class("echo")
    names = {a for a in dir(cls) if not a.startswith("_")
             and hasattr(inspect.getattr_static(cls, a), "_description")}
    assert names == {"echo_say"}
    assert all(policy_for(n) for n in names)


@pytest.mark.parametrize("env, status, why", [
    ({"POLYROB_PACKS_DISABLED": "echo"}, state.DISABLED, "POLYROB_PACKS_DISABLED"),
    ({"POLYROB_PACKS": "other"}, state.DISABLED, "not named in POLYROB_PACKS"),
    ({"POLYROB_PACKS": "echo", "POLYROB_PACKS_DISABLED": "echo"}, state.DISABLED,
     "POLYROB_PACKS_DISABLED"),
    ({"POLYROB_PACKS": " Echo , x"}, state.LOADED, ""),
])
def test_the_enabled_set(use_packs, monkeypatch, env, status, why):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    loader = use_packs("echo")
    loader.load_packs()
    rec = state.record("echo")
    assert rec.status == status and why in rec.reason
    if status == state.DISABLED:
        assert state.action_refusal("echo", "echo_say").startswith("pack 'echo' is disabled")
        assert state.skill_dirs() == []


@pytest.mark.parametrize("named, status", [(None, state.DISABLED), ("tp", state.LOADED)])
def test_a_third_party_pack_loads_only_when_named(scratch, tmp_path, monkeypatch, named, status):
    write_pack(tmp_path, monkeypatch, "tp", _toml("tp", tier="third-party"), _init("tp"))
    from importlib.metadata import EntryPoint
    monkeypatch.setattr(scratch, "_entry_points",
                        lambda: [EntryPoint("tp", "polyrob_tp:pack", "polyrob.packs")])
    monkeypatch.setattr(scratch, "custody_refusal", lambda: None)
    if named:
        monkeypatch.setenv("POLYROB_PACKS", named)
    scratch.load_packs()
    rec = state.record("tp")
    assert rec.status == status, rec.reason
    if status == state.DISABLED:
        assert "third-party" in rec.reason


def test_a_third_party_pack_is_refused_under_custody(scratch, tmp_path, monkeypatch):
    write_pack(tmp_path, monkeypatch, "tp", _toml("tp", tier="third-party"),
               "raise RuntimeError('third-party code ran')\n")
    from importlib.metadata import EntryPoint
    monkeypatch.setattr(scratch, "_entry_points",
                        lambda: [EntryPoint("tp", "polyrob_tp:pack", "polyrob.packs")])
    monkeypatch.setenv("POLYROB_PACKS", "tp")
    import core.security.host_execution as he
    monkeypatch.setattr(he, "wallet_custody_enabled", lambda: True)
    monkeypatch.setenv("WALLET_SIGNER", "local")
    scratch.load_packs()
    rec = state.record("tp")
    assert rec.status == state.REFUSED and "wallet custody" in rec.reason


def test_custody_rule_allows_a_remote_signer_without_secrets(monkeypatch):
    from core.packs import loader
    import core.security.custody_env as ce
    import core.security.host_execution as he
    monkeypatch.setattr(he, "wallet_custody_enabled", lambda: False)
    assert loader.custody_refusal() is None
    monkeypatch.setattr(he, "wallet_custody_enabled", lambda: True)
    monkeypatch.setenv("WALLET_SIGNER", "remote")
    monkeypatch.setattr(ce, "holds_custody_secret", lambda: False)
    assert loader.custody_refusal() is None
    monkeypatch.setattr(ce, "holds_custody_secret", lambda: True)
    assert "WALLET_SIGNER" in loader.custody_refusal()


def _install(scratch, monkeypatch, packs):
    from importlib.metadata import EntryPoint
    monkeypatch.setattr(scratch, "_entry_points", lambda: [
        EntryPoint(pid, f"polyrob_{pid}:pack", "polyrob.packs") for pid in packs])


def test_static_refusals_are_named(scratch, tmp_path, monkeypatch):
    write_pack(tmp_path, monkeypatch, "old", _toml("old", core="<0.1"))
    write_pack(tmp_path, monkeypatch, "liar", _toml("liar").replace(
        'capabilities = ["tools"]', 'capabilities = ["tools", "money"]'))
    write_pack(tmp_path, monkeypatch, "named", _toml("other"))
    write_pack(tmp_path, monkeypatch, "future", _toml("future").replace("pack_api = 1",
                                                                          "pack_api = 9"))
    _install(scratch, monkeypatch, ["old", "liar", "named", "future"])
    scratch.register_policies()
    reasons = {r.id: r.reason for r in state.records()}
    assert all(state.record(p).status == state.REFUSED for p in reasons)
    assert "requires polyrob <0.1" in reasons["old"]
    assert "declared capabilities" in reasons["liar"]
    assert "differs from the entry point name" in reasons["named"]
    assert "pack_api 9 is not supported" in reasons["future"]
    from core.tool_capabilities import is_classified
    assert not any(is_classified(p + "t") for p in reasons)


def test_requires_packs_order_missing_and_cycles(scratch, tmp_path, monkeypatch):
    write_pack(tmp_path, monkeypatch, "base", _toml("base"), _init("base"))
    write_pack(tmp_path, monkeypatch, "top", _toml("top", deps='["base"]'), _init("top"))
    write_pack(tmp_path, monkeypatch, "orphan", _toml("orphan", deps='["nothere"]'))
    write_pack(tmp_path, monkeypatch, "cya", _toml("cya", deps='["cyb"]'))
    write_pack(tmp_path, monkeypatch, "cyb", _toml("cyb", deps='["cya"]'))
    _install(scratch, monkeypatch, ["top", "base", "orphan", "cya", "cyb"])
    scratch.load_packs()
    assert state.record("base").status == state.LOADED
    assert state.record("top").status == state.LOADED
    assert scratch._ORDER.index("base") < scratch._ORDER.index("top")
    assert "'nothere', which is not installed" in state.record("orphan").reason
    assert "cycle" in state.record("cya").reason and "cycle" in state.record("cyb").reason


def test_a_dependent_of_a_disabled_pack_is_refused(scratch, tmp_path, monkeypatch):
    write_pack(tmp_path, monkeypatch, "base", _toml("base"), _init("base"))
    write_pack(tmp_path, monkeypatch, "top", _toml("top", deps='["base"]'), _init("top"))
    _install(scratch, monkeypatch, ["base", "top"])
    monkeypatch.setenv("POLYROB_PACKS_DISABLED", "base")
    scratch.load_packs()
    assert "requires pack 'base', which is disabled" in state.record("top").reason


def test_a_row_for_an_already_built_view_refuses_the_pack(scratch, tmp_path, monkeypatch):
    """The guard behind the two phases: a late phase 1 fails closed per pack."""
    from core.verb_policy import ids_where
    write_pack(tmp_path, monkeypatch, "late", _toml("late"))
    _install(scratch, monkeypatch, ["late"])
    ids_where(effect="none")        # a view built before phase 1 ran
    scratch.register_policies()
    rec = state.record("late")
    assert rec.status == state.REFUSED
    assert "would change a policy view" in rec.reason and "test_loader" in rec.reason
    from core.tool_capabilities import is_classified
    assert not is_classified("latet"), "all or nothing: the tool row did not register"


def test_permissions_register_and_a_taken_row_refuses(scratch, tmp_path, monkeypatch):
    from core import tool_capabilities as tc
    toml = _toml("perm").replace("capabilities = []\n",
                                 'capabilities = []\npermissions = ["network.read"]\n', 1)
    write_pack(tmp_path, monkeypatch, "perm", toml)
    tc.TOOL_PERMISSIONS["permt"] = ("fs.read",)
    _install(scratch, monkeypatch, ["perm"])
    scratch.register_policies()
    rec = state.record("perm")
    assert rec.status == state.REFUSED and "catalog permission row" in rec.reason
    assert not tc.is_classified("permt"), "all or nothing"
    del tc.TOOL_PERMISSIONS["permt"]
    scratch.reset_for_tests()
    _install(scratch, monkeypatch, ["perm"])
    scratch.register_policies()
    assert state.record("perm").status == state.INSTALLED, state.record("perm").reason
    assert tc.TOOL_PERMISSIONS["permt"] == ("network.read",)


def test_a_tool_id_in_a_core_namespace_is_refused(scratch, tmp_path, monkeypatch):
    toml = _toml("web").replace("[tools.webt", "[tools.web").replace("webt_read", "web_read")
    write_pack(tmp_path, monkeypatch, "web", toml)
    _install(scratch, monkeypatch, ["web"])
    scratch.register_policies()
    assert "action namespace of 'web_fetch'" in state.record("web").reason


_NESTED = '''id = "nest"
version = "0.1.0"
pack_api = 1
tier = "first-party"
capabilities = ["tools"]

[tools.nestt]
capabilities = []

[tools.nestt.verbs.nestt_read]
effect = "none"
{stray}
[tools.nestt_data]
capabilities = []

[tools.nestt_data.verbs.nestt_data_read]
'''


def test_a_pack_may_nest_its_own_tool_ids(scratch, tmp_path, monkeypatch):
    """067 P4 (markets: polymarket / polymarket_data): one pack's nested ids load,
    and each action row names its own tool."""
    from core.verb_policy import policy_for
    write_pack(tmp_path, monkeypatch, "nest", _NESTED.format(stray=""))
    _install(scratch, monkeypatch, ["nest"])
    scratch.register_policies()
    assert state.record("nest").status == state.INSTALLED, state.record("nest").reason
    assert policy_for("nestt_data_read").tool == "nestt_data"
    assert policy_for("nestt_read").tool == "nestt"


def test_a_verb_of_the_shorter_id_in_the_nested_namespace_is_refused(scratch, tmp_path,
                                                                      monkeypatch):
    stray = '[tools.nestt.verbs.nestt_data_write]\neffect = "none"\n'
    write_pack(tmp_path, monkeypatch, "nest", _NESTED.format(stray=stray))
    _install(scratch, monkeypatch, ["nest"])
    scratch.register_policies()
    rec = state.record("nest")
    assert rec.status == state.REFUSED
    assert "nestt_data_write" in rec.reason and "'nestt_data'" in rec.reason


@pytest.mark.parametrize("init, match", [
    ("from core.packs.spec import PackSpec\ndef pack():\n    return PackSpec(id='cx')\n",
     "tools in pack.toml"),
    ("def pack():\n    return {}\n", "not a PackSpec"),
    ("raise ImportError('boom')\n", "import failed: ImportError: boom"),
    ("from core.packs.spec import PackSpec, ToolContribution\ndef pack():\n"
     "    return PackSpec(id='cx', tools=(ToolContribution('cxt', lambda: True),),"
     " hooks={'nope': 1})\n", "unknown hook"),
    ("from core.packs.spec import PackSpec, ToolContribution\ndef pack():\n"
     "    return PackSpec(id='cx', tools=(ToolContribution('cxt', lambda: True),),"
     " api_routers=('x:y',))\n", "api_routes"),
    ("from core.packs.spec import PackSpec, ToolContribution\ndef boom():\n"
     "    raise RuntimeError('registrar broke')\ndef pack():\n"
     "    return PackSpec(id='cx', tools=(ToolContribution('cxt', boom),))\n",
     "contribution failed: RuntimeError: registrar broke"),
])
def test_code_half_refusals(scratch, tmp_path, monkeypatch, init, match):
    write_pack(tmp_path, monkeypatch, "cx", _toml("cx"), init)
    _install(scratch, monkeypatch, ["cx"])
    scratch.load_packs()
    rec = state.record("cx")
    assert rec.status == state.REFUSED and match in rec.reason
    assert "is refused" in state.action_refusal("cxt", "cxt_read")


def test_hooks_reach_the_core_seams(scratch, tmp_path, monkeypatch):
    from core import boot_reconcilers, token_check_hook
    monkeypatch.setattr(boot_reconcilers, "_RECONCILERS", {})
    monkeypatch.setattr(token_check_hook, "_checker", None)
    init = ("from core.packs.spec import PackSpec, ToolContribution\n"
            "async def rec():\n    return 0\n"
            "async def chk(tool, addr):\n    return {}\n"
            "def pack():\n    return PackSpec(id='hk', tools=(ToolContribution('hkt', "
            "lambda: True),), hooks={'autonomy.boot_reconciler': {'hkt': rec}, "
            "'identity.token_checker': chk})\n")
    write_pack(tmp_path, monkeypatch, "hk", _toml("hk"), init)
    _install(scratch, monkeypatch, ["hk"])
    scratch.load_packs()
    assert state.record("hk").status == state.LOADED, state.record("hk").reason
    assert boot_reconcilers.boot_reconciler("hkt").__name__ == "rec"
    assert token_check_hook.token_checker().__name__ == "chk"


def test_a_reconciler_for_a_foreign_tool_refuses_the_pack(scratch, tmp_path, monkeypatch):
    from core import boot_reconcilers
    monkeypatch.setattr(boot_reconcilers, "_RECONCILERS", {})
    init = ("from core.packs.spec import PackSpec, ToolContribution\n"
            "def pack():\n    return PackSpec(id='hj', tools=(ToolContribution('hjt', "
            "lambda: True),), hooks={'autonomy.boot_reconciler': {'hf_deploy': print}})\n")
    write_pack(tmp_path, monkeypatch, "hj", _toml("hj"), init)
    _install(scratch, monkeypatch, ["hj"])
    scratch.load_packs()
    assert "not this pack's tool" in state.record("hj").reason
    assert boot_reconcilers.boot_reconciler("hf_deploy") is None


def test_a_broken_scan_is_the_discovery_error(scratch, monkeypatch):
    def boom():
        raise OSError("metadata unreadable")
    monkeypatch.setattr(scratch, "_entry_points", boom)
    scratch.load_packs()
    assert "metadata unreadable" in state.discovery_error()
    assert "discovery failed" in state.summary_line()
