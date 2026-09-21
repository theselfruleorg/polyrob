"""The CLI half of the 2026-09-21 interface deep audit (section C + E).

Each test pins ONE finding by the behaviour it changed, not by the code shape,
so a future refactor that keeps the promise keeps the test.
"""
from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner


def _run(cmd, args, **kw):
    return CliRunner().invoke(cmd, args, **kw)


# --- C12: dead targets came from the wrong db and the wrong column -----------

def test_surface_status_reads_the_dead_target_store(tmp_path, monkeypatch):
    from core.surfaces.dead_targets import DeadTargetStore
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    store = DeadTargetStore(str(tmp_path / "dead_targets.db"))
    store.mark("telegram", "12345", "blocked")

    from cli.commands.surface import _surface_status_rows
    rows = {r["surface_id"]: r for r in _surface_status_rows()}
    assert rows["telegram"]["dead_targets"] == 1
    assert rows["email"]["dead_targets"] == 0


def test_surface_status_never_creates_the_dead_target_store(tmp_path, monkeypatch):
    """A read that opens the store would mint a db file to answer 'none'."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.commands.surface import _surface_status_rows
    _surface_status_rows()
    assert not (tmp_path / "dead_targets.db").exists()


# --- C56: the surface vocabulary is derived from SurfaceConfig ---------------

def test_known_surfaces_is_derived_from_surface_config():
    from cli.commands.surface import _known_surfaces
    from core.surfaces.config import SurfaceConfig
    expected = {a[: -len("_surface_enabled")] for a in dir(SurfaceConfig)
                if a.endswith("_surface_enabled")}
    assert set(_known_surfaces()) == expected


# --- C13: EIP-55 is not noise, and a Solana address is not "not an address" --

def test_require_address_refuses_a_bad_checksum():
    import click

    from cli.commands.wallet import _require_address
    good = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"   # USDC, checksummed
    assert _require_address("base", good) == good
    with pytest.raises(click.ClickException):
        _require_address("base", good[:-1] + "9")          # one char flipped


def test_require_address_accepts_a_solana_key():
    from cli.commands.wallet import _require_address
    mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert _require_address("solana", mint) == mint


# --- C14: the board view is list_recent + status_counts, never board.list ----

def test_goals_list_never_calls_board_list(monkeypatch, tmp_path):
    from cli.commands import goals as G

    class _Board:
        def __init__(self):
            self.list_calls = 0

        def list(self, *a, **k):           # the dispatcher's order — not a view
            self.list_calls += 1
            return []

        def list_recent(self, *, user_id, statuses=None, limit=30):
            return []

        def status_counts(self, *, user_id):
            return {"done": 3}

    board = _Board()
    monkeypatch.setattr(G, "_get_board", lambda data_root=None: board)
    res = _run(G.goals, ["list", "--user", "u1"])
    assert res.exit_code == 0, res.output
    assert board.list_calls == 0
    assert "3 done" in res.output


def test_goals_list_names_the_counts_source_when_it_cannot_be_read(monkeypatch):
    from cli.commands import goals as G

    class _Board:
        def list_recent(self, *, user_id, statuses=None, limit=30):
            return []

        def status_counts(self, *, user_id):
            raise OSError("disk I/O error")

    monkeypatch.setattr(G, "_get_board", lambda data_root=None: _Board())
    res = _run(G.goals, ["list", "--user", "u1"])
    assert res.exit_code == 0, res.output
    assert "counts unavailable" in res.output
    assert "UNKNOWN" in res.output


# --- C21: the CLI's remedies name `polyrob owner …`, not slash verbs ---------

def test_cli_remedies_name_polyrob_verbs():
    from core.surfaces.inbox_render import CLI_REMEDIES
    for rows in CLI_REMEDIES.values():
        for _word, template in rows:
            assert template.startswith("polyrob "), template


# --- C22: allow/deny/allowlist key on the canonical address ------------------

def test_owner_allow_canonicalizes_the_target(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.commands.owner import _allowlist, owner
    res = _run(owner, ["allow", "telegram", "@Handle", "--user", "u1"])
    assert res.exit_code == 0, res.output
    rows = _allowlist(str(tmp_path)).list("u1")
    assert [r["target"] for r in rows] == ["handle"]


# --- C23: the approval tenant is the owner resolver, not the literal "local" -

def test_approvals_list_names_the_resolved_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    from cli.commands.approvals import approvals
    res = _run(approvals, ["list", "--home", str(tmp_path), "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["user_id"] == "u_owner"


# --- C31: a NULL money amount is UNKNOWN, never $0.00 or a crash -------------

def test_sub_list_survives_a_null_amount(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.commands import owner as O

    async def _fake(coro_factory):
        return True, [{"status": "active", "id": "s1", "amount_usd": None,
                       "period_days": 30, "cron_job_id": "c1",
                       "correspondent_surface": "email",
                       "correspondent_address": "a@b.c",
                       "paid_through": "2026-10-01"}]

    monkeypatch.setattr(O, "_with_bot_db", _fake)
    res = _run(O.owner, ["sub", "list"])
    assert res.exit_code == 0, res.output
    assert "$?" in res.output
    assert "$0.00" not in res.output


# --- C34: ambiguity is not absence ------------------------------------------

def test_owner_approve_distinguishes_ambiguity_from_absence(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.commands import owner as O

    class _Reg:
        def approve(self, **kw):
            return False

        def list(self, user_id=None):
            return [{"surface": "email", "address": "a@b.c", "state": "pending",
                     "thread_id": "t1", "user_id": user_id or "local"},
                    {"surface": "email", "address": "a@b.c", "state": "pending",
                     "thread_id": "t2", "user_id": user_id or "local"}]

    monkeypatch.setattr(O, "_registry", lambda data_dir, **k: _Reg())
    res = _run(O.owner, ["approve", "email", "a@b.c"])
    assert res.exit_code != 0
    assert "AMBIGUOUS" in res.output
    assert "t1" in res.output and "t2" in res.output


# --- C35: a flag that cannot be honoured is refused, never dropped -----------

def test_deploy_token_refuses_uri_on_an_evm_chain():
    from cli.commands.wallet import wallet_cmd
    res = _run(wallet_cmd, ["deploy-token", "ROB", "1000", "Rob", "Coin",
                            "--chain", "base", "--uri", "https://x/y.json"])
    assert res.exit_code != 0
    assert "SOLANA only" in res.output


def test_deploy_token_refuses_solana_decimals_above_nine():
    from cli.commands.wallet import wallet_cmd
    res = _run(wallet_cmd, ["deploy-token", "ROB", "1000", "Rob", "Coin",
                            "--chain", "solana", "--decimals", "18"])
    assert res.exit_code != 0
    assert "clamped silently" in res.output


# --- C36: a curve quote has ONE side ----------------------------------------

def test_curve_refuses_both_sides():
    from cli.commands.wallet import wallet_cmd
    res = _run(wallet_cmd, ["curve", "0xabc", "--buy", "1", "--sell", "2"])
    assert res.exit_code != 0
    assert "--buy OR --sell" in res.output


# --- C37: `doctor --json` carries the typed snapshot and honours --full ------

def test_doctor_json_emits_typed_sections_and_honours_full(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.commands.doctor import doctor, reset_doctor_snapshot_cache
    reset_doctor_snapshot_cache()
    res = _run(doctor, ["--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["full"] is False
    assert len(payload["report"]) == 1          # the pointer, not the transcript
    snap = payload["snapshot"]
    assert snap["available"] is True
    assert "providers" in snap["sections"]
    assert "state" in snap["sections"]["providers"]

    reset_doctor_snapshot_cache()
    res_full = _run(doctor, ["--json", "--full"])
    full = json.loads(res_full.stdout)
    assert full["full"] is True
    assert len(full["report"]) > 1


# --- C51: `todos --json` is honoured on every exit ---------------------------

@pytest.mark.parametrize("args", [["stats"], ["clear"]])
def test_todos_json_is_honoured_with_no_file(tmp_path, args):
    from cli.commands.todos import todos
    missing = str(tmp_path / "nope.md")
    res = _run(todos, args + ["--file", missing, "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["error"].endswith("nope.md")


def test_todos_empty_state_names_the_file_and_the_repl_twin(tmp_path):
    from cli.commands.todos import todos
    res = _run(todos, ["list", "--file", str(tmp_path / "nope.md")])
    assert res.exit_code == 0, res.output
    assert "nope.md" in res.output
    assert "/todos" in res.output


# --- C58: an unreadable security list is UNKNOWN, never empty ----------------

def test_subagents_info_says_unknown_when_the_policy_cannot_be_read(monkeypatch):
    import tools.controller.delegation as D
    from cli.commands.subagents import subagents

    def _boom():
        raise RuntimeError("import failed")

    monkeypatch.setattr(D, "get_blocked_child_tools", _boom)
    res = _run(subagents, ["info"])
    assert res.exit_code == 0, res.output
    assert "UNKNOWN" in res.output
    assert "nothing is blocked" in res.output


# --- E6/E7/E8/E9: the new owner seats exist and are wired --------------------

@pytest.mark.parametrize("path", [
    ["wallet", "claim"], ["wallet", "nft"], ["wallet", "dapp"],
    ["wallet", "lp"], ["wallet", "asset"], ["wallet", "overview"],
])
def test_new_wallet_seats_have_help(path):
    from cli.commands.wallet import wallet_cmd
    res = _run(wallet_cmd, path[1:] + ["--help"])
    assert res.exit_code == 0, res.output
    assert res.output.strip()


@pytest.mark.parametrize("verb", ["register", "set-uri"])
def test_identity_onchain_seats_have_help(verb):
    from cli.commands.identity import identity
    res = _run(identity, [verb, "--help"])
    assert res.exit_code == 0, res.output


def test_wallet_dapp_list_is_honest_without_a_store(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.commands.wallet import wallet_cmd
    res = _run(wallet_cmd, ["dapp", "list"])
    assert res.exit_code == 0, res.output
    assert "never connected" in res.output
    assert not (tmp_path / "dapp_sessions.db").exists()


# --- E11 / E12: the paid + groups seats -------------------------------------

def test_owner_paid_has_the_write_verbs():
    from cli.commands.owner import owner
    res = _run(owner, ["paid", "--help"])
    for verb in ("enable", "disable", "price", "asset", "offers"):
        assert verb in res.output


def test_owner_groups_admins_is_honest_without_a_store(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.commands.owner import owner
    res = _run(owner, ["groups", "admins", "telegram", "_100123"])
    assert res.exit_code == 0, res.output
    assert "no surface store yet" in res.output
    assert not (tmp_path / "surfaces.db").exists()


# --- E10: the contact transcript has an owner seat --------------------------

def test_owner_correspondents_history_reports_an_absent_store(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.commands.owner import owner
    res = _run(owner, ["correspondents", "--history", "email", "a@b.c"])
    assert res.exit_code == 0, res.output
    assert "no conversation store yet" in res.output
    assert not (tmp_path / "conversations.db").exists()


# --- C40: every mutating owner verb carries the root escape hatch ------------

_MUTATING = [
    ("owner", ["halt"]), ("owner", ["resume"]), ("owner", ["promote"]),
    ("owner", ["reject"]), ("owner", ["approve"]), ("owner", ["allow"]),
    ("owner", ["deny"]), ("owner", ["invite"]), ("owner", ["settle"]),
    ("owner", ["fulfill"]), ("owner", ["pair", "approve"]),
    ("owner", ["groups", "allow"]), ("owner", ["paid", "enable"]),
    ("apps", ["approve"]), ("apps", ["reject"]), ("apps", ["kill"]),
    ("cron", ["schedule"]), ("cron", ["cancel"]), ("cron", ["edit"]),
    ("cron", ["prune"]), ("approvals", ["add"]), ("approvals", ["remove"]),
    ("keys", ["create"]), ("keys", ["revoke"]),
    ("wallet", ["init"]), ("wallet", ["set-cap"]), ("wallet", ["asset", "add"]),
]


@pytest.mark.parametrize("group_name,path", _MUTATING)
def test_mutating_verbs_offer_as_root(group_name, path):
    import importlib
    mod = importlib.import_module(f"cli.commands.{group_name}")
    group = getattr(mod, {"owner": "owner", "apps": "apps", "cron": "cron",
                          "approvals": "approvals", "keys": "keys",
                          "wallet": "wallet_cmd"}[group_name])
    res = _run(group, path + ["--help"])
    assert res.exit_code == 0, res.output
    assert "--as-root" in res.output, f"{group_name} {' '.join(path)}"
