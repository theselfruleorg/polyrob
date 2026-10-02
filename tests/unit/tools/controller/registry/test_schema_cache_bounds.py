"""F24: the tool-schema memo is bounded, hands out a copy, and reads under the lock.

The memo keyed `(provider, action-set, exclusions)` was unbounded and returned the
LIVE list object, and `get_all_actions_for_provider` iterated `registry.actions`
outside `_registry_lock`. Three properties are pinned here:

1. a mutation of the returned list never reaches the next call;
2. more than `SCHEMA_CACHE_MAX_ENTRIES` distinct action sets keep at most that many;
3. two threads calling concurrently while a third registers do not raise.
"""
import threading

import pytest

from tools.controller.registry.service import Registry, SCHEMA_CACHE_MAX_ENTRIES


def _registry_with(names):
    reg = Registry()
    for name in names:
        def _fn(value: str = "x", _n=name):
            return _n

        _fn.__name__ = name
        reg.action(f"{name} action")(_fn)
    return reg


def test_returned_list_is_a_copy_not_the_live_cache():
    reg = _registry_with(["alpha", "beta"])

    first = reg.get_all_actions_for_provider("openai")
    n = len(first)
    assert n >= 2

    # A consumer that appends (or clears, or re-orders) must not poison the memo.
    first.append({"type": "function", "function": {"name": "injected"}})
    first[0] = {"type": "function", "function": {"name": "clobbered"}}

    second = reg.get_all_actions_for_provider("openai")
    assert len(second) == n
    assert second is not first
    assert all(
        s.get("function", {}).get("name") not in ("injected", "clobbered")
        for s in second
        if isinstance(s, dict)
    )


def test_cache_is_bounded_to_eight_entries():
    reg = _registry_with(["base"])

    # Every new action changes the key (the action-set frozenset), so N
    # registrations produce N distinct cache keys.
    for i in range(SCHEMA_CACHE_MAX_ENTRIES + 6):
        def _fn(value: str = "x", _i=i):
            return _i

        _fn.__name__ = f"grow_{i}"
        reg.action(f"grow {i}")(_fn)
        reg.get_all_actions_for_provider("openai")
        assert len(reg._provider_schema_cache) <= SCHEMA_CACHE_MAX_ENTRIES
        assert len(reg._provider_schema_tokens) <= SCHEMA_CACHE_MAX_ENTRIES

    assert len(reg._provider_schema_cache) == SCHEMA_CACHE_MAX_ENTRIES


def test_concurrent_calls_do_not_raise():
    reg = _registry_with([f"a{i}" for i in range(6)])
    errors = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                out = reg.get_all_actions_for_provider("openai")
                out.append("scribble")  # a mutating consumer, on purpose
        except Exception as exc:  # pragma: no cover - the assertion is below
            errors.append(exc)

    def writer():
        try:
            for i in range(40):
                def _fn(value: str = "x", _i=i):
                    return _i

                _fn.__name__ = f"w{i}"
                reg.action(f"w {i}")(_fn)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(2)]
    threads.append(threading.Thread(target=writer))
    for t in threads:
        t.start()
    threads[-1].join(timeout=30)
    stop.set()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors


def test_token_estimate_stays_coherent_with_the_bounded_cache():
    reg = _registry_with(["alpha"])
    reg.get_all_actions_for_provider("openai")
    assert reg.get_schema_token_estimate("openai") > 0

    # Still self-serving on a cold cache.
    reg._provider_schema_cache.clear()
    reg._provider_schema_tokens.clear()
    assert reg.get_schema_token_estimate("openai") > 0
