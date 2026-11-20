"""Audit 2026-11-21 M-5 — a rollback (``polyrob update rollback``, the auto-rollback) restores
``wallet/`` but never the mandate store or the income-rule store: an ended mandate stays
ended, a used count stays used, an income already counted (``seen``) is never counted again.
The live copies cross the swap as they are; every other wallet file is restored."""
from core.update_snapshot.context import ROLLBACK_KEPT_FILES
from core.update_snapshot.snapshot import create_snapshot, restore_snapshot
from core.wallet import income_rule, standing_grant


def _snap(tmp_path):
    home = tmp_path / "home"
    wallet = home / "wallet"
    wallet.mkdir(parents=True)
    (wallet / "meta.json").write_text("META-BEFORE")
    (wallet / standing_grant.STORE_NAME).write_text("MANDATES-BEFORE")
    (wallet / income_rule.STORE_NAME).write_text("INCOME-BEFORE")
    snap = create_snapshot(snapshots_root=tmp_path / "snapshots", data_home=home,
                           from_version="1", db_paths=[], config_paths=[],
                           dir_paths=[wallet])
    return home, wallet, snap


def test_the_kept_files_are_the_two_standing_money_stores():
    assert set(ROLLBACK_KEPT_FILES["wallet"]) >= {standing_grant.STORE_NAME,
                                                  income_rule.STORE_NAME}


def test_a_rollback_keeps_the_live_mandate_and_income_stores(tmp_path):
    home, wallet, snap = _snap(tmp_path)
    (wallet / "meta.json").write_text("META-AFTER")
    (wallet / standing_grant.STORE_NAME).write_text("MANDATES-AFTER")
    (wallet / income_rule.STORE_NAME).write_text("INCOME-AFTER")
    restore_snapshot(snap.path, data_home=home)
    assert (wallet / "meta.json").read_text() == "META-BEFORE"
    assert (wallet / standing_grant.STORE_NAME).read_text() == "MANDATES-AFTER"
    assert (wallet / income_rule.STORE_NAME).read_text() == "INCOME-AFTER"


def test_a_rollback_never_brings_back_a_store_that_is_gone(tmp_path):
    home, wallet, snap = _snap(tmp_path)
    (wallet / standing_grant.STORE_NAME).unlink()
    restore_snapshot(snap.path, data_home=home)
    assert not (wallet / standing_grant.STORE_NAME).exists()
    assert (wallet / income_rule.STORE_NAME).read_text() == "INCOME-BEFORE"
