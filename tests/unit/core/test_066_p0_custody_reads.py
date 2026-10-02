"""066 Phase 0 — close the reads of the wallet seed.

P0.1 the custody process is non-dumpable (prctl), P0.2 the key material leaves
``os.environ`` once the wallet config loads, P0.3 the Playwright driver gets a
scrubbed env. P0.4 (ledger 0600) is run in
tests/unit/deployment/test_deploy_wallet_ledger_perms.py.
"""
import ast
import inspect
import os
import subprocess
import sys
import textwrap

import pytest

import core.security.process_hardening as ph
from core.security import custody_env

SEED = "ab" * 24  # 48 hex chars: a live-length legacy seed (no BIP-39 needed)
SECRET_NAMES = custody_env.CUSTODY_SECRET_ENV


@pytest.fixture
def custody(monkeypatch):
    for n in SECRET_NAMES:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_DERIVATION", "legacy")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    monkeypatch.setenv("PAYMENT_MASTER_SEED", "p" * 40)
    monkeypatch.setenv("EIP8004_AGENT_PRIVATE_KEY", "0x" + "11" * 32)
    # never make the pytest process itself non-dumpable
    monkeypatch.setattr(ph, "_apply", lambda: ph.HardeningResult(ph.STATE_HELD, "test"))


# --- P0.2: no secret remains in os.environ after the load ------------------- #

def test_load_takes_every_custody_secret_out_of_os_environ(custody):
    from core.wallet.config import load_wallet_config
    cfg = load_wallet_config()
    assert cfg.master_seed == SEED
    assert [n for n in SECRET_NAMES if n in os.environ] == []
    assert custody_env.custody_loaded()
    # a second load in the same process sees the SAME seed (the held copy)
    assert load_wallet_config().master_seed == SEED


def test_a_child_cannot_read_the_secret_from_its_env(custody):
    from core.wallet.config import load_wallet_config
    load_wallet_config()
    code = ("import os,json;print(json.dumps({n: os.environ.get(n) for n in %r}))"
            % (list(SECRET_NAMES),))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout
    assert SEED not in out and "p" * 40 not in out
    assert set(__import__("json").loads(out).values()) == {None}


def test_an_explicit_env_mapping_is_never_popped(custody):
    from core.wallet.config import load_wallet_config
    mapping = {"AGENT_WALLET_ENABLED": "true", "AGENT_WALLET_DERIVATION": "legacy",
               "AGENT_WALLET_MASTER_SEED": SEED}
    assert load_wallet_config(mapping).master_seed == SEED
    assert mapping["AGENT_WALLET_MASTER_SEED"] == SEED
    assert os.environ.get("AGENT_WALLET_MASTER_SEED") == SEED  # untouched: not a process load
    assert not custody_env.custody_loaded()


def test_a_config_that_raises_still_takes_the_seed(custody, monkeypatch):
    from core.wallet.config import load_wallet_config
    monkeypatch.setenv("AGENT_WALLET_MAX_PER_TX_USD", "not-a-number")
    with pytest.raises(ValueError):
        load_wallet_config()
    assert "AGENT_WALLET_MASTER_SEED" not in os.environ


def test_every_later_reader_uses_the_held_copy(custody):
    from core.payment_config import resolve_master_seed
    from core.secret_scrub import scrub_loaded_seed
    from core.security.host_execution import wallet_custody_enabled
    from core.wallet.config import load_wallet_config
    load_wallet_config()
    assert resolve_master_seed() == "p" * 40
    assert custody_env.custody_secret("EIP8004_AGENT_PRIVATE_KEY") == "0x" + "11" * 32
    assert SEED not in scrub_loaded_seed(f"err: {SEED}")
    os.environ.pop("AGENT_WALLET_ENABLED")
    assert wallet_custody_enabled()  # the seed is held, custody still counts


def test_a_value_set_after_the_load_wins_and_is_taken_again(custody):
    """`polyrob wallet init` writes the new seed into os.environ after a load."""
    from core.wallet.config import load_wallet_config
    load_wallet_config()
    os.environ["AGENT_WALLET_MASTER_SEED"] = "cd" * 24
    assert load_wallet_config().master_seed == "cd" * 24
    assert "AGENT_WALLET_MASTER_SEED" not in os.environ


def test_a_later_env_load_cannot_put_the_seed_back(custody):
    from core.wallet.config import load_wallet_config
    load_wallet_config()
    os.environ["AGENT_WALLET_MASTER_SEED"] = SEED  # what load_dotenv would do
    custody_env.repop_after_env_load()
    assert "AGENT_WALLET_MASTER_SEED" not in os.environ
    assert custody_env.custody_secret("AGENT_WALLET_MASTER_SEED") == SEED
    # and load_env runs that repop (the hook is wired, not only defined)
    from core import bootstrap
    assert "repop_after_env_load()" in inspect.getsource(bootstrap.load_env)


def test_doctor_sees_a_loaded_seed(custody):
    from core.wallet.config import load_wallet_config
    load_wallet_config()
    env = custody_env.custody_environ()
    assert env["AGENT_WALLET_MASTER_SEED"] == SEED


def test_derived_addresses_are_unchanged_by_the_pop(custody, tmp_path):
    """The wallet_continuity derivation (an env MAPPING, before any pop) and the
    runtime wallet (process env, after the pop) name the same address."""
    pytest.importorskip("eth_account")
    scripts_dir = __import__("pathlib").Path(__file__).resolve().parents[3] / "scripts"
    if not (scripts_dir / "wallet_continuity.py").exists():
        pytest.skip("scripts/wallet_continuity.py is private tooling")
    sys.path.insert(0, str(scripts_dir))
    try:
        import wallet_continuity
    finally:
        sys.path.pop(0)
    from core.wallet.agent_wallet import AgentWallet
    from core.wallet.config import load_wallet_config
    before, _ = wallet_continuity.derive_addresses(dict(os.environ), tmp_path)
    after = AgentWallet(load_wallet_config()).address
    assert "AGENT_WALLET_MASTER_SEED" not in os.environ
    again = AgentWallet(load_wallet_config()).address
    assert before == after == again


# --- P0.1: prctl ------------------------------------------------------------ #

class _FakeLibc:
    def __init__(self, set_rc=0, get_value=0):
        self.calls = []
        self._set_rc, self._get = set_rc, get_value

    def prctl(self, option, *args):
        self.calls.append(option)
        return self._set_rc if option == ph.PR_SET_DUMPABLE else self._get


def test_prctl_path_holds_on_linux(monkeypatch):
    fake = _FakeLibc()
    monkeypatch.setattr(ph, "_is_linux", lambda: True)
    monkeypatch.setattr(ph, "_libc", lambda: fake)
    res = ph.harden_custody_process()
    assert res.held and fake.calls == [ph.PR_SET_DUMPABLE, ph.PR_GET_DUMPABLE]
    assert ph.hardening_state().state == ph.STATE_HELD
    assert ph.custody_reads_closed()
    ph.harden_custody_process()
    assert fake.calls == [ph.PR_SET_DUMPABLE, ph.PR_GET_DUMPABLE]  # idempotent


@pytest.mark.parametrize("set_rc,get_value", [(-1, 1), (0, 1)])
def test_prctl_failure_is_failed_never_held(monkeypatch, set_rc, get_value):
    monkeypatch.setattr(ph, "_is_linux", lambda: True)
    monkeypatch.setattr(ph, "_libc", lambda: _FakeLibc(set_rc, get_value))
    res = ph.harden_custody_process()
    assert res.state == ph.STATE_FAILED
    assert not ph.custody_reads_closed()


def test_non_linux_is_a_reported_no_op(monkeypatch):
    monkeypatch.setattr(ph, "_is_linux", lambda: False)
    monkeypatch.setattr(ph, "_libc", lambda: pytest.fail("prctl called off Linux"))
    assert ph.harden_custody_process().state == ph.STATE_UNSUPPORTED


def test_load_wallet_config_hardens_before_it_reads_the_seed(custody, monkeypatch):
    """Ratchet: the harden call precedes the first wallet read."""
    import core.wallet.config as wc
    order = []
    monkeypatch.setattr(ph, "harden_custody_process",
                        lambda: order.append("harden") or ph.HardeningResult(ph.STATE_HELD))
    real = custody_env.custody_secret

    def spy(name, env=None):
        order.append(("read", name))
        return real(name, env)

    monkeypatch.setattr(custody_env, "custody_secret", spy)
    wc.load_wallet_config()
    assert order and order[0] == "harden", order
    # and structurally: the public entry calls _harden_if_custody before _load_wallet_config
    src = textwrap.dedent(inspect.getsource(wc.load_wallet_config))
    calls = [n.func.id for n in sorted(
        (n for n in ast.walk(ast.parse(src))
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)),
        key=lambda n: (n.lineno, n.col_offset))]
    assert calls.index("_harden_if_custody") < calls.index("_load_wallet_config")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="real /proc + prctl: Linux only")
def test_real_proc_environ_is_unreadable_by_a_same_uid_child(tmp_path):
    parent = textwrap.dedent(f"""
        import os, subprocess, sys
        os.environ["POLYROB_066_CANARY"] = "x"  # not in the exec block; the real one is below
        from core.security.process_hardening import harden_custody_process
        assert harden_custody_process().held
        child = "import sys; open('/proc/%d/environ' % int(sys.argv[1]),'rb').read()"
        r = subprocess.run([sys.executable, "-c", child, str(os.getpid())],
                           capture_output=True, text=True)
        print("DENIED" if "PermissionError" in r.stderr else "READ:" + r.stderr)
    """)
    env = dict(os.environ, CANARY_SECRET_066="fake-seed-material")
    root = str(__import__("pathlib").Path(__file__).resolve().parents[3])
    out = subprocess.run([sys.executable, "-c", parent], capture_output=True, text=True,
                         cwd=root, env=env, timeout=60)
    assert "DENIED" in out.stdout, out.stdout + out.stderr


# --- P0.3: the Playwright driver env ---------------------------------------- #

def test_the_playwright_driver_env_is_scrubbed(monkeypatch):
    transport = pytest.importorskip("playwright._impl._transport")
    from tools.browser.child_env import install_driver_env_scrub
    monkeypatch.setattr(transport, "get_driver_env", transport.get_driver_env)
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw")
    assert install_driver_env_scrub() is True
    assert install_driver_env_scrub() is True  # idempotent: not wrapped twice
    env = transport.get_driver_env()
    assert "AGENT_WALLET_MASTER_SEED" not in env and "OPENAI_API_KEY" not in env
    assert SEED not in env.values() and "sk-leak" not in env.values()
    assert env.get("PW_LANG_NAME") == "python"  # the driver's own markers survive
    assert env.get("PLAYWRIGHT_BROWSERS_PATH") == "/opt/pw"
    assert "PATH" in env


def test_browser_init_installs_the_driver_scrub_before_start():
    from tools.browser import browser
    src = inspect.getsource(browser.Browser._init) if hasattr(browser, "Browser") else \
        inspect.getsource(browser)
    assert src.index("install_driver_env_scrub()") < src.index("async_playwright().start()")


# --- status ----------------------------------------------------------------- #

def test_custody_status_section_reports_this_process(custody):
    from core.status_custody import custody_section
    from core.wallet.config import load_wallet_config
    ph.harden_custody_process()
    load_wallet_config()
    sec = custody_section()
    assert sec.data["holds_seed"] and sec.data["loaded"]
    assert sec.data["secrets_in_env"] == []
    assert "non-dumpable held" in sec.lines[0] and "env scrubbed" in sec.lines[0]
    assert not sec.health
    assert SEED not in repr(sec)


def test_custody_status_flags_a_dumpable_linux_process(custody, monkeypatch):
    from core.status_custody import custody_section
    monkeypatch.setattr(ph, "_is_linux", lambda: True)
    sec = custody_section()  # never hardened in this process
    assert [h.key for h in sec.health] == ["custody_dumpable"]


def test_custody_status_without_custody_is_ok(monkeypatch):
    from core.status_custody import custody_section
    for n in SECRET_NAMES + ("AGENT_WALLET_ENABLED",):
        monkeypatch.delenv(n, raising=False)
    sec = custody_section()
    assert sec.data == {"custody": False} and not sec.health
