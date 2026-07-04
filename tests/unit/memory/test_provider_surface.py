from modules.memory.provider import MemoryProvider

def test_dead_hooks_removed():
    for dead in ("on_turn_start", "on_memory_write", "on_delegation", "get_config_schema", "system_prompt_block"):
        assert not hasattr(MemoryProvider, dead), f"{dead} is dead (no call sites) — remove it"

def test_wired_hooks_kept():
    for live in ("on_pre_compress", "on_session_end", "prefetch", "sync_turn", "search"):
        assert hasattr(MemoryProvider, live), f"{live} is wired — must stay"
