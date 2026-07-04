"""TDD test: GC-race guard (task 5.4).

A binding with a pending outbound row must NOT be purged, even when it is older
than the staleness horizon.
"""
import os
from core.surfaces.session_chat_registry import SessionChatRegistry
from core.surfaces.outbound_queue import OutboundDeliveryQueue
from core.surfaces.gc import purge_stale_safe


def test_binding_with_pending_outbound_is_not_purged(tmp_path):
    reg = SessionChatRegistry(os.path.join(tmp_path, "r.db"))
    reg.bind("sk", "sid", "u1", "wa", "123")
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="sk", surface_id="wa", dest="123", payload="x")
    removed = purge_stale_safe(reg, q, older_than_secs=0)   # everything is "stale"
    assert removed == 0                                       # protected by pending outbound
    assert reg.resolve("sk") is not None


def test_binding_without_pending_is_purged(tmp_path):
    """When there is no pending outbound, stale bindings are deleted normally."""
    reg = SessionChatRegistry(os.path.join(tmp_path, "r.db"))
    reg.bind("sk", "sid", "u1", "wa", "123")
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    # no enqueue → queue is empty; -1 ensures the just-inserted row is "stale"
    removed = purge_stale_safe(reg, q, older_than_secs=-1)
    assert removed == 1
    assert reg.resolve("sk") is None


def test_queue_none_falls_back_to_registry_purge(tmp_path):
    """When queue=None the legacy purge_stale path is taken."""
    reg = SessionChatRegistry(os.path.join(tmp_path, "r.db"))
    reg.bind("sk", "sid", "u1", "wa", "123")
    removed = purge_stale_safe(reg, None, older_than_secs=-1)
    assert removed == 1
    assert reg.resolve("sk") is None


def test_inflight_also_protects_binding(tmp_path):
    """An inflight (in-progress delivery) row must also protect the binding."""
    reg = SessionChatRegistry(os.path.join(tmp_path, "r.db"))
    reg.bind("sk", "sid", "u1", "wa", "123")
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="b", session_key="sk", surface_id="wa", dest="123", payload="y")
    # transition to inflight
    import time as _time
    q.claim_due(now=_time.time() + 1)
    removed = purge_stale_safe(reg, q, older_than_secs=0)
    assert removed == 0
    assert reg.resolve("sk") is not None


def test_unrelated_pending_does_not_protect_other_binding(tmp_path):
    """Pending rows for session_key='other' don't protect 'sk' bindings."""
    reg = SessionChatRegistry(os.path.join(tmp_path, "r.db"))
    reg.bind("sk", "sid", "u1", "wa", "123")
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    # pending row for a DIFFERENT session_key; -1 ensures 'sk' is stale
    q.enqueue(idempotency_key="c", session_key="other", surface_id="wa", dest="456", payload="z")
    removed = purge_stale_safe(reg, q, older_than_secs=-1)
    assert removed == 1          # 'sk' is stale and not protected
    assert reg.resolve("sk") is None


def test_pending_protects_genuinely_stale_binding(tmp_path):
    """Non-vacuous: cutoff in the FUTURE (older_than_secs=-1) makes the row genuinely
    stale, so without the guard it WOULD be deleted — the pending outbound protects it."""
    reg = SessionChatRegistry(os.path.join(tmp_path, "r.db"))
    reg.bind("sk", "sid", "u1", "wa", "123")
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    q.enqueue(idempotency_key="a", session_key="sk", surface_id="wa", dest="123", payload="x")
    removed = purge_stale_safe(reg, q, older_than_secs=-1)  # cutoff = now+1 -> row IS stale
    assert removed == 0                                      # still protected
    assert reg.resolve("sk") is not None


def test_unprotected_stale_binding_is_deleted_at_future_cutoff(tmp_path):
    """Counterpart: a genuinely-stale binding with NO pending outbound IS purged."""
    reg = SessionChatRegistry(os.path.join(tmp_path, "r.db"))
    reg.bind("sk", "sid", "u1", "wa", "123")
    q = OutboundDeliveryQueue(os.path.join(tmp_path, "o.db"))
    removed = purge_stale_safe(reg, q, older_than_secs=-1)   # stale + unprotected
    assert removed == 1
    assert reg.resolve("sk") is None
