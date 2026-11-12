"""WAL-19: the shadow cut-over criterion is read from the signer's own
decision log (signer.sqlite, 0600 polyrob-signer), not only from the
agent-writable signer_shadow.jsonl."""


from core.signer.shadow import CUTOVER_MIN_AGREED_SENDS, signer_cutover_ready
from core.signer.store import SignerStore

DAY = 86400.0
NOW = 1_800_000_000.0


def _store(tmp_path, clock):
    return SignerStore(str(tmp_path / "state"), clock=clock)


def _log(store, allowed, digest="0xab"):
    store.log_decision(op="evm.verdict", digest=digest, allowed=allowed)


def _agree_many(store, n=CUTOVER_MIN_AGREED_SENDS):
    for i in range(n):
        _log(store, True, digest=f"0x{i:02x}")


def test_a_clean_signer_week_is_ready(tmp_path):
    t = [NOW - 7 * DAY + 60]
    st = _store(tmp_path, lambda: t[0])
    _log(st, True)
    t[0] = NOW - DAY
    _agree_many(st)
    ok, why, counts = signer_cutover_ready(st.state_dir, now=NOW)
    assert ok, why
    assert counts == {"agree": CUTOVER_MIN_AGREED_SENDS + 1, "disagree": 0,
                      "needs_approval": 0}


def test_a_signer_refusal_blocks_the_cutover(tmp_path):
    t = [NOW - 7 * DAY + 60]
    st = _store(tmp_path, lambda: t[0])
    _log(st, True)
    t[0] = NOW - 3600
    _log(st, False)
    ok, why, _ = signer_cutover_ready(st.state_dir, now=NOW)
    assert not ok and "refused" in why


def test_a_short_week_or_no_store_is_not_ready(tmp_path):
    st = _store(tmp_path, lambda: NOW - DAY)
    _log(st, True)
    assert signer_cutover_ready(st.state_dir, now=NOW)[0] is False
    ok, why, _ = signer_cutover_ready(str(tmp_path / "missing"), now=NOW)
    assert not ok and "unreadable" in why


def test_other_ops_do_not_count(tmp_path):
    t = [NOW - 7 * DAY + 60]
    st = _store(tmp_path, lambda: t[0])
    st.log_decision(op="evm.send", digest="0x", allowed=False)
    _log(st, True)
    t[0] = NOW - DAY
    _agree_many(st)
    assert signer_cutover_ready(st.state_dir, now=NOW)[0] is True


def test_few_or_repeated_agreements_are_not_ready(tmp_path):
    """M6: the agent UID can send evm.verdict itself — one chosen verdict, or
    the same transaction sent many times, must not open the cut-over."""
    t = [NOW - 7 * DAY + 60]
    st = _store(tmp_path, lambda: t[0])
    _log(st, True)
    t[0] = NOW - DAY
    for _ in range(CUTOVER_MIN_AGREED_SENDS * 2):
        _log(st, True, digest="0xsame")
    ok, why, _ = signer_cutover_ready(st.state_dir, now=NOW)
    assert not ok and "distinct" in why
    # 0xab, 0xsame and (min - 2) new digests = the minimum of distinct sends
    _agree_many(st, CUTOVER_MIN_AGREED_SENDS - 2)
    assert signer_cutover_ready(st.state_dir, now=NOW)[0] is True


def test_needs_approval_does_not_block(tmp_path):
    t = [NOW - 7 * DAY + 60]
    st = _store(tmp_path, lambda: t[0])
    _log(st, True)
    t[0] = NOW - DAY
    _agree_many(st)
    st.log_decision(op="evm.verdict", digest="0xff", allowed=False, code="approval_required")
    ok, why, counts = signer_cutover_ready(st.state_dir, now=NOW)
    assert ok, why
    assert counts["needs_approval"] == 1
