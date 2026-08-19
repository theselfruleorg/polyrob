"""requirements.txt must not unconditionally ship sentence-transformers (-> torch, ~2GB).

Fresh-install finding (2026-07-19, docs/ops/inbox.md): every requirements.txt install
(contributor local dev + server prod deploys — see AGENTS.md's Environment
Configuration section) pulled in sentence-transformers even where
MEMORY_BACKEND=sqlite (the server default; this box's actual config), which per
core/embedding.py's own docstring costs ~6s cold + torch's full weight, purely for a
provider that degrades gracefully to FTS5 keyword recall when the embedder is
absent (AGENTS.md's Memory System section) — so it was never load-bearing for the
default backend.

Verified the default (sqlite) backend initializes fine with sentence_transformers AND
torch import-blocked (see tests/unit/agents/task/test_import_no_playwright.py for the
same technique applied to playwright). pyproject.toml already gated this correctly
behind the `memory-vector` extra for the public `pip install polyrob[...]` path
(README.md / docs/guide/getting-started.md never reference requirements.txt at all);
this just brings requirements.txt in line with that existing precedent, mirroring the
`browser` extra's exact treatment (see the sibling
tests/unit/tools/web_fetch/test_default_import_no_playwright.py::
test_requirements_has_no_top_level_playwright)."""


def test_requirements_has_no_top_level_sentence_transformers():
    with open("requirements.txt", "r", encoding="utf-8") as fh:
        lines = [l.strip() for l in fh if l.strip() and not l.strip().startswith("#")]
    assert not any(l.lower().startswith("sentence-transformers") for l in lines)


def test_pyproject_memory_vector_extra_still_has_sentence_transformers():
    """The opt-in path must still exist — this is a move, not a removal."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        import tomli as tomllib  # type: ignore
    with open("pyproject.toml", "rb") as fh:
        cfg = tomllib.load(fh)
    extras = cfg["project"]["optional-dependencies"]
    assert "memory-vector" in extras
    assert any("sentence-transformers" in dep for dep in extras["memory-vector"])
