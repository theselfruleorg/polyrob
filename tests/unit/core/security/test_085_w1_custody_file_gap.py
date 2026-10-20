"""085 W1 (C-1): a wallet seed in an env file this UID can read is an isolation gap.

The in-memory isolation (pop from ``os.environ`` + non-dumpable) cannot close a
dotenv file a same-UID child can open. Under that gap, only the children that
run MODEL-WRITTEN code refuse (owner decision D1): ``run_tests``, ``run_code``,
the host shell. Git, LSP, ``rg``, MCP stdio keep running. A seed from the
process env (systemd ``EnvironmentFile``) and a box with no seed see no change.
"""
import logging
import os
import types

import pytest

import core.security.custody_env as ce
import core.security.host_execution as he
import core.security.process_hardening as ph
from core.security import settings as ss

SEED = "test-only-not-a-real-seed-" * 3


@pytest.fixture
def custody(monkeypatch, tmp_path):
    for name in (*ce.CUSTODY_SECRET_ENV, ss.ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("WALLET_SIGNER", "local")
    ce._reset_for_tests()
    monkeypatch.setattr(ph, "hardening_state",
                        lambda: ph.HardeningResult(ph.STATE_HELD, "test: held"))
    yield tmp_path
    ce._reset_for_tests()


def _env_file(tmp_path, seed=SEED):
    path = tmp_path / ".env"
    path.write_text(f"AGENT_WALLET_MASTER_SEED={seed}\n")
    path.chmod(0o600)
    return path


def _dotenv_seed(monkeypatch, tmp_path):
    """The local shape: load_env layered the home file, then the wallet loaded."""
    path = _env_file(tmp_path)
    ce.note_env_file(path, {"AGENT_WALLET_MASTER_SEED": SEED})
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    ce.take_custody_secrets()
    return path


def test_dotenv_seed_in_a_readable_file_is_a_gap(custody, monkeypatch):
    path = _dotenv_seed(monkeypatch, custody)
    assert ce.custody_secret_sources() == {"AGENT_WALLET_MASTER_SEED": f"dotenv:{path}"}
    assert ce.readable_custody_file() == str(path)
    why = he.model_code_host_refusal()
    assert why and "polyrob signer install" in why and "sandbox" in why
    assert SEED not in why and str(path) not in why


def test_d1_git_lsp_mcp_children_stay_allowed_under_the_file_gap(custody, monkeypatch):
    """host_execution_refusal is the gate of git, LSP, MCP stdio, installs and
    snapshots: the file gap does not reach it."""
    _dotenv_seed(monkeypatch, custody)
    assert he.custody_file_gap()
    assert he.host_execution_refusal() is None


def test_a_file_that_repeats_the_process_value_is_a_gap(custody, monkeypatch):
    """override=False: the process env wins, but the file still holds the seed."""
    path = _env_file(custody)
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    ce.note_env_file(path, {"AGENT_WALLET_MASTER_SEED": SEED})
    # before the wallet config loads: the live value is looked up
    assert ce.readable_custody_file() == str(path)
    ce.take_custody_secrets()
    assert ce.readable_custody_file() == str(path)


def test_load_env_feeds_the_record():
    """Every ladder file load_env layers reaches the record (the same pattern as
    the repop check in test_066_p0_custody_reads)."""
    import inspect
    import polyrob.app.bootstrap as bootstrap
    assert "note_env_file(cand.path, values)" in inspect.getsource(bootstrap.load_env)


def test_process_env_seed_is_no_gap(custody, monkeypatch):
    """The prod shape: systemd EnvironmentFile (root 0600), no ladder file holds it."""
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    ce.take_custody_secrets()
    assert ce.custody_secret_sources() == {"AGENT_WALLET_MASTER_SEED": "process_env"}
    assert he.custody_file_gap() is None
    assert he.model_code_host_refusal() is None


def test_a_ladder_file_with_another_seed_is_a_gap(custody, monkeypatch):
    """systemd set one seed, a readable ladder file holds an older one: a child
    can still read a key, so the file decides (085 reval)."""
    path = _env_file(custody, seed="another-test-value")
    ce.note_env_file(path, {"AGENT_WALLET_MASTER_SEED": "another-test-value"})
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    ce.take_custody_secrets()
    assert ce.custody_secret_sources() == {"AGENT_WALLET_MASTER_SEED": "process_env"}
    assert ce.readable_custody_file() == str(path)
    assert he.model_code_host_refusal()


def test_a_ladder_file_without_a_seed_is_no_gap(custody, monkeypatch):
    path = custody / ".env"
    path.write_text("OPENAI_API_KEY=test-only\nAGENT_WALLET_MASTER_SEED=\n")
    ce.note_env_file(path, {"OPENAI_API_KEY": "test-only"})
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    ce.take_custody_secrets()
    assert he.custody_file_gap() is None


def test_a_seed_written_after_the_load_is_a_gap_and_removing_it_ends_it(custody, monkeypatch):
    """`polyrob config set` / the console write through upsert_env_var: the gap
    shows in this process at once, and ends when the seed leaves the file."""
    from core.env_file import remove_env_var, upsert_env_var
    path = custody / ".env"
    upsert_env_var(path, "OPENAI_API_KEY", "test-only")
    assert he.custody_file_gap() is None
    upsert_env_var(path, "AGENT_WALLET_MASTER_SEED", SEED)
    assert ce.readable_custody_file() == str(path)
    assert he.custody_file_gap()
    remove_env_var(path, "AGENT_WALLET_MASTER_SEED")
    assert he.custody_file_gap() is None


def test_a_seed_added_to_a_layered_file_by_another_process_is_a_gap(custody):
    path = custody / ".env"
    path.write_text("OPENAI_API_KEY=test-only\n")
    ce.note_env_file(path, {"OPENAI_API_KEY": "test-only"})
    assert he.custody_file_gap() is None
    path.write_text(f"OPENAI_API_KEY=test-only\nexport AGENT_WALLET_MASTER_SEED='{SEED}'\n")
    assert he.custody_file_gap()


def test_the_record_keeps_no_copy_of_the_seed(custody, monkeypatch):
    _dotenv_seed(monkeypatch, custody)
    assert SEED not in repr(ce._env_files)


def test_no_seed_is_no_gap(custody, monkeypatch):
    monkeypatch.delenv("AGENT_WALLET_ENABLED", raising=False)
    assert he.custody_file_gap() is None
    assert he.model_code_host_refusal() is None


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_file_is_no_gap(custody, monkeypatch):
    path = _dotenv_seed(monkeypatch, custody)
    path.chmod(0o000)
    try:
        assert he.custody_file_gap() is None
    finally:
        path.chmod(0o600)


def test_remote_signer_discards_the_seed_but_a_seed_left_in_the_file_is_a_gap(custody, monkeypatch):
    """Under WALLET_SIGNER=remote the process holds no key, but a child can still
    open the file — the gap ends only when the seed leaves it (085 reval)."""
    path = _dotenv_seed(monkeypatch, custody)
    ce.discard_custody_secrets()
    assert ce.custody_secret_sources() == {}
    assert he.custody_file_gap()
    path.write_text("AGENT_WALLET_ENABLED=true\n")
    assert he.custody_file_gap() is None


# -- the gated children -------------------------------------------------------

def test_local_run_code_refuses_on_the_host_and_names_the_fix(custody, monkeypatch):
    from tools.code_exec import sandbox_guard
    monkeypatch.setattr(sandbox_guard, "docker_socket_unreachable_reason", lambda b: None)
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    assert sandbox_guard.require_sandbox_or_none("local_subprocess") is None
    _dotenv_seed(monkeypatch, custody)
    why = sandbox_guard.require_sandbox_or_none("local_subprocess")
    assert why and "polyrob signer install" in why and "on this server" not in why
    # a sandbox backend is the way out
    assert sandbox_guard.require_sandbox_or_none("docker") is None


def test_host_shell_refuses_under_the_file_gap(custody, monkeypatch):
    import core.config_policy as cp
    monkeypatch.setattr(cp, "compute_posture", lambda: 3)
    monkeypatch.setattr(cp, "compute_posture_allows_safe", lambda ctx, n: True)
    monkeypatch.setattr(cp, "local_mode_enabled", lambda: True)
    ctx = types.SimpleNamespace(metadata={"seat": he.HOST_SEAT})
    assert he.host_shell_refusal(ctx) is None
    _dotenv_seed(monkeypatch, custody)
    why = he.host_shell_refusal(ctx)
    assert why and "polyrob signer install" in why


def _owner_turn(monkeypatch):
    from tools.code_exec import sandbox_guard
    monkeypatch.setattr(sandbox_guard, "local_host_exec_refusal", lambda ctx: None)


def _coding_tool(root):
    from tools.coding.tool import CodingTool
    t = object.__new__(CodingTool)
    t.logger = logging.getLogger("coding-test")
    t._root_override = str(root)
    t._backend = None
    return t


@pytest.mark.asyncio
async def test_probe_conftest_that_opens_the_env_file_never_runs(custody, monkeypatch, tmp_path):
    """The C-1 path: a conftest.py that reads ~/.polyrob/.env stops at run_tests."""
    from tools.coding.tool import CodingTool, RunTestsParams
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    work = tmp_path / "work"
    work.mkdir()
    marker = tmp_path / "leaked"
    (work / "conftest.py").write_text(
        f"open({str(marker)!r}, 'w').write(open({str(custody / '.env')!r}).read())\n")
    _dotenv_seed(monkeypatch, custody)

    async def _no_backend(*a, **k):
        pytest.fail("a backend was built under the file gap")
    monkeypatch.setattr(CodingTool, "_get_code_exec_backend", _no_backend)
    res = await _coding_tool(work).run_tests(RunTestsParams())
    assert res.error and "polyrob signer install" in res.error
    assert not marker.exists()


@pytest.mark.asyncio
async def test_natural_run_tests_is_unchanged_without_a_seed(custody, monkeypatch, tmp_path):
    from tools.coding.tool import RunTestsParams
    monkeypatch.delenv("AGENT_WALLET_ENABLED", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    _owner_turn(monkeypatch)
    res = await _coding_tool(tmp_path).run_tests(RunTestsParams(command="echo hi"))
    assert res.error in (None, "") and "hi" in res.extracted_content


@pytest.mark.asyncio
async def test_natural_run_tests_is_unchanged_with_a_process_env_seed(custody, monkeypatch, tmp_path):
    from tools.coding.tool import RunTestsParams
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    _owner_turn(monkeypatch)
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    ce.take_custody_secrets()
    res = await _coding_tool(tmp_path).run_tests(RunTestsParams(command="echo hi"))
    assert res.error in (None, "") and "hi" in res.extracted_content


@pytest.mark.asyncio
async def test_run_code_refuses_under_the_file_gap(custody, monkeypatch):
    from tools.code_exec.tool import CodeExecutionTool, RunCodeParams
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    _dotenv_seed(monkeypatch, custody)
    t = object.__new__(CodeExecutionTool)
    t.logger = logging.getLogger("code-exec-test")
    res = await t.run_code(RunCodeParams(language="python", code="print(1)"))
    assert res.error and "polyrob signer install" in res.error


# -- an old `polyrob update` snapshot copy ------------------------------------

def _snapshot(home, body):
    snap = home / "snapshots" / "20261001T000000Z_1.4.0" / "config"
    snap.mkdir(parents=True)
    copy = snap / "00_.env"
    copy.write_text(body)
    copy.chmod(0o600)
    return copy


def test_a_seed_in_an_old_update_snapshot_is_a_gap_and_names_deleting_it(custody, monkeypatch):
    """A seed moved to the remote signer can still sit in a snapshot copy."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(custody))
    copy = _snapshot(custody, f"AGENT_WALLET_MASTER_SEED={SEED}\n")
    assert ce.readable_custody_file() is None  # no live env file holds it
    assert he._snapshot_copy_with_key() == str(copy)
    why = he.model_code_host_refusal()
    assert why and "snapshot" in why and "delete that old snapshot" in why
    assert SEED not in why and str(copy) not in why
    # the gap only reads: the snapshot keeps the seed for a restore
    assert SEED in copy.read_text()


def test_a_snapshot_without_a_seed_is_no_gap(custody, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(custody))
    _snapshot(custody, "OPENAI_API_KEY=test-only\n")
    assert he.custody_file_gap() is None


def test_the_live_env_file_wins_the_remedy_over_a_snapshot(custody, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(custody / "data"))
    _snapshot(custody / "data", f"AGENT_WALLET_MASTER_SEED={SEED}\n")
    _dotenv_seed(monkeypatch, custody)
    why = he.model_code_host_refusal()
    assert "polyrob signer install" in why and "delete that old snapshot" not in why


def test_update_context_and_the_gap_share_one_snapshot_root(tmp_path):
    from core.runtime_paths import UPDATE_SNAPSHOT_CONFIG_SUBDIR, update_snapshots_root
    from cli.update import snapshot
    assert snapshot._FILE_SUBDIR == UPDATE_SNAPSHOT_CONFIG_SUBDIR
    assert update_snapshots_root(tmp_path) == tmp_path / "snapshots"
