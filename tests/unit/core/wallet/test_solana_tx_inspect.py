"""Solana pre-broadcast inspection: Token-2022 ATAs, the observation set, and
the top-level program allowlist.

Two verified holes in the SVM money path, both of which made a REFUSAL silently
turn into a PASS:

* `_solana_ata` derived the associated account under the classic SPL Token
  program only. A Token-2022 mint (Jupiter routes them) therefore resolved to an
  address that does not exist, `parse_deltas` observed no state for the declared
  mint, and `solana_swap`'s `if in_delta is not None and in_delta < 0` outflow
  assertion was SKIPPED instead of tripped.
* The simulation named only `[owner, ATA(in), ATA(out)]`, so an extra
  instruction touching a DIFFERENT wallet-owned token account was invisible.
"""
import pytest

pytest.importorskip("solders", reason="needs the `solana` extra")

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from core.wallet import solana_tx_inspect as sti
from core.wallet.solana_simulation import SPL_TOKEN_PROGRAMS

CLASSIC = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
OWNER = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _tx(program_ids, named=()):
    """A signed v0 transaction whose top-level instructions call *program_ids*
    (each also naming the accounts in *named*)."""
    kp = Keypair()
    payer = kp.pubkey()
    ixs = [Instruction(Pubkey.from_string(p), b"\x00",
                       [AccountMeta(payer, True, True)]
                       + [AccountMeta(Pubkey.from_string(a), False, True) for a in named])
           for p in program_ids]
    msg = MessageV0.try_compile(payer, ixs, [], Hash.default())
    return bytes(VersionedTransaction(msg, [kp]))


# -- 1a: the ATA must follow the mint's OWN token program -------------------

def test_the_two_token_programs_derive_different_accounts():
    """The associated address is a PDA over (owner, TOKEN PROGRAM, mint), so a
    Token-2022 mint simply is not at the classic program's address."""
    classic = sti.derive_ata(OWNER, MINT, CLASSIC)
    t22 = sti.derive_ata(OWNER, MINT, TOKEN2022)
    assert classic and t22
    assert classic != t22


def test_airdropped_accounts_do_not_disable_simulation(monkeypatch):
    from types import SimpleNamespace
    addresses = [OWNER] + [str(Pubkey.new_unique()) for _ in range(201)]
    monkeypatch.setattr(sti, "inspect_transaction", lambda *a, **k: SimpleNamespace(
        ok=True, instructions=[], num_signatures=1))
    monkeypatch.setattr(sti, "owned_token_accounts", lambda *a, **k: addresses[1:])
    monkeypatch.setattr(sti, "sim_max_addresses", lambda: 5)
    reads, simulations = [], []

    def rpc(method, params):
        if method == "getMultipleAccounts":
            reads.append(params[0])
            assert len(params[0]) <= 100
            return {"value": [None] * len(params[0])}
        assert method == "simulateTransaction"
        chunk = params[1]["accounts"]["addresses"]
        simulations.extend(chunk)
        return {"value": {"err": None, "accounts": [None] * len(chunk)}}

    observed = {}
    def parsed(sim, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(ok=True)
    monkeypatch.setattr(sti, "parse_deltas", parsed)
    assert sti.simulate(b"transaction", owner=OWNER, rpc=rpc).ok
    assert [len(chunk) for chunk in reads] == [100, 100, 2]
    assert simulations == addresses == observed["ours"]


def test_a_token_2022_mint_resolves_to_its_own_associated_account():
    """The mint's owner program is READ, not assumed."""
    def _rpc(method, params):
        assert method == "getAccountInfo"
        return {"value": {"owner": TOKEN2022}}

    assert sti.atas_for_mint(OWNER, MINT, _rpc) == (
        sti.derive_ata(OWNER, MINT, TOKEN2022),)


def test_an_unreadable_mint_program_names_both_candidates():
    """Fail-safe, not fail-silent: naming an address that does not exist costs
    nothing, while naming the wrong one disables the outflow assertion."""
    def _rpc(method, params):
        raise RuntimeError("rpc down")

    both = sti.atas_for_mint(OWNER, MINT, _rpc)
    assert set(both) == {sti.derive_ata(OWNER, MINT, p) for p in SPL_TOKEN_PROGRAMS}


def test_the_spl_token_programs_constant_is_actually_used():
    """It was dead: defined in solana_simulation.py with zero references, while
    the ATA derivation hard-coded the classic program."""
    assert set(sti.candidate_atas(OWNER, MINT)) == {
        sti.derive_ata(OWNER, MINT, p) for p in SPL_TOKEN_PROGRAMS}


# -- 2: the observation set ------------------------------------------------

def test_the_simulation_names_every_token_account_the_wallet_owns():
    """An extra instruction touching a DIFFERENT wallet-owned token account was
    invisible, because the simulation only reported the accounts the CALLER
    declared. parse_deltas discards anything not owned by us, so 'observe
    everything that matters' == 'name every token account we own'."""
    other = "6ASf5EcmiEXZQFsx1nqvBiYRUwjRW4wCVLxTdxHtnQhU"

    def _rpc(method, params):
        if method == "getAccountInfo":
            return {"value": {"owner": CLASSIC}}
        if method == "getTokenAccountsByOwner":
            return {"value": [{"pubkey": other}]} if params[1]["programId"] == CLASSIC else {"value": []}
        raise AssertionError(method)

    addresses = sti.simulation_addresses(OWNER, mints=[MINT], rpc=_rpc)
    assert OWNER in addresses
    assert sti.derive_ata(OWNER, MINT, CLASSIC) in addresses
    assert other in addresses, "a wallet-owned token account must be observed"


def test_the_declared_accounts_come_first_so_a_truncation_cannot_drop_them():
    """The list is capped by getMultipleAccounts' 100-key limit; the ordering is
    what makes the cap safe."""
    filler = [str(Pubkey.new_unique()) for _ in range(sti.MAX_SIM_ADDRESSES + 20)]

    def _rpc(method, params):
        if method == "getAccountInfo":
            return {"value": {"owner": CLASSIC}}
        return {"value": []}

    addresses = sti.simulation_addresses(
        OWNER, mints=[MINT], rpc=_rpc, account_keys=filler)
    assert len(addresses) == sti.MAX_SIM_ADDRESSES
    assert addresses[0] == OWNER
    assert sti.derive_ata(OWNER, MINT, CLASSIC) in addresses


def test_a_cut_inside_our_own_accounts_is_reported_loudly(caplog):
    """The cap is safe only while the tail is third-party. When the wallet owns
    more token accounts than the limit, some of OUR OWN go unobserved — that
    must be stated, not implied away."""
    import logging

    mine = [str(Pubkey.new_unique()) for _ in range(sti.MAX_SIM_ADDRESSES + 5)]

    def _rpc(method, params):
        if method == "getAccountInfo":
            return {"value": {"owner": CLASSIC}}
        return {"value": [{"pubkey": p} for p in mine]
                if params[1]["programId"] == CLASSIC else []}

    with caplog.at_level(logging.WARNING, logger=sti.__name__):
        sti.simulation_addresses(OWNER, mints=[MINT], rpc=_rpc)
    assert "OUR OWN accounts are NOT observed" in caplog.text


# -- 2: the top-level program allowlist ------------------------------------

def test_a_jupiter_shaped_transaction_is_accepted():
    raw = _tx([sti.COMPUTE_BUDGET_PROGRAM_ID, CLASSIC,
               "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"])
    assert sti.inspect_transaction(raw).ok is True


def test_an_unknown_top_level_program_refuses():
    """The threat is an aggregator RESPONSE that carries an extra instruction —
    a SetAuthority, a CloseAccount, a delegate grant. That instruction has to
    name its program in the static keys, so it is always visible here."""
    raw = _tx(["JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
               str(Pubkey.new_unique())])
    inspection = sti.inspect_transaction(raw)
    assert inspection.ok is False
    assert "unrecognized program" in inspection.reason
    assert inspection.unknown_programs


def test_undecodable_bytes_refuse_rather_than_pass():
    inspection = sti.inspect_transaction(b"\x01")
    assert inspection.ok is False
    assert "could not be decoded" in inspection.reason


def test_the_operator_can_widen_the_allowlist(monkeypatch):
    """A hard refusal on a live trading path needs an owner-controlled escape
    hatch for the day the aggregator ships a new program version."""
    new_program = str(Pubkey.new_unique())
    raw = _tx([new_program])
    assert sti.inspect_transaction(raw).ok is False
    monkeypatch.setenv("DEFI_SOLANA_PROGRAM_ALLOWLIST_EXTRA", new_program)
    assert sti.inspect_transaction(raw).ok is True


def test_simulate_refuses_before_touching_the_rpc_when_the_tx_is_unvetted():
    """A transaction that fails the allowlist must never be simulated, let
    alone signed."""
    calls = []

    def _rpc(method, params):
        calls.append(method)
        return {}

    raw = _tx([str(Pubkey.new_unique())])
    deltas = sti.simulate(raw, owner=OWNER, mints=[MINT], rpc=_rpc)
    assert deltas.ok is False
    assert calls == []


def test_simulate_parses_deltas_from_a_clean_transaction():
    ata = sti.derive_ata(OWNER, MINT, CLASSIC)
    raw = _tx([sti.COMPUTE_BUDGET_PROGRAM_ID,
               "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"], named=[ata])

    def _acct(amount):
        return {"data": {"parsed": {"info": {
            "owner": OWNER, "mint": MINT,
            "tokenAmount": {"amount": str(amount)}}}}}

    def _rpc(method, params):
        if method == "getAccountInfo":
            return {"value": {"owner": CLASSIC}}
        if method == "getTokenAccountsByOwner":
            return {"value": [{"pubkey": ata}]}
        if method == "getMultipleAccounts":
            return {"value": [None, _acct(5_000_000)]}
        if method == "simulateTransaction":
            assert ata in params[1]["accounts"]["addresses"]
            return {"value": {"err": None, "unitsConsumed": 1234,
                              "accounts": [None, _acct(4_000_000)]}}
        raise AssertionError(method)

    deltas = sti.simulate(raw, owner=OWNER, mints=[MINT], rpc=_rpc)
    assert deltas.ok is True
    assert deltas.token_deltas == {MINT: -1_000_000}


@pytest.mark.parametrize("program", [CLASSIC, TOKEN2022])
@pytest.mark.parametrize("tag", [3, 7, 8, 12, 14, 15])
def test_top_level_token_outflow_from_existing_account_refused(program, tag):
    ix = sti.DecodedIx(program=program, data=bytes([tag]) + bytes(9), accounts=(OWNER, MINT, OWNER))
    assert sti.instruction_refusal([ix], owner=OWNER)



def test_the_outflow_names_match_the_spl_token_tags():
    # 7 is MintTo and 8 is Burn (a Burn mislabeled as tag 7 left the real Burn unchecked).
    assert sti._TOK_OUTFLOW_TAGS[8] == "Burn" and sti._TOK_OUTFLOW_TAGS[7] == "MintTo"
    assert sti._TOK_OUTFLOW_TAGS[15] == "BurnChecked" and sti._TOK_OUTFLOW_TAGS[12] == "TransferChecked"


@pytest.mark.parametrize("data,name", [(bytes([26, 1]) + bytes(17), "TransferCheckedWithFee"),
                                       (bytes([34, 0]), "CpiGuardExtension"),
                                       (bytes([27, 0]), "ConfidentialTransferExtension")])
def test_token_2022_extension_transfers_and_policies_on_existing_accounts_refuse(data, name):
    ix = sti.DecodedIx(program=TOKEN2022, data=data, accounts=(OWNER, MINT, OWNER))
    why = sti.instruction_refusal([ix], owner=OWNER)
    assert why and name in why


def test_a_token_2022_extension_on_the_classic_program_is_not_misread():
    ix = sti.DecodedIx(program=CLASSIC, data=bytes([26, 1]), accounts=(OWNER, MINT, OWNER))
    assert sti.instruction_refusal([ix], owner=OWNER) is None



def test_spam_token_accounts_the_transaction_does_not_name_cost_no_simulations(monkeypatch):
    """Anyone can create token accounts for the wallet. An account a transaction does not
    name cannot change, so it is not observed — the simulation count stays bounded by the
    transaction, not by the spam (each extra RPC call is one more rate-limit refusal)."""
    ata = sti.derive_ata(OWNER, MINT, CLASSIC)
    spam = [str(Pubkey.new_unique()) for _ in range(60)]
    raw = _tx(["JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"], named=[ata])
    monkeypatch.setattr(sti, "sim_max_addresses", lambda: 5)
    sims = []

    def _rpc(method, params):
        if method == "getAccountInfo":
            return {"value": {"owner": CLASSIC}}
        if method == "getTokenAccountsByOwner":
            return {"value": [{"pubkey": a} for a in [ata] + spam]}
        if method == "getMultipleAccounts":
            return {"value": [None] * len(params[0])}
        if method == "simulateTransaction":
            sims.append(params[1]["accounts"]["addresses"])
            return {"value": {"err": None, "accounts": [None] * len(sims[-1])}}
        raise AssertionError(method)

    sti.simulate(raw, owner=OWNER, mints=[MINT], rpc=_rpc)
    observed = [a for chunk in sims for a in chunk]
    assert len(sims) == 1 and ata in observed and not set(spam) & set(observed)
