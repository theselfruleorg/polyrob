from modules.memory.task.lexical_retriever import LexicalRetriever


def test_lexical_finds_token_overlap():
    r = LexicalRetriever(min_similarity=0.05)
    findings = {"research": ["A costs $10 per month", "B is slower than C"],
                "analysis": ["the API key rotates daily"]}
    hits = r.search_similar("how much does A cost per month", findings, top_k=2)
    assert hits, "lexical retriever returned nothing on clear token overlap"
    assert hits[0][0] == "A costs $10 per month", f"top hit wrong: {hits}"
    assert hits[0][1] == "research", "phase name must be element 1"
    assert isinstance(hits[0][2], float)


def test_lexical_empty_inputs():
    r = LexicalRetriever()
    assert r.search_similar("", {"p": ["x"]}) == []
    assert r.search_similar("q", {}) == []


def test_lexical_parity_stubs():
    r = LexicalRetriever()
    assert r.embed_text("hello") is None
    r.clear_cache()  # must not raise
    stats = r.get_cache_stats()
    assert "cache_size" in stats
    assert stats["min_similarity"] == 0.1
