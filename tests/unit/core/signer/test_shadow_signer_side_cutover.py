"""WAL-19: the shadow cut-over criterion is read from the signer's own
decision log (signer.sqlite, 0600 polyrob-signer), not only from the
agent-writable signer_shadow.jsonl."""
from pathlib import Path

import pytest

from core.signer.shadow import signer_cutover_ready
from core.signer.store import SignerStore

DAY = 86400.0
NOW = 1_800_000_000.0


def _store(tmp_path, clock):
    return SignerStore(str(tmp_path / "state"), clock=clock)


def _log(store, allowed):
    store.log_decision(op="evm.verdict", digest="0xab", allowed=allowed)


def test_a_clean_signer_week_is_ready(tmp_path):
    t = [NOW - 7 * DAY + 60]
    st = _store(tmp_path, lambda: t[0])
    _log(st, True)
    t[0] = NOW - DAY
    _log(st, True)
    ok, why, counts = signer_cutover_ready(st.state_dir, now=NOW)
    assert ok, why
    assert counts == {"agree": 2, "disagree": 0}


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
    _log(st, True)
    assert signer_cutover_ready(st.state_dir, now=NOW)[0] is True


def test_install_script_checks_the_signer_log_as_the_signer_user():
    path = (Path(__file__).resolve().parents[4] / "deployment" / "hardening"
            / "install-signer.sh")
    if not path.exists():
        pytest.skip("the hardening scripts are not in the public tree")
    text = path.read_text()
    block = text.split("shadow_clean(){")[1].split("\n}\n")[0]
    assert 'runuser -u "$SIGNER_USER"' in block and "signer_cutover_ready" in block
    assert '"$STATE"' in block
    assert "cutover_ready()" in block          # the agent log still counts no_intent
