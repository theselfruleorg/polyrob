"""The always-visible wallet (039 Unit D).

The agent could always LOOK at its balances. What it could not do was KNOW them
without being told to check — so "how much ETH do I have?" was answered from
whatever happened to be in context. On 2026-08-25 that produced a published claim
that the book was flat while three positions were open.
"""
import json
import os
import time
from types import SimpleNamespace

import pytest

from core.wallet import balance_cache as bc


class _Wallet:
    address = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"
    solana_address = "So1anaFake1111111111111111111111111111111111"


def _snap(**kw):
    base = dict(address=_Wallet.address, taken_at=time.time(),
                chains=[bc.ChainBalance("base", "ETH", native=0.0372, usdc=12.5),
                        bc.ChainBalance("robinhood", "ETH", native=0.00088)])
    base.update(kw)
    return bc.BalanceSnapshot(**base)


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------

def test_a_read_never_creates_the_store(tmp_path):
    """The status-SSOT rule: `_init`-ing on read leaves an empty cache in
    whatever data home happened to resolve."""
    assert bc.read(str(tmp_path)) is None
    assert not os.path.exists(bc.cache_path(str(tmp_path)))


def test_no_snapshot_is_a_real_answer_not_an_empty_wallet(tmp_path):
    lines = bc.render_lines(bc.read(str(tmp_path)))
    assert "no balance snapshot yet" in lines[0]
    assert "0" not in lines[0]


def test_a_written_snapshot_round_trips(tmp_path):
    bc.write(_snap(), str(tmp_path))
    got = bc.read(str(tmp_path))
    assert got is not None
    assert got.address == _Wallet.address
    assert [c.chain for c in got.chains] == ["base", "robinhood"]
    assert got.chains[0].native == pytest.approx(0.0372)


def test_a_corrupt_cache_reads_as_no_snapshot(tmp_path):
    path = bc.cache_path(str(tmp_path))
    os.makedirs(tmp_path, exist_ok=True)
    open(path, "w").write("{not json")
    assert bc.read(str(tmp_path)) is None


def test_the_write_is_atomic(tmp_path):
    """A half-written cache would read as a wallet with fewer assets than it has."""
    bc.write(_snap(), str(tmp_path))
    assert not os.path.exists(bc.cache_path(str(tmp_path)) + ".tmp")


# --------------------------------------------------------------------------
# Collection: one dead RPC must not blank the wallet
# --------------------------------------------------------------------------

def test_collect_reads_every_money_chain_plus_solana():
    snap = bc.collect(_Wallet(), evm_reader=lambda a, c: (1.5, 100.0),
                      solana_reader=lambda a: 2.5)
    names = [c.chain for c in snap.chains]
    assert "base" in names and "robinhood" in names and "solana" in names
    assert snap.chains[-1].symbol == "SOL"
    assert snap.chains[-1].native == 2.5


def test_a_failing_chain_records_its_reason_and_does_not_abort_the_rest():
    def _reader(addr, chain):
        if chain == "base":
            raise RuntimeError("rpc down")
        return (1.0, 2.0)
    snap = bc.collect(_Wallet(), evm_reader=_reader, solana_reader=lambda a: 1.0)
    base = next(c for c in snap.chains if c.chain == "base")
    assert base.native is None
    assert "rpc down" in base.error
    assert any(c.chain == "robinhood" and c.native == 1.0 for c in snap.chains)


def test_an_unreadable_balance_is_None_never_zero():
    snap = bc.collect(_Wallet(), evm_reader=lambda a, c: (None, None),
                      solana_reader=lambda a: None)
    assert all(c.native is None for c in snap.chains)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def test_eth_leads_because_it_is_gas_and_buying_power():
    lines = bc.render_lines(_snap())
    assert "base: 0.0372 ETH" in lines[1]
    assert "12.50 USDC" in lines[1]


def test_unknown_is_said_out_loud():
    snap = _snap(chains=[bc.ChainBalance("base", "ETH", native=None)])
    assert "unknown ETH" in bc.render_lines(snap)[1]


def test_an_errored_chain_names_its_reason():
    snap = _snap(chains=[bc.ChainBalance("base", "ETH", error="RuntimeError: rpc down")])
    line = bc.render_lines(snap)[1]
    assert "unknown" in line and "rpc down" in line


def test_a_stale_snapshot_says_so():
    """A balance with no age invites the agent to quote a six-hour-old number as
    current."""
    snap = _snap(taken_at=time.time() - (bc.STALE_AFTER_SEC + 600))
    assert snap.stale is True
    assert "STALE" in bc.render_lines(snap)[0]


def test_a_fresh_snapshot_carries_its_age_without_the_warning():
    lines = bc.render_lines(_snap())
    assert "read " in lines[0] and "STALE" not in lines[0]


# --------------------------------------------------------------------------
# Totals
# --------------------------------------------------------------------------

def test_native_totals_sum_per_symbol():
    totals = bc.total_native_by_symbol(_snap())
    assert totals["ETH"] == pytest.approx(0.0372 + 0.00088)


def test_a_symbol_with_no_readable_chain_totals_UNKNOWN_not_zero():
    """Summing an unreadable chain as zero is how a wallet reports itself emptier
    than it is."""
    snap = _snap(chains=[bc.ChainBalance("base", "ETH", native=None)])
    assert bc.total_native_by_symbol(snap)["ETH"] is None


# --------------------------------------------------------------------------
# The status section and the per-turn note
# --------------------------------------------------------------------------

def test_the_status_section_renders_from_the_cache_with_no_network_read(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    bc.write(_snap(), str(tmp_path))

    def _no_network(*a, **kw):
        raise AssertionError("the wallet section must not touch the network")
    monkeypatch.setattr("core.wallet.onchain.balances", _no_network)
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("owner", data_dir=str(tmp_path),
                                 include_money=False)
    sec = snap.sections["wallet"]
    assert sec.state == "ok"
    assert any("base: 0.0372 ETH" in line for line in sec.lines)


def test_an_unreadable_chain_raises_a_health_item(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    bc.write(_snap(chains=[bc.ChainBalance("base", "ETH", native=None)]), str(tmp_path))
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("owner", data_dir=str(tmp_path), include_money=False)
    keys = [h.key for h in snap.sections["wallet"].health]
    assert "wallet_unreadable" in keys


def test_the_agent_sees_its_balances_every_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    bc.write(_snap(), str(tmp_path))
    from core.status_render import render_agent_health_note
    from core.status_snapshot import build_status_snapshot
    note = render_agent_health_note(
        build_status_snapshot("owner", data_dir=str(tmp_path), include_money=False))
    assert "wallet 0xcAda" in note
    assert "base: 0.0372 ETH" in note


def test_the_note_says_unavailable_rather_than_inventing_a_balance(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    from core.status_render import render_agent_health_note
    from core.status_snapshot import build_status_snapshot
    note = render_agent_health_note(
        build_status_snapshot("owner", data_dir=str(tmp_path), include_money=False))
    assert "wallet: unavailable" in note
    assert "do not state a balance you have not read" in note


def test_the_flag_off_removes_the_note_but_not_the_status_section(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    bc.write(_snap(), str(tmp_path))
    monkeypatch.setenv("WALLET_CONTEXT_VISIBLE", "false")
    from core.status_render import render_agent_health_note
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("owner", data_dir=str(tmp_path), include_money=False)
    assert snap.sections["wallet"].state == "ok"
    assert "wallet 0xcAda" not in render_agent_health_note(snap)


def test_a_wallet_less_deployment_is_not_a_degraded_one(tmp_path, monkeypatch):
    """A status must degrade for real conditions only.

    Reporting `wallet: unavailable` on a deployment that never enabled a wallet
    would mark every status PARTIAL forever over a feature nobody turned on — and
    a status that is always yellow is one nobody reads.
    """
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "false")
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("owner", data_dir=str(tmp_path), include_money=False)
    sec = snap.sections["wallet"]
    assert sec.state == "ok"
    assert "not enabled" in sec.lines[0]
    assert not sec.health
