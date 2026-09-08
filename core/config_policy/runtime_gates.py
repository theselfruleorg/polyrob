"""Cross-cluster runtime gates that need the whole policy surface (``embedder_needed``).

Split out of ``core/config_policy/policy.py`` (S2, 2026-08-29); ``policy.py`` re-exports
every name so existing importers are unaffected. Import order (a DAG): _env -> local_profile
-> autonomy_mode -> autonomy_posture -> compute_posture -> payment_policy -> capability_toggles
-> autonomy_config -> runtime_gates.
"""
from core.config_policy.local_profile import local_mode_enabled
from core.config_policy.capability_toggles import resolved_memory_backend
from core.config_policy.autonomy_config import AutonomyConfig


def embedder_needed() -> bool:
    """Whether this deployment actually needs the sentence-transformers embedder (torch).

    SSOT for both the CLI (maybe_register_cli_embedder) and the server (initialize_modules):
    only build the heavy embedder when KB is enabled, MEMORY_BACKEND=local_vector (hybrid
    vector recall), or local mode. The default MEMORY_BACKEND=sqlite uses FTS5 keyword recall
    and needs no embeddings (P1-EMB, 2026-06-26 runtime-architecture finalization).
    """
    return (
        AutonomyConfig.kb_enabled()
        or resolved_memory_backend() == "local_vector"
        or local_mode_enabled()
    )
