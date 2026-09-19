from core.wallet.infra_error import is_infra_broadcast_failure as infra


def test_the_prod_line_is_infra():
    assert infra("broadcast failed: [Errno 13] Permission denied: "
                 "'/var/lib/polyrob/wallet/submissions.sqlite' — nothing was sent")


def test_guard_and_chain_refusals_are_not_infra():
    assert not infra("tx_guard refused: per-tx cap $300 exceeded")
    assert not infra("broadcast failed: execution reverted: INSUFFICIENT_OUTPUT_AMOUNT — nothing was sent")
    assert not infra("no route for 0.4 ETH")
    assert not infra("")
    assert not infra(None)


def test_other_os_shapes_are_infra_only_with_the_broadcast_prefix():
    assert infra("broadcast failed: OSError: [Errno 30] Read-only file system — nothing was sent")
    assert infra("broadcast failed: sqlite3.OperationalError: unable to open database file — nothing was sent")
    assert not infra("[Errno 30] Read-only file system: 'data/auto/rob/sessions/x'")  # H-MEM save, not a money verb
