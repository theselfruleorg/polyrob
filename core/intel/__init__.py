"""The ONE token read layer (proposal 071 §3.2).

Pure reads: no money authority, no LLM, no network code of its own. The
sources that answer a price live in the tools tier (``tools/defi/providers``)
and register themselves here through :func:`core.intel.price.register_source`,
because ``core`` must never import ``tools`` (layering ratchet). What lives
here is the part every caller must agree on: the typed model, the TTL cache,
and the arithmetic that turns several claims into one quote.
"""
