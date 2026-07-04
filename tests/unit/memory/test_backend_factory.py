import logging

from modules.memory.registry import reset_memory_registry


def test_local_vector_warns_loudly_when_embedder_missing(monkeypatch, tmp_path, caplog):
    """Phase 1.2: local_vector requested + sqlite-vec available but NO embedder must
    degrade to FTS-only LOUDLY, not silently report healthy."""
    from modules.memory.backend_factory import maybe_register_memory_backend

    reset_memory_registry()
    monkeypatch.setenv("MEMORY_BACKEND", "local_vector")
    # Pretend the extension is importable so the factory enters the vector branch;
    # the embedder is None, so the provider degrades internally to FTS-only.
    monkeypatch.setattr(
        "modules.memory.local_vector_memory_provider._vec_available",
        lambda: True,
    )
    with caplog.at_level(logging.WARNING):
        provider = maybe_register_memory_backend(data_dir=str(tmp_path), embedding_model=None)

    assert provider is not None
    assert "fts-only" in provider.name          # degraded
    assert any("vector recall is disabled" in r.message.lower() for r in caplog.records)
    reset_memory_registry()


def test_default_selects_local_vector_under_local_mode(monkeypatch, tmp_path):
    """Phase E: with MEMORY_BACKEND unset and POLYROB_LOCAL on, default to local_vector
    (semantic recall over the answer-only facts). It may degrade to fts-only internally
    without apsw, but the SELECTION must be local_vector, not sqlite."""
    from modules.memory.backend_factory import maybe_register_memory_backend

    reset_memory_registry()
    monkeypatch.delenv("MEMORY_BACKEND", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setattr(
        "modules.memory.local_vector_memory_provider._vec_available", lambda: True
    )
    provider = maybe_register_memory_backend(data_dir=str(tmp_path), embedding_model=None)
    assert provider is not None
    assert "local-vector" in provider.name  # selected local_vector (may be fts-only)
    reset_memory_registry()


def test_default_stays_sqlite_on_server(monkeypatch, tmp_path):
    """Phase E: no POLYROB_LOCAL -> server default stays sqlite (byte-identical)."""
    from modules.memory.backend_factory import maybe_register_memory_backend

    reset_memory_registry()
    monkeypatch.delenv("MEMORY_BACKEND", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    provider = maybe_register_memory_backend(data_dir=str(tmp_path))
    assert provider is not None
    assert provider.name == "sqlite-fts"
    reset_memory_registry()


def test_explicit_sqlite_wins_over_local_default(monkeypatch, tmp_path):
    """Phase E: an explicit MEMORY_BACKEND=sqlite overrides the local-mode default."""
    from modules.memory.backend_factory import maybe_register_memory_backend

    reset_memory_registry()
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("MEMORY_BACKEND", "sqlite")
    provider = maybe_register_memory_backend(data_dir=str(tmp_path))
    assert provider is not None
    assert provider.name == "sqlite-fts"
    reset_memory_registry()


def test_local_vector_backend_falls_back_to_sqlite_when_vec_unavailable(monkeypatch, tmp_path):
    from modules.memory.backend_factory import maybe_register_memory_backend

    reset_memory_registry()
    monkeypatch.setenv("MEMORY_BACKEND", "local_vector")
    monkeypatch.setattr(
        "modules.memory.local_vector_memory_provider._vec_available",
        lambda: False,
    )

    provider = maybe_register_memory_backend(data_dir=str(tmp_path))

    assert provider is not None
    assert provider.name == "sqlite-fts"
    reset_memory_registry()
