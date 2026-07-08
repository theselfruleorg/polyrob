"""Regression: KB vector recall must over-fetch before the per-collection filter.

kb_vec is partitioned by user_id only, so the sqlite-vec `k` limit applies across
ALL of a tenant's collections BEFORE the m.collection filter — a dominant other
collection could truncate the queried collection's recall to zero. The provider
now over-fetches k and slices to `limit` after filtering.
"""
import sys
import types

import modules.memory.local_vector_memory_provider as lv


class _Cur:
    def __init__(self, sink, rows):
        self.sink = sink
        self.rows = rows

    def execute(self, sql, params):
        self.sink["params"] = params
        return self

    def fetchall(self):
        return self.rows


class _Con:
    def __init__(self, sink, rows):
        self._c = _Cur(sink, rows)

    def cursor(self):
        return self._c

    def close(self):
        pass


def test_overfetch_k_and_slice_to_limit(monkeypatch):
    fake = types.ModuleType("sqlite_vec")
    fake.serialize_float32 = lambda emb: b"vec"
    monkeypatch.setitem(sys.modules, "sqlite_vec", fake)

    sink = {}
    rows = [(f"content{i}", f"path{i}", i, 0.1) for i in range(20)]  # all in-collection, close
    monkeypatch.setattr(lv, "vec_connect", lambda p: _Con(sink, rows))
    monkeypatch.setattr(lv, "_max_distance", lambda: 1.0)

    prov = lv.LocalVectorMemoryProvider.__new__(lv.LocalVectorMemoryProvider)
    prov.db_path = ":memory:"
    prov._embed_sync = lambda q: [0.0] * 4
    # P2-6: vec schema is lazy now; mark it ready so this unit test exercises the
    # overfetch SQL directly without a real embedder probe.
    prov._vec_ok = True
    prov._vec_schema_ready = True

    out = prov._kb_vector_contents("some query", "user", "collB", limit=5)

    # k passed to sqlite-vec is the OVER-FETCH, not the requested limit.
    assert sink["params"][3] == min(5 * lv._KB_VEC_OVERFETCH, lv._KB_VEC_OVERFETCH_CAP)
    assert sink["params"][3] > 5
    # Final result is sliced back down to the requested limit.
    assert len(out) == 5
    assert out[0] == ("content0", "path0", 0)
