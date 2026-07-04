"""Tests that the unused H-MEM Domain/layer-3 concept has been removed.

The Domain class was serialized/deserialized but never constructed at runtime
(no add_domain/link_phase_to_domain callers existed anywhere). These tests
enforce that the dead code is gone and that loading old JSON files that happen
to contain a "domains" key does not crash.
"""
import inspect
import json
import tempfile
from pathlib import Path

from modules.memory.task import hierarchical_memory as hm


def test_domain_layer_gone():
    assert not hasattr(hm, "Domain"), "Domain/layer-3 was never constructed — remove it"
    assert not hasattr(hm.HierarchicalMemory, "add_domain")
    assert not hasattr(hm.HierarchicalMemory, "link_phase_to_domain")


def test_load_ignores_legacy_domains_key():
    """A persisted dict from the old format may carry a 'domains' key; load must ignore it."""
    src = inspect.getsource(hm.HierarchicalMemory)
    # constructor no longer accepts domain kwargs
    assert "current_domain=" not in src and "domain_index=" not in src


def test_load_with_legacy_domains_key_does_not_crash():
    """Behavioral: HierarchicalMemory.load() must not raise when JSON has a 'domains' key."""
    legacy_data = {
        "session_id": "test-session",
        "task": "test task",
        "current_phase": "discovery",
        "current_domain": "some_domain",  # old field
        "progress": "0/?",
        "phases_completed": [],
        "domains": [  # old field — must be silently ignored
            {
                "domain_name": "some_domain",
                "summary": "A domain",
                "phase_indices": [],
                "domain_embedding": [],
                "created_at": "2025-01-01T00:00:00",
                "updated_at": "2025-01-01T00:00:00",
            }
        ],
        "domain_index": {"some_domain": 0},  # old field — must be silently ignored
        "phase_memories": [],
        "phase_index": {},
        "recent_steps": [],
        "created_at": "2025-01-01T00:00:00",
        "updated_at": "2025-01-01T00:00:00",
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "hmem.json"
        path.write_text(json.dumps(legacy_data))
        mem = hm.HierarchicalMemory.load(path)
    assert mem.session_id == "test-session"
    assert mem.task == "test task"
