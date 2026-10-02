"""068 X2: a stuck submission row is released only by BOOKING it."""
import json
import os

import pytest

from core.wallet import submission_journal as sj
from core.wallet.submission_release import (ReleaseRefused, plan_release,
                                            release_submission)

HASH = "0x" + "ab" * 32


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return str(tmp_path)


def _audit(home):
    path = os.path.join(home, "wallet", "audit.jsonl")
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_an_x402_attempt_blocks_until_booked_at_its_recorded_amount(home):
    ref = sj.prepare_attempt("x402", "0xholder", 2.5, idempotency_key="x402:u:2.5")
    with pytest.raises(ValueError):
        sj.prepare(HASH, "base", "0x" + "1" * 40, "7")          # the lock is real
    entry = release_submission(ref, data_dir=home,
                               inspect=lambda r: {"outcome": "operator_evidence_required"})
    assert entry["amount_usd"] == 2.5 and entry["venue"] == "x402"
    assert sj.unresolved(home) == []
    (row,) = _audit(home)
    assert row["action"] == "operator_release" and row["submission_ref"] == ref
    sj.prepare(HASH, "base", "0x" + "1" * 40, "7")              # sends work again


def test_the_charge_is_written_before_the_row_is_released(home, monkeypatch):
    ref = sj.prepare_attempt("x402", "0xholder", 1.0)
    import core.wallet.audit_sink as sink_mod

    class _Broken(list):
        healthy = False
    monkeypatch.setattr(sink_mod, "default_audit_sink", lambda d=None, **kw: _Broken())
    with pytest.raises(ReleaseRefused):
        release_submission(ref, data_dir=home, inspect=lambda r: {"outcome": "x"})
    assert [r["tx_hash"] for r in sj.unresolved(home)] == [ref]


def test_an_unknown_reference_is_refused(home):
    with pytest.raises(ReleaseRefused):
        release_submission("attempt:nope", data_dir=home, inspect=lambda r: {})


# ---- plan_release (pure) ----------------------------------------------------

def _tx_row():
    return {"tx_hash": HASH, "chain": "base", "nonce": "7"}


@pytest.mark.parametrize("outcome", ["unknown", "included_unfinalized", "reorg_or_inconsistent"])
def test_a_transaction_that_may_still_land_is_never_released(outcome):
    with pytest.raises(ReleaseRefused, match="may still land"):
        plan_release(_tx_row(), {"outcome": outcome}, charge_usd=10.0)


def test_a_transaction_row_needs_an_explicit_charge():
    with pytest.raises(ReleaseRefused, match="--charge-usd"):
        plan_release(_tx_row(), {"outcome": "finalized_failed"})
    assert plan_release(_tx_row(), {"outcome": "finalized_success"}, charge_usd=12.5) == 12.5


def test_never_sent_needs_a_reason_and_cannot_follow_a_success():
    with pytest.raises(ReleaseRefused, match="--reason"):
        plan_release(_tx_row(), {"outcome": "finalized_failed"}, never_sent=True, reason="x")
    with pytest.raises(ReleaseRefused, match="was sent"):
        plan_release(_tx_row(), {"outcome": "finalized_success"}, never_sent=True,
                     reason="the facilitator said so")
    assert plan_release({"tx_hash": "attempt:a", "chain": "x402", "nonce": "3"},
                        {"outcome": "operator_evidence_required"}, never_sent=True,
                        reason="402 rejected before settle") == 0.0


def test_an_attempt_never_books_below_what_it_recorded():
    row = {"tx_hash": "attempt:a", "chain": "x402", "nonce": "3"}
    assert plan_release(row, {}, charge_usd=1.0) == 3.0
    assert plan_release(row, {}, charge_usd=5.0) == 5.0


def test_a_signing_reservation_needs_a_charge():
    row = {"tx_hash": "signing:abc", "chain": "base", "nonce": "1"}
    with pytest.raises(ReleaseRefused, match="signing reservation"):
        plan_release(row, {"outcome": "operator_evidence_required"})


# ---- the CLI ----------------------------------------------------------------

def test_cli_names_the_agent_identity_on_a_permission_error(monkeypatch):
    from click.testing import CliRunner
    import cli.commands.wallet as w
    import core.wallet.submission_recovery as rec
    monkeypatch.setattr(w, "_admin_home", lambda write=None: "/nonexistent")

    def denied(**kw):
        raise PermissionError("denied")
    monkeypatch.setattr(rec, "recovery_report", denied)
    res = CliRunner().invoke(w.wallet_cmd, ["submissions"])
    assert res.exit_code != 0 and "sudo -u polyrob-agent polyrob wallet submissions" in res.output


def test_cli_release_books_and_releases(home, monkeypatch):
    from click.testing import CliRunner
    import cli.commands.wallet as w
    import core.wallet.submission_recovery as rec
    ref = sj.prepare_attempt("x402", "0xholder", 0.05, idempotency_key="x402:u:0.05")
    monkeypatch.setattr(w, "_admin_home", lambda write=None: home)
    monkeypatch.setattr(rec, "inspect_submission", lambda row, **kw: rec.RecoveryEvidence(
        row["tx_hash"], row["chain"], "operator_evidence_required", "no id"))
    res = CliRunner().invoke(w.wallet_cmd, ["release-submission", ref, "--yes"])
    assert res.exit_code == 0, res.output
    assert "Booked $0.05" in res.output and sj.unresolved(home) == []


# ---- Codex B5–B7 --------------------------------------------------------------

def test_a_chain_row_books_under_defi_so_the_defi_cap_counts_it(home):
    sj.prepare(HASH, "base", "0x" + "1" * 40, "7")
    entry = release_submission(HASH, data_dir=home, charge_usd=80.0,
                               inspect=lambda r: {"outcome": "finalized_success"})
    assert entry["venue"] == "defi" and entry["amount_usd"] == 80.0


def test_the_recorded_venue_wins_and_a_legacy_row_is_derived():
    from core.wallet.submission_release import booking_venue
    assert booking_venue({"tx_hash": HASH, "chain": "base", "venue": "defi"}) == "defi"
    assert booking_venue({"tx_hash": HASH, "chain": "base"}) == "defi"
    assert booking_venue({"tx_hash": "attempt:1", "chain": "x402"}) == "x402"
    assert booking_venue({"tx_hash": "attempt:1", "chain": "signer:base"}) == "defi"


def test_release_books_the_original_replay_key(home):
    key = "x402:https://api.example/v1:0.1:rid=q1"
    ref = sj.prepare_attempt("x402", "0xholder", 0.1, idempotency_key=key)
    release_submission(ref, data_dir=home, inspect=lambda r: {"outcome": "x"})
    (row,) = _audit(home)
    assert row["idempotency_key"] == key
    # The ledger's rebuilt replay set now refuses the same request.
    from core.wallet.audit_sink import default_audit_sink
    from core.money.ledger import SpendLedger
    gate = SpendLedger(max_per_tx_usd=10, daily_cap_usd=100,
                       audit_sink=default_audit_sink(home))
    assert not gate.check(venue="x402", amount_usd=0.1, idempotency_key=key).allowed


def test_a_crash_after_the_charge_does_not_charge_twice(home, monkeypatch):
    ref = sj.prepare_attempt("x402", "0xholder", 80.0, idempotency_key="x402:u:80")
    real = sj.operator_release

    def crash_after_book(reference, book, *, data_dir=None):
        from core.wallet.submission_store import connection
        with connection(sj.journal_path(data_dir), write=True) as db:
            row = db.execute("SELECT * FROM submissions WHERE tx_hash=?", (reference,)).fetchone()
            book(dict(row))                       # the charge lands …
        raise RuntimeError("power loss")          # … the mark does not
    monkeypatch.setattr(sj, "operator_release", crash_after_book)
    with pytest.raises(RuntimeError):
        release_submission(ref, data_dir=home, inspect=lambda r: {"outcome": "x"})
    assert [r["tx_hash"] for r in sj.unresolved(home)] == [ref]
    monkeypatch.setattr(sj, "operator_release", real)
    release_submission(ref, data_dir=home, inspect=lambda r: {"outcome": "x"})
    assert len(_audit(home)) == 1 and sj.unresolved(home) == []


def test_concurrent_releases_charge_once(home):
    import threading
    ref = sj.prepare_attempt("x402", "0xholder", 80.0, idempotency_key="x402:u:80")
    results, errors = [], []

    def go():
        try:
            results.append(release_submission(ref, data_dir=home,
                                               inspect=lambda r: {"outcome": "x"}))
        except ReleaseRefused as exc:
            errors.append(exc)
    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_audit(home)) == 1
    assert len(results) + len(errors) == 4 and results


# ---- Codex round 2: N2, B7 residue, N3 -----------------------------------------

def test_a_long_replay_key_is_journaled_whole_and_refuses_the_retry(home):
    key = "x402:https://api.example/v1/data?" + "q" * 600 + ":0.1:rid=q1"
    assert len(key) > 512
    ref = sj.prepare_attempt("x402", "0xholder", 0.1, idempotency_key=key)
    release_submission(ref, data_dir=home, inspect=lambda r: {"outcome": "x"})
    (row,) = _audit(home)
    assert row["idempotency_key"] == key
    from core.wallet.audit_sink import default_audit_sink
    from core.money.ledger import SpendLedger
    gate = SpendLedger(max_per_tx_usd=10, daily_cap_usd=100,
                       audit_sink=default_audit_sink(home))
    assert not gate.check(venue="x402", amount_usd=0.1, idempotency_key=key).allowed


def test_an_absurd_replay_key_is_refused_not_cut():
    with pytest.raises(ValueError, match="too long"):
        sj.prepare_attempt("x402", "0xholder", 0.1,
                           idempotency_key="k" * (sj.MAX_IDEMPOTENCY_KEY_CHARS + 1))


def test_a_keyless_x402_row_needs_an_explicit_acknowledgement(home):
    ref = sj.prepare_attempt("x402", "0xholder", 0.5)
    with pytest.raises(ReleaseRefused, match="COULD PAY AGAIN"):
        release_submission(ref, data_dir=home, inspect=lambda r: {"outcome": "x"})
    assert [r["tx_hash"] for r in sj.unresolved(home)] == [ref]
    entry = release_submission(ref, data_dir=home, no_replay_key=True,
                               reason="pre-068 row, facilitator rejected it",
                               inspect=lambda r: {"outcome": "x"})
    assert entry["amount_usd"] == 0.5 and sj.unresolved(home) == []


def test_a_keyless_chain_row_needs_no_acknowledgement(home):
    sj.prepare(HASH, "base", "0x" + "1" * 40, "7")
    entry = release_submission(HASH, data_dir=home, charge_usd=5.0,
                               inspect=lambda r: {"outcome": "finalized_success"})
    assert entry["amount_usd"] == 5.0


def test_a_normal_charge_that_crashed_before_its_mark_is_not_charged_again(home):
    """Codex round 2: the normal path charged $80 (result_ref = the tx hash) and
    crashed before mark_booked; the release charged another $80."""
    from core.wallet.audit_sink import default_audit_sink
    sj.prepare(HASH, "base", "0x" + "1" * 40, "7")
    default_audit_sink(home).append({
        "ts": 1.0, "venue": "defi", "action": "swap", "amount_usd": 80.0,
        "counterparty": None, "idempotency_key": "defi_swap:base:x",
        "result_ref": "0x" + "AB" * 32, "chain": "base", "asset": None})
    entry = release_submission(HASH, data_dir=home, charge_usd=80.0,
                               inspect=lambda r: {"outcome": "finalized_success"})
    assert entry["already_booked"] is True
    assert [e["amount_usd"] for e in _audit(home)] == [80.0]
    assert sj.unresolved(home) == []


def test_a_normal_x402_charge_by_submission_ref_is_recognised(home):
    from core.wallet.audit_sink import default_audit_sink
    ref = sj.prepare_attempt("x402", "0xholder", 0.2, idempotency_key="x402:u:0.2")
    default_audit_sink(home).append({
        "ts": 1.0, "venue": "x402", "action": "x402_fetch", "amount_usd": 0.2,
        "counterparty": None, "idempotency_key": "x402:u:0.2", "result_ref": "0xpaid",
        "chain": None, "asset": None, "submission_ref": ref})
    entry = release_submission(ref, data_dir=home, inspect=lambda r: {"outcome": "x"})
    assert entry["already_booked"] is True and len(_audit(home)) == 1


def test_release_waits_for_a_spend_in_progress_and_charges_once(home):
    """N3: a normal completion holds the audit lock, THEN the journal; the
    release takes the same order, so neither waits on the other forever."""
    import threading
    import time as _t
    from core.wallet.audit_sink import default_audit_sink
    sj.prepare(HASH, "base", "0x" + "1" * 40, "7")
    spender = default_audit_sink(home)
    holding = threading.Event()
    out = {}

    def normal_completion():
        with spender.reserve_blocking():
            holding.set()
            _t.sleep(0.3)                      # the spend's network leg
            spender.append({"ts": 1.0, "venue": "defi", "action": "swap",
                            "amount_usd": 80.0, "counterparty": None,
                            "idempotency_key": "k", "result_ref": HASH,
                            "chain": "base", "asset": None})
            sj.mark_booked(HASH, amount_usd=80.0, venue="defi")
        out["normal"] = True

    def release():
        holding.wait()
        try:
            out["release"] = release_submission(
                HASH, data_dir=home, charge_usd=80.0,
                inspect=lambda r: {"outcome": "finalized_success"})
        except ReleaseRefused as exc:
            out["release_refused"] = str(exc)

    threads = [threading.Thread(target=normal_completion), threading.Thread(target=release)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not any(t.is_alive() for t in threads), "deadlock"
    assert out.get("normal") is True
    assert "already released or booked" in out.get("release_refused", "") \
        or out.get("release", {}).get("already_booked")
    assert [e["amount_usd"] for e in _audit(home)] == [80.0]
