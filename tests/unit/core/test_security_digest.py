"""045: the rollup counts, never fetches; and an absent store raises so the
caller can render `unavailable` rather than a confident zero."""
import time

import pytest


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    db = tmp_path / "telemetry_events.db"
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(db))
    monkeypatch.setenv("SECURITY_EVENT_LOG_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    import core.event_log as el
    el._INSTANCES.clear()
    log = el.get_event_log()
    from core import event_kinds as ek
    for _ in range(5):
        log.record(ek.INBOUND_ROUTED, user_id="u1", source="perimeter",
                   attrs={"sender": "42", "decision": "task_agent"})
    for _ in range(3):
        log.record(ek.ACCESS_DENIED, user_id="u1", source="perimeter",
                   attrs={"sender": "999", "reason": "raw_allowlist"})
    log.record(ek.TOOL_DENIED, user_id="u1", source="gate",
               attrs={"reason": "correspondent_taint", "tool": "defi_trade"})
    log.record(ek.INJECTION_FLAGGED, user_id="u1", source="threat_scan",
               attrs={"origin": "url"})
    yield str(tmp_path)
    el._INSTANCES.clear()


def test_counts_each_lane(seeded):
    from core.security_digest import build_security_rollup
    r = build_security_rollup("u1", data_dir=seeded,
                              since_ts=time.time() - 3600, window_sec=3600)
    assert r.inbound == 5
    assert r.denied == 3
    assert r.refused == 1
    assert r.flagged == 1


def test_top_denial_reasons_are_ranked(seeded):
    from core.security_digest import build_security_rollup
    r = build_security_rollup("u1", data_dir=seeded,
                              since_ts=time.time() - 3600, window_sec=3600)
    assert r.top_denial_reasons[0] == ("raw_allowlist", 3)
    assert r.top_senders[0][1] >= 3


def test_absent_store_raises_so_the_caller_renders_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "nope.db"))
    import core.event_log as el
    el._INSTANCES.clear()
    from core.security_digest import build_security_rollup
    with pytest.raises(FileNotFoundError):
        build_security_rollup("u1", data_dir=str(tmp_path),
                              since_ts=time.time() - 3600, window_sec=3600)


def test_tenant_scoped(seeded):
    """The tenant column is the DEPLOYMENT, not the counterparty.

    C1: this test used to read as "a sender's rows are invisible under another
    id", which quietly pinned the perimeter blindness as correct — the lane
    stamped the SENDER's surface-hashed id while every owner seat reads with
    the OWNER's. The scoping being tested is between two DEPLOYMENTS; the
    counterparty lives in `attrs.sender` and is never a tenant.
    """
    from core.security_digest import build_security_rollup
    # `42`/`999` are SENDERS in the fixture, never tenants — reading the store
    # as if a sender were a tenant must find nothing.
    for not_a_tenant in ("u_other_deployment", "42", "999"):
        r = build_security_rollup(not_a_tenant, data_dir=seeded,
                                  since_ts=time.time() - 3600, window_sec=3600)
        assert r.inbound == 0
        assert r.denied == 0
    # …and the deployment tenant sees the counterparties in `attrs.sender`.
    owner = build_security_rollup("u1", data_dir=seeded,
                                  since_ts=time.time() - 3600, window_sec=3600)
    assert ("42", 5) in owner.top_senders
    assert ("999", 3) in owner.top_senders


def _fresh_log(tmp_path, monkeypatch):
    """A clean event-log store, independent of the `seeded` fixture's counts
    — the ranking tests below need exact, hand-picked totals."""
    db = tmp_path / "telemetry_events.db"
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(db))
    monkeypatch.setenv("SECURITY_EVENT_LOG_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    import core.event_log as el
    el._INSTANCES.clear()
    return el.get_event_log()


def test_top_senders_cross_kind_ranking_beats_truncate_then_merge(tmp_path, monkeypatch):
    """A sender who is moderate in BOTH lanes must not be invisible just
    because each lane's OWN top-5 cut excludes it. Ranking must run over the
    TRUE combined total (one SQL query over the kind union), never a merge of
    two already-truncated per-kind top-5 lists.

    denied:  A10 B9 C8 D7 E6 | G5 (falls outside denied's own top 5)
    routed:  F20 H15 I13 J11 K9 | G7 (falls outside routed's own top 5)
    G's true total is 5+7=12 — it outranks J(11) and A(10) — but a
    truncate-then-merge implementation drops G before it ever reaches the
    merge, and A(10) wrongly fills rank 5 instead.
    """
    log = _fresh_log(tmp_path, monkeypatch)
    import core.event_log as el
    from core import event_kinds as ek

    denied = {"A": 10, "B": 9, "C": 8, "D": 7, "E": 6, "G": 5}
    routed = {"F": 20, "H": 15, "I": 13, "J": 11, "K": 9, "G": 7}
    for sender, n in denied.items():
        for _ in range(n):
            log.record(ek.ACCESS_DENIED, user_id="u1", source="perimeter",
                       attrs={"sender": sender, "reason": "x"})
    for sender, n in routed.items():
        for _ in range(n):
            log.record(ek.INBOUND_ROUTED, user_id="u1", source="perimeter",
                       attrs={"sender": sender, "decision": "task_agent"})

    from core.security_digest import build_security_rollup
    r = build_security_rollup("u1", data_dir=str(tmp_path),
                              since_ts=time.time() - 3600, window_sec=3600)
    assert r.top_senders[:5] == [
        ("F", 20), ("H", 15), ("I", 13), ("G", 12), ("J", 11),
    ]
    el._INSTANCES.clear()


def test_top_senders_tie_break_is_deterministic(tmp_path, monkeypatch):
    """Equal counts must sort deterministically (ascending by value), not by
    insertion/hash order — Tasks 8/9 must not flap on equal counts."""
    log = _fresh_log(tmp_path, monkeypatch)
    import core.event_log as el
    from core import event_kinds as ek

    for _ in range(2):
        log.record(ek.ACCESS_DENIED, user_id="u1", source="perimeter",
                   attrs={"sender": "zed", "reason": "tie"})
    for _ in range(2):
        log.record(ek.ACCESS_DENIED, user_id="u1", source="perimeter",
                   attrs={"sender": "amy", "reason": "tie"})

    from core.security_digest import build_security_rollup
    r = build_security_rollup("u1", data_dir=str(tmp_path),
                              since_ts=time.time() - 3600, window_sec=3600)
    assert r.top_senders[:2] == [("amy", 2), ("zed", 2)]
    el._INSTANCES.clear()


def test_top_denial_reasons_tie_break_is_deterministic(tmp_path, monkeypatch):
    """Same guarantee as above, for the single-kind `_top_attr` ranking."""
    log = _fresh_log(tmp_path, monkeypatch)
    import core.event_log as el
    from core import event_kinds as ek

    for _ in range(2):
        log.record(ek.ACCESS_DENIED, user_id="u1", source="perimeter",
                   attrs={"sender": "s1", "reason": "zulu"})
    for _ in range(2):
        log.record(ek.ACCESS_DENIED, user_id="u1", source="perimeter",
                   attrs={"sender": "s2", "reason": "alpha"})

    from core.security_digest import build_security_rollup
    r = build_security_rollup("u1", data_dir=str(tmp_path),
                              since_ts=time.time() - 3600, window_sec=3600)
    assert r.top_denial_reasons[:2] == [("alpha", 2), ("zulu", 2)]
    el._INSTANCES.clear()


# --- I3: `refused` counts the TYPED producer, not every hook veto ------------

def test_refused_counts_only_the_typed_gate_producer(tmp_path, monkeypatch):
    """`tools/controller/execution.py` has emitted `tool_denied` for EVERY
    pre-tool-call hook veto since 2026-07-04. The correspondent gate and the
    approval hook are both pre-tool-call hooks, so one correspondent-taint
    refusal wrote 2 rows and one forged-turn owner-queue refusal wrote 3:
    `refused` was inflated by an inconsistent factor, and the reason ranking
    grouped 200-character denial prose beside the typed slugs.

    The rollup counts `source='gate'` only. The untyped emit is untouched —
    other consumers read it, and narrowing the READER is the smaller change.
    """
    log = _fresh_log(tmp_path, monkeypatch)
    import core.event_log as el
    from core import event_kinds as ek

    # ONE real refusal, as it actually lands today: the typed gate row…
    log.record(ek.TOOL_DENIED, user_id="u1", source="gate",
               attrs={"reason": "correspondent_taint", "tool": "defi_trade"})
    # …plus the controller's untyped echo of the SAME refusal, twice over.
    for _ in range(2):
        log.record(ek.TOOL_DENIED, user_id="u1", source="controller",
                   attrs={"action": "defi_trade",
                          "reason": "Action 'defi_trade' blocked: this session is "
                                    "tainted by correspondent data and may not "
                                    "move money; ask the owner to…"})

    from core.security_digest import build_security_rollup
    r = build_security_rollup("u1", data_dir=str(tmp_path),
                              since_ts=time.time() - 3600, window_sec=3600)
    assert r.refused == 1, "one refusal must count once, not three times"
    assert r.top_refusal_reasons == [("correspondent_taint", 1)]
    el._INSTANCES.clear()
