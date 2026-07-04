"""Task context manager - orchestrator for hierarchical memory system.

The TaskContextManager is the main interface for the hierarchical memory system.
It coordinates all components and provides a unified API for the task agent.

Responsibilities:
    - Session lifecycle (create, load, save)
    - Coordinate PhaseManager, ContextRetriever, CompactionManager
    - Provide unified API for agent integration
    - Persist hierarchical memory to disk

Usage:
    task_context = TaskContextManager(config)
    await task_context.initialize()

    # Create session
    memory = task_context.create_session(session_id, task)

    # Add step
    task_context.add_step_memory(session_id, step, brain_state, finding)

    # Get context
    context = task_context.get_context_injection(session_id)

    # Save
    task_context.save_session(session_id)
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from core.base_component import BaseComponent
from core.config import BotConfig
from core.exceptions import ComponentError

from .hierarchical_memory import HierarchicalMemory
from .phase_manager import PhaseManager
from .context_retriever import ContextRetriever
from .compaction_manager import CompactionManager
from .semantic_retriever import SemanticRetriever

logger = logging.getLogger(__name__)


class SessionData:
    """Container for session-level components.

    Holds all the managers for a single task session.

    Attributes:
        memory: HierarchicalMemory instance
        phase_manager: PhaseManager instance
        context_retriever: ContextRetriever instance
        compaction_manager: CompactionManager instance
        last_brain_state: Last brain state from agent (for semantic search)
    """

    def __init__(
        self,
        memory: HierarchicalMemory,
        phase_manager: PhaseManager,
        context_retriever: ContextRetriever,
        compaction_manager: CompactionManager
    ):
        self.memory = memory
        self.phase_manager = phase_manager
        self.context_retriever = context_retriever
        self.compaction_manager = compaction_manager
        self.last_brain_state: Optional[Dict[str, Any]] = None  # Phase 3: For semantic search
        # FIX (Dec 12, 2025): Step-level cache to prevent 5x redundant hierarchical searches per step
        self._cached_context: Optional[str] = None
        self._cached_step: int = -1  # Track which step the cache is for
        # FIX (Jan 2026): Content-aware cache invalidation
        # Version increments on any memory change (finding added, phase transition, etc.)
        self._cache_version: int = 0
        self._cached_at_version: int = -1

    def invalidate_cache(self) -> None:
        """Invalidate context cache - call after any memory change.

        This method should be called whenever:
        - A finding is added
        - A phase transition occurs
        - Memory is pruned
        - Any other operation that changes the context
        """
        self._cache_version += 1
        self._cached_context = None
        self._cached_step = -1
        self._cached_at_version = -1

    def is_cache_valid(self, current_step: int) -> bool:
        """Check if cached context is still valid.

        Args:
            current_step: Current step number

        Returns:
            True if cache is valid, False otherwise
        """
        return (
            self._cached_context is not None and
            self._cached_step == current_step and
            self._cached_at_version == self._cache_version
        )


class TaskContextManager(BaseComponent):
    """Orchestrates hierarchical memory system for task agents.

    The TaskContextManager is a BaseComponent that manages multiple active
    task sessions, each with its own hierarchical memory.

    Configuration:
        HIERARCHICAL_MEMORY_ENABLED: Enable/disable system (default: true)
        COMPACTION_ENABLED: Enable message compaction (default: true)
        CONTEXT_SOFT_THRESHOLD: Soft limit ratio (default: 0.70)
        CONTEXT_HARD_THRESHOLD: Hard limit ratio (default: 0.85)

    Attributes:
        config: Bot configuration
        _sessions: Active sessions (keyed by session_id)
        _base_path: Base path for session storage
        enabled: Whether system is enabled
    """

    def __init__(
        self,
        name: str,
        config: BotConfig,
        base_path: Optional[Path] = None
    ):
        """Initialize task context manager.

        Args:
            name: Component name
            config: Bot configuration
            base_path: Base path for storage (if None, uses config)
        """
        super().__init__(name=name, config=config)

        self._sessions: Dict[str, SessionData] = {}

        # Storage path
        if base_path:
            self._base_path = Path(base_path)
        else:
            self._base_path = Path(config.get("DATA_PATH", "data")) / "auto"

        # Configuration
        self.enabled = config.get("HIERARCHICAL_MEMORY_ENABLED", True)
        self.compaction_enabled = config.get("COMPACTION_ENABLED", True)
        self.soft_threshold = config.get("CONTEXT_SOFT_THRESHOLD", 0.70)
        self.hard_threshold = config.get("CONTEXT_HARD_THRESHOLD", 0.85)

        # Phase 3: Semantic retrieval configuration
        # Uses FREE local embeddings (SentenceTransformer), no API costs
        self.semantic_enabled = config.get("SEMANTIC_RETRIEVAL_ENABLED", True)
        self.semantic_top_k = config.get("SEMANTIC_TOP_K", 3)
        self.semantic_min_similarity = config.get("SEMANTIC_MIN_SIMILARITY", 0.65)
        
        # FIX #6 (Nov 26, 2025): Context-aware compaction scaling
        # Model context window for adaptive thresholds (None = use defaults)
        self.context_window = config.get("MODEL_CONTEXT_WINDOW", None)

        # H-MEM Paper Section 3.3: Memory Update Mechanisms
        # OPTIMIZATION (Nov 14, 2025): More aggressive consolidation
        self.reflection_enabled = config.get("REFLECTION_ENABLED", True)
        self.reflection_threshold = config.get("REFLECTION_THRESHOLD", 25)  # Was 100, now 25 (4x more frequent!)
        # §7.7: the per-step trigger (25 findings in ONE session) is structurally
        # unreachable for short cron/goal sessions, so reflection never fires for the
        # autonomous workload. A session-CLOSE trigger consolidates a short session's
        # handful of findings at cleanup, at a lower threshold. Default OFF (an extra
        # aux-model call per closed session — opt in after verifying cost).
        self.reflection_on_session_close = config.get("REFLECTION_ON_SESSION_CLOSE", False)
        self.reflection_session_close_threshold = config.get(
            "REFLECTION_SESSION_CLOSE_THRESHOLD", 5)
        self.forgetting_enabled = config.get("FORGETTING_ENABLED", True)
        # Storage cap for importance-based forgetting. Default lowered 500 -> 60 so
        # pruning actually engages (retriever displays ~15; 60 keeps headroom without
        # letting phases grow unbounded). Override via MAX_FINDINGS_PER_PHASE.
        # NOTE (L4, known/bounded): without query embeddings the importance signal is
        # near-uniform, so over-cap eviction degenerates to FIFO and a >60-finding
        # research phase can drop early findings that are still needed. This is LOGGED
        # (see _check_and_prune_memories MED-6 warning) and bounded, and the cap is
        # intentionally kept <=100 (pinned by test_forgetting_engages.py) so pruning
        # engages in normal sessions. Raise via env MAX_FINDINGS_PER_PHASE for
        # research-heavy workloads rather than changing the default.
        self.max_findings_per_phase = config.get("MAX_FINDINGS_PER_PHASE", 60)
        self.importance_threshold = config.get("IMPORTANCE_THRESHOLD", 0.3)  # Min to keep

        # LLM-synthesized phase reflection (H-MEM §3.3). UP-09: default ON via the
        # SAME helper construction.py uses to provision reflection_llm, so the guard and
        # the provisioning can never disagree. (Historical bug: this read
        # config.get("REFLECTION_LLM_ENABLED", False) where BotConfig.get is
        # getattr(self,key,default) with no such attr => always False => reflection never
        # fired even with the env set.) reflection_llm stays None until construction.py
        # provisions it; when None, _llm_consolidate returns None => concat fallback.
        from agents.task.constants import reflection_llm_enabled_default
        self.reflection_llm_enabled = reflection_llm_enabled_default()
        self.reflection_llm = None
        # A3: metering context (usage_tracker/user_id/session_id/agent_id) for billing
        # the reflection aux LLM call; populated by construction.py, empty => no metering.
        self.reflection_meter_ctx: Optional[dict] = None

        # Track memories since last reflection (per session)
        self._memories_since_reflection: Dict[str, int] = {}

        logger.info(
            f"TaskContextManager initialized: enabled={self.enabled}, "
            f"compaction={self.compaction_enabled}, "
            f"thresholds={self.soft_threshold}/{self.hard_threshold}, "
            f"semantic={self.semantic_enabled}, "
            f"reflection={self.reflection_enabled}, "
            f"forgetting={self.forgetting_enabled}"
        )

    async def _initialize(self) -> None:
        """Initialize component."""
        try:
            self.logger.info("Task Context Manager initialization started")

            # Ensure base path exists
            self._base_path.mkdir(parents=True, exist_ok=True)

            self.logger.info(
                f"Task Context Manager initialized | "
                f"Compaction: {'enabled' if self.compaction_enabled else 'disabled'} | "
                f"Thresholds: {self.soft_threshold:.0%}/{self.hard_threshold:.0%}"
            )

        except Exception as e:
            self.logger.error(f"Task Context Manager initialization failed: {e}")
            raise ComponentError(f"Failed to initialize task context manager: {e}")

    async def _cleanup(self) -> None:
        """Clean up component resources."""
        try:
            self.logger.info("Task Context Manager cleanup started")

            # Save all active sessions
            for session_id in list(self._sessions.keys()):
                try:
                    self.save_session(session_id)
                except Exception as e:
                    self.logger.error(f"Failed to save session {session_id} during cleanup: {e}")

            self._sessions.clear()
            self.logger.info("Task Context Manager cleanup completed")

        except Exception as e:
            self.logger.error(f"Task Context Manager cleanup failed: {e}")

    def _get_semantic_retriever(self):
        """Get a retriever for H-MEM cross-phase search.

        Mode is controlled by the ``HMEM_SEMANTIC`` env var (default ``auto``):
        - ``auto``       — use the local embedding model if one is registered;
                           fall back to :class:`LexicalRetriever` otherwise.
        - ``embeddings`` — require the embedding model; return ``None`` if absent.
        - ``lexical``    — always use :class:`LexicalRetriever` (no embedder needed).
        - ``off``        — disable cross-phase search entirely.

        Returns:
            A retriever instance (SemanticRetriever or LexicalRetriever), or None.
        """
        import os
        if not self.semantic_enabled:
            logger.info("⚠️ Semantic retrieval disabled (user config)")
            return None

        mode = os.getenv("HMEM_SEMANTIC", "auto").strip().lower()

        if mode == "off":
            logger.debug("H-MEM cross-phase search disabled via HMEM_SEMANTIC=off")
            return None

        # Resolve the embedder defensively — failure must NOT disable the lexical
        # fallback (lexical needs no container, so it must never depend on one
        # resolving). A raising container would otherwise silently defeat the whole
        # no-embedder fallback this method exists to provide.
        embedding_model = None
        try:
            from core.container import DependencyContainer

            container = DependencyContainer.get_instance()
            if container and container.has_service('embedding_model'):
                embedding_model = container.get_service('embedding_model')
        except Exception as e:
            logger.debug(f"embedding model resolution failed (will fall back): {e}")

        use_embeddings = (mode == "embeddings") or (mode == "auto" and embedding_model is not None)

        if use_embeddings:
            if not embedding_model:
                # mode == "embeddings" but no model registered
                logger.debug("HMEM_SEMANTIC=embeddings but no embedding model available")
                return None

            class _EmbeddingHolder:
                """Adapts the bare embedding model to SemanticRetriever's `.embedding_model` contract."""
                def __init__(self, model):
                    self.embedding_model = model

            try:
                semantic_retriever = SemanticRetriever(
                    rag_manager=_EmbeddingHolder(embedding_model),
                    min_similarity=self.semantic_min_similarity
                )
                logger.info("✅ H-MEM cross-phase search: embedding mode (local model)")
                return semantic_retriever
            except Exception as e:
                logger.warning(f"SemanticRetriever init failed, falling back to lexical: {e}")
                # Fall through to lexical under auto; under explicit `embeddings` the
                # caller asked for embeddings only — but a failed init is better served
                # by the deterministic lexical path than by silently dropping section-3.

        # mode == "lexical", OR auto with no embedder, OR embeddings-init failure:
        from .lexical_retriever import LexicalRetriever
        logger.info("✅ H-MEM cross-phase search: lexical mode (no embedder; TF cosine)")
        return LexicalRetriever()

    def create_session(
        self,
        session_id: str,
        task: str,
        user_id: Optional[str] = None
    ) -> HierarchicalMemory:
        """Create a new task session with hierarchical memory.

        Args:
            session_id: Unique session identifier
            task: Task description
            user_id: Optional user ID for path construction

        Returns:
            Created HierarchicalMemory instance

        Raises:
            ValueError: If session already exists
        """
        if session_id in self._sessions:
            raise ValueError(f"Session {session_id} already exists")

        # Create hierarchical memory
        memory = HierarchicalMemory(
            session_id=session_id,
            task=task
        )

        # Get semantic retriever if available (Phase 3)
        semantic_retriever = self._get_semantic_retriever()

        # Create managers with PHASE 1 FIX optimized values
        # FIX (Nov 26, 2025): Use expanded context window values, not hardcoded low values
        phase_manager = PhaseManager(memory, semantic_retriever=semantic_retriever)
        context_retriever = ContextRetriever(
            memory=memory,
            max_findings_per_phase=15,  # PHASE 1 FIX: Was 5, now 15 (3x more findings)
            max_recent_steps=50,         # PHASE 1 FIX: Was 10, now 50 (5x more steps)
            semantic_retriever=semantic_retriever,
            semantic_top_k=self.semantic_top_k,
            enable_cross_phase_search=self.semantic_enabled
        )
        compaction_manager = CompactionManager(
            soft_threshold=self.soft_threshold,
            hard_threshold=self.hard_threshold,
            context_window=self.context_window  # FIX #6: Pass context window for adaptive scaling
        )

        # Store session data
        session_data = SessionData(
            memory=memory,
            phase_manager=phase_manager,
            context_retriever=context_retriever,
            compaction_manager=compaction_manager
        )
        self._sessions[session_id] = session_data

        logger.info(f"Created task session: {session_id}")
        return memory

    def load_session(
        self,
        session_id: str,
        user_id: Optional[str] = None
    ) -> Optional[HierarchicalMemory]:
        """Load an existing task session.

        Args:
            session_id: Session identifier to load
            user_id: Optional user ID for path construction

        Returns:
            Loaded HierarchicalMemory, or None if not found
        """
        # Check if already loaded
        if session_id in self._sessions:
            return self._sessions[session_id].memory

        # Try to load from disk
        memory_path = self._get_memory_path(session_id, user_id)

        if not memory_path.exists():
            logger.debug(f"No hierarchical memory found for session {session_id}")
            return None

        try:
            memory = HierarchicalMemory.load(memory_path)

            # Get semantic retriever if available (Phase 3)
            semantic_retriever = self._get_semantic_retriever()

            # Create managers with PHASE 1 FIX optimized values
            # FIX (Nov 26, 2025): Match create_session values - was missing optimized settings
            phase_manager = PhaseManager(memory, semantic_retriever=semantic_retriever)
            context_retriever = ContextRetriever(
                memory=memory,
                max_findings_per_phase=15,  # PHASE 1 FIX: Was using default 5
                max_recent_steps=50,         # PHASE 1 FIX: Was using default 10
                semantic_retriever=semantic_retriever,
                semantic_top_k=self.semantic_top_k,
                enable_cross_phase_search=self.semantic_enabled
            )
            compaction_manager = CompactionManager(
                soft_threshold=self.soft_threshold,
                hard_threshold=self.hard_threshold,
                context_window=self.context_window  # FIX #6: Pass context window
            )

            # Store session data
            session_data = SessionData(
                memory=memory,
                phase_manager=phase_manager,
                context_retriever=context_retriever,
                compaction_manager=compaction_manager
            )
            self._sessions[session_id] = session_data

            logger.info(f"Loaded task session: {session_id}")
            return memory

        except Exception as e:
            logger.error(f"Failed to load session {session_id}: {e}")
            return None

    def get_session(self, session_id: str) -> Optional[SessionData]:
        """Get session data for a session.

        Args:
            session_id: Session identifier

        Returns:
            SessionData if session exists, None otherwise
        """
        return self._sessions.get(session_id)

    def add_step_memory(
        self,
        session_id: str,
        step: int,
        brain_state: Dict[str, Any],
        action_summary: str,
        finding: Optional[str] = None,
        total_steps: Optional[int] = None
    ) -> bool:
        """Add memory for a step.

        This is the main integration point - call this after each agent step.

        Args:
            session_id: Session identifier
            step: Current step number
            brain_state: Brain state from agent (contains 'phase')
            action_summary: Summary of action taken
            finding: Optional finding from this step
            total_steps: Optional total steps for progress tracking

        Returns:
            True if successful, False if session not found
        """
        session_data = self._sessions.get(session_id)
        if not session_data:
            logger.warning(f"Session {session_id} not found")
            return False

        try:
            # Store brain state for next context injection (Phase 3: semantic search)
            if brain_state:
                session_data.last_brain_state = brain_state

            # Add step via phase manager (handles phase transitions)
            session_data.phase_manager.add_step(
                step_number=step,
                brain_state=brain_state,
                action_summary=action_summary,
                finding=finding
            )

            # Update progress
            if total_steps:
                session_data.phase_manager.update_progress(step, total_steps)

            # H-MEM Section 3.3: Memory Update Mechanisms
            # OPTIMIZATION (Nov 14, 2025): More aggressive consolidation
            if finding and self.enabled:
                # Track memories added
                if session_id not in self._memories_since_reflection:
                    self._memories_since_reflection[session_id] = 0
                self._memories_since_reflection[session_id] += 1

                # Get current phase
                current_phase = brain_state.get("phase", "discovery") if brain_state else "discovery"

                # OPTIMIZATION: More frequent reflection (25 findings instead of 100)
                if (self.reflection_enabled and
                    self._memories_since_reflection[session_id] >= self.reflection_threshold):
                    self._trigger_reflection(session_id, current_phase)
                    self._memories_since_reflection[session_id] = 0
                    logger.info(
                        f"💭 Reflection triggered for '{current_phase}' "
                        f"(threshold: {self.reflection_threshold} findings)"
                    )

                # Trigger forgetting if phase has too many findings
                if self.forgetting_enabled:
                    self._check_and_prune_memories(session_id, current_phase)

            logger.debug(f"Added step {step} to session {session_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to add step memory for session {session_id}: {e}")
            return False

    def check_loop_signal(self, session_id: str) -> Optional[str]:
        """FIX #5: Check if H-MEM is signaling a potential loop.
        
        This checks if the agent has been repeatedly generating similar/duplicate
        findings, indicating it's stuck in a loop without making progress.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Loop signal description if detected, None otherwise
        """
        session_data = self._sessions.get(session_id)
        if not session_data:
            return None
        
        try:
            return session_data.memory.get_loop_signal_info()
        except Exception as e:
            logger.debug(f"Error checking loop signal for {session_id}: {e}")
            return None
    
    def reset_loop_signal(self, session_id: str) -> None:
        """FIX #5: Reset the H-MEM loop signal after intervention.

        Args:
            session_id: Session identifier
        """
        session_data = self._sessions.get(session_id)
        if session_data:
            try:
                session_data.memory.reset_loop_signal()
            except Exception as e:
                logger.debug(f"Error resetting loop signal for {session_id}: {e}")

    def record_finding(
        self,
        session_id: str,
        finding: str,
        phase_name: Optional[str] = None,
        embedding: Optional[List[float]] = None
    ) -> bool:
        """Record a finding to the hierarchical memory with proper cache invalidation.

        FIX (Jan 2026): This is the preferred way to add findings as it ensures
        the context cache is properly invalidated.

        Args:
            session_id: Session identifier
            finding: Finding text to record
            phase_name: Optional phase name (defaults to current phase)
            embedding: Optional vector embedding for semantic search

        Returns:
            True if finding was added, False otherwise
        """
        session_data = self._sessions.get(session_id)
        if not session_data:
            logger.warning(f"Session {session_id} not found")
            return False

        try:
            # Get current phase if not specified
            if phase_name is None:
                phase_name = session_data.memory.current_phase

            # Add finding to memory
            result = session_data.memory.add_finding_to_phase(
                phase_name=phase_name,
                finding=finding,
                embedding=embedding
            )

            # Invalidate cache on successful addition
            if result:
                session_data.invalidate_cache()
                logger.debug(f"Added finding to phase '{phase_name}', cache invalidated (version {session_data._cache_version})")

            return result

        except Exception as e:
            logger.error(f"Failed to record finding for session {session_id}: {e}")
            return False

    def get_context_injection(self, session_id: str, brain_state: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Get formatted context for injection into agent prompt.

        FIX (Dec 12, 2025): Added step-level caching to prevent 5x redundant hierarchical searches.
        Before: Each token count check, context breakdown, and LLM call triggered a new H-MEM search.
        After: Cache result per step, invalidate when step count changes.

        Args:
            session_id: Session identifier
            brain_state: Optional brain state for semantic search query (Phase 3)
                        If None, uses last stored brain_state from previous step

        Returns:
            Formatted context string, or None if session not found
        """
        session_data = self._sessions.get(session_id)
        if not session_data:
            return None

        try:
            # Use step count as part of cache key
            current_step = len(session_data.memory.recent_steps)

            # FIX (Jan 2026): Use content-aware cache validation
            # Cache is valid only if: same step AND same version (no memory changes)
            if session_data.is_cache_valid(current_step):
                logger.debug(f"📦 H-MEM cache HIT for step {current_step} (version {session_data._cache_version})")
                return session_data._cached_context

            # Use provided brain_state, or fall back to last stored brain_state (Phase 3)
            effective_brain_state = brain_state or session_data.last_brain_state

            # Generate new context (expensive - involves hierarchical_search)
            context = session_data.context_retriever.get_context_injection(brain_state=effective_brain_state)

            # Cache for this step and version
            session_data._cached_context = context
            session_data._cached_step = current_step
            session_data._cached_at_version = session_data._cache_version
            logger.debug(f"📦 H-MEM cache MISS for step {current_step} - cached new context (version {session_data._cache_version})")
            
            return context
        except Exception as e:
            logger.error(f"Failed to get context for session {session_id}: {e}")
            return None

    def save_session(self, session_id: str, user_id: Optional[str] = None) -> bool:
        """Save session to disk.

        Args:
            session_id: Session identifier
            user_id: Optional user ID for path construction

        Returns:
            True if successful, False otherwise
        """
        session_data = self._sessions.get(session_id)
        if not session_data:
            logger.warning(f"Session {session_id} not found")
            return False

        try:
            memory_path = self._get_memory_path(session_id, user_id)
            session_data.memory.save(memory_path)
            logger.debug(f"Saved session {session_id} to {memory_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to save session {session_id}: {e}")
            return False

    def invalidate_context_cache(self, session_id: str) -> None:
        """Explicitly invalidate the context cache for a session.

        FIX (Jan 2026): Now uses SessionData.invalidate_cache() for proper version tracking.
        This increments the version counter, ensuring next get_context_injection() generates fresh context.

        Call this when you want to force a fresh H-MEM search, e.g.:
        - After adding significant new findings
        - After phase transitions
        - After memory pruning

        Args:
            session_id: Session identifier
        """
        session_data = self._sessions.get(session_id)
        if session_data:
            session_data.invalidate_cache()
            logger.debug(f"📦 H-MEM cache invalidated for session {session_id} (version now {session_data._cache_version})")

    def close_session(self, session_id: str, user_id: Optional[str] = None) -> bool:
        """Close and save a session with proper cache cleanup.

        FIX #14 (Nov 26, 2025): Clear caches on session end to prevent memory leaks.

        Args:
            session_id: Session identifier
            user_id: Optional user ID for path construction

        Returns:
            True if successful, False otherwise
        """
        if session_id not in self._sessions:
            return False

        try:
            # Save before closing
            self.save_session(session_id, user_id)

            # FIX #14: Clear caches to prevent memory leaks
            session_data = self._sessions[session_id]
            
            # Clear semantic retriever cache if present
            if hasattr(session_data, 'context_retriever') and session_data.context_retriever:
                if hasattr(session_data.context_retriever, 'semantic_retriever'):
                    sr = session_data.context_retriever.semantic_retriever
                    if sr and hasattr(sr, 'clear_cache'):
                        sr.clear_cache()
                        logger.debug(f"Cleared semantic retriever cache for session {session_id}")
            
            # §7.7: consolidate a short session's findings at close before we drop the
            # counter — this is what makes reflection actually fire for the autonomous
            # workload (short cron/goal sessions never reach the 25/step threshold).
            n_findings = self._memories_since_reflection.get(session_id, 0)
            if (self.reflection_enabled and self.reflection_on_session_close
                    and 0 < self.reflection_session_close_threshold <= n_findings):
                try:
                    phase = getattr(session_data.memory, "current_phase", "discovery")
                    self._trigger_reflection(session_id, phase)
                except Exception:
                    logger.debug("session-close reflection skipped for %s", session_id,
                                 exc_info=True)

            # Clear reflection tracking
            if session_id in self._memories_since_reflection:
                del self._memories_since_reflection[session_id]

            # Remove from active sessions
            del self._sessions[session_id]

            logger.info(f"Closed session {session_id} with cache cleanup")
            return True

        except Exception as e:
            logger.error(f"Failed to close session {session_id}: {e}")
            return False

    def get_session_statistics(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get statistics for a session.

        Args:
            session_id: Session identifier

        Returns:
            Statistics dictionary, or None if session not found
        """
        session_data = self._sessions.get(session_id)
        if not session_data:
            return None

        try:
            phase_stats = session_data.phase_manager.get_phase_statistics()
            context_stats = session_data.context_retriever.get_context_statistics()

            return {
                "session_id": session_id,
                "task": session_data.memory.task,
                "progress": session_data.memory.progress,
                **phase_stats,
                **context_stats
            }

        except Exception as e:
            logger.error(f"Failed to get statistics for session {session_id}: {e}")
            return None

    def _get_memory_path(self, session_id: str, user_id: Optional[str] = None) -> Path:
        """Get file path for hierarchical memory.

        Args:
            session_id: Session identifier
            user_id: Optional user ID

        Returns:
            Path to hierarchical_memory.json
        """
        if user_id:
            # user_id/sessions/session_id/hierarchical_memory.json
            return self._base_path / user_id / "sessions" / session_id / "hierarchical_memory.json"
        else:
            # sessions/session_id/hierarchical_memory.json
            return self._base_path / "sessions" / session_id / "hierarchical_memory.json"

    def get_active_sessions(self) -> List[str]:
        """Get list of active session IDs.

        Returns:
            List of session IDs
        """
        return list(self._sessions.keys())

    def get_session_count(self) -> int:
        """Get count of active sessions.

        Returns:
            Number of active sessions
        """
        return len(self._sessions)

    def _trigger_reflection(self, session_id: str, phase_name: str) -> None:
        """Trigger reflection process per H-MEM paper Section 3.3.

        Every REFLECTION_THRESHOLD memories (default: 25), the system triggers a
        reflection process to consolidate related memories and update phase summaries.
        Consolidation uses the aux LLM when REFLECTION_LLM_ENABLED (see _llm_consolidate),
        else falls back to concatenation.

        Args:
            session_id: Session identifier
            phase_name: Current phase name
        """
        session_data = self._sessions.get(session_id)
        if not session_data:
            return

        try:
            # Get phase memory
            phase_memory = session_data.memory.get_phase_by_name(phase_name)
            if not phase_memory or len(phase_memory.key_findings) == 0:
                return

            # Get recent findings (last N where N = reflection_threshold)
            n_recent = min(self.reflection_threshold, len(phase_memory.key_findings))
            recent_findings = phase_memory.key_findings[-n_recent:]

            logger.info(
                f"🔄 Reflection triggered for {session_id} phase '{phase_name}': "
                f"consolidating {len(recent_findings)} memories"
            )

            # Prefer LLM synthesis (H-MEM §3.3) when enabled; else concatenate.
            new_summary = self._llm_consolidate(recent_findings)
            if new_summary is None:
                if len(recent_findings) <= 3:
                    new_summary = "; ".join(recent_findings)
                else:
                    new_summary = (
                        f"{phase_name.title()} phase: {len(recent_findings)} key findings. "
                        f"Latest: {recent_findings[-1][:100]}..."
                    )

            # Update phase summary
            phase_memory.summary = new_summary

            # Optionally update phase embedding
            if session_data.phase_manager.semantic_retriever:
                try:
                    phase_embedding = session_data.phase_manager.semantic_retriever.embed_text(
                        new_summary
                    )
                    if phase_embedding is not None:
                        phase_memory.phase_embedding = phase_embedding.tolist()
                except Exception as e:
                    logger.warning(f"Failed to update phase embedding: {e}")

            logger.info(
                f"✅ Reflection completed for {session_id} phase '{phase_name}': "
                f"summary updated"
            )

        except Exception as e:
            logger.error(f"Reflection failed for {session_id}: {e}")

    def _llm_consolidate(self, findings: list) -> Optional[str]:
        """Synthesize a phase summary from findings via the aux model (H-MEM §3.3).

        Thin delegator to ReflectionService — the consolidation logic lives there.
        Returns None when disabled, no model, no findings, or on any error — the
        caller then falls back to the existing concatenation.
        """
        from .reflection_service import ReflectionService
        svc = ReflectionService(
            enabled=self.reflection_llm_enabled,
            llm=self.reflection_llm,
            meter_ctx=getattr(self, "reflection_meter_ctx", None),
        )
        return svc.consolidate(findings)

    def drain_promoted_findings(self, session_id: str) -> List[str]:
        """Return curated findings not yet handed to the cross-session store.

        Tracks emitted findings by CONTENT (a per-session set), NOT a positional
        high-water mark: key_findings is pruned/evicted and phases can be resumed,
        so any index/count over the flattened list loses or re-emits findings. The
        content set guarantees each distinct finding is emitted exactly once even as
        the underlying lists shrink.
        """
        session_data = self._sessions.get(session_id)
        if not session_data:
            return []
        emitted = getattr(session_data, '_emitted_findings', None)
        if emitted is None:
            emitted = set()
            session_data._emitted_findings = emitted
        new: List[str] = []
        for pm in session_data.memory.phase_memories:
            for finding in pm.key_findings:
                if finding not in emitted:
                    emitted.add(finding)
                    new.append(finding)
        return new

    def _check_and_prune_memories(self, session_id: str, phase_name: str) -> None:
        """Check if pruning needed and prune low-importance memories (H-MEM Section 3.3).

        When a phase exceeds max_findings_per_phase, prune in two passes: (1) remove
        every finding scoring below importance_threshold, then (2) if still over the cap,
        remove the lowest-importance findings until back at the cap. NOTE: without query
        embeddings the importance signal is near-uniform, so pass (2) degenerates to
        FIFO (oldest-first) eviction — bounded to the cap and logged, not silent.

        Args:
            session_id: Session identifier
            phase_name: Phase to check and prune
        """
        session_data = self._sessions.get(session_id)
        if not session_data:
            return

        try:
            # Get phase memory
            phase_memory = session_data.memory.get_phase_by_name(phase_name)
            if not phase_memory:
                return

            finding_count = len(phase_memory.key_findings)

            # Check if pruning needed
            if finding_count < self.max_findings_per_phase:
                return

            logger.info(
                f"🗑️  Pruning check for {session_id} phase '{phase_name}': "
                f"{finding_count} findings (max: {self.max_findings_per_phase})"
            )

            # Calculate importance for all findings
            importances = []
            for i in range(finding_count):
                # Use last brain state for relevance if available
                query_embedding = None
                if session_data.last_brain_state and session_data.phase_manager.semantic_retriever:
                    brain_memory = session_data.last_brain_state.get("memory", "")
                    if brain_memory:
                        try:
                            query_embedding = session_data.phase_manager.semantic_retriever.embed_text(
                                brain_memory
                            )
                            if query_embedding is not None:
                                query_embedding = query_embedding.tolist()
                        except Exception:
                            pass

                importance = phase_memory.calculate_importance(i, query_embedding)
                importances.append((i, importance))

            # Sort by importance
            importances.sort(key=lambda x: x[1])

            min_importance = self.importance_threshold

            # Two-pass eviction back to the cap.
            indices_to_remove = set()

            # First pass: remove everything below the importance threshold.
            for idx, importance in importances:
                if importance < min_importance:
                    indices_to_remove.add(idx)

            # Second pass: if still over max, remove lowest importance regardless of threshold.
            # MED-6: surface when this drops a finding we'd otherwise keep (>= threshold), so
            # over-cap data loss is observable rather than silently FIFO'd.
            if finding_count - len(indices_to_remove) > self.max_findings_per_phase:
                remaining_needed = finding_count - self.max_findings_per_phase - len(indices_to_remove)
                kept_worthy_dropped = 0
                for idx, importance in importances:
                    if idx not in indices_to_remove:
                        indices_to_remove.add(idx)
                        if importance >= min_importance:
                            kept_worthy_dropped += 1
                        remaining_needed -= 1
                        if remaining_needed <= 0:
                            break
                if kept_worthy_dropped:
                    logger.warning(
                        f"⚠️  Pruning '{phase_name}' dropped {kept_worthy_dropped} finding(s) "
                        f"at/above importance_threshold ({min_importance}) to honor the cap "
                        f"({self.max_findings_per_phase}) — consider raising MAX_FINDINGS_PER_PHASE."
                    )

            if not indices_to_remove:
                logger.debug(
                    f"No pruning needed for {phase_name} - {finding_count} findings all above threshold"
                )
                return

            # FIX #2: Build index mapping from old indices to new indices
            # This allows us to update sub_memory_indices correctly
            old_to_new_index = {}
            new_idx = 0
            for old_idx in range(len(phase_memory.key_findings)):
                if old_idx not in indices_to_remove:
                    old_to_new_index[old_idx] = new_idx
                    new_idx += 1

            # Remove low-importance memories
            phase_memory.key_findings = [
                f for i, f in enumerate(phase_memory.key_findings)
                if i not in indices_to_remove
            ]
            phase_memory.finding_embeddings = [
                e for i, e in enumerate(phase_memory.finding_embeddings)
                if i not in indices_to_remove
            ]
            phase_memory.finding_importance = [
                s for i, s in enumerate(phase_memory.finding_importance)
                if i not in indices_to_remove
            ]
            phase_memory.finding_last_accessed = [
                ts for i, ts in enumerate(phase_memory.finding_last_accessed)
                if i not in indices_to_remove
            ]
            phase_memory.finding_access_count = [
                c for i, c in enumerate(phase_memory.finding_access_count)
                if i not in indices_to_remove
            ]

            # FIX #2: Update sub_memory_indices to point to new positions
            # Remove indices that were pruned, remap remaining indices
            phase_memory.sub_memory_indices = [
                old_to_new_index[old_idx]
                for old_idx in phase_memory.sub_memory_indices
                if old_idx in old_to_new_index  # Skip pruned indices
            ]

            logger.info(
                f"✅ Pruned {len(indices_to_remove)} low-importance memories from {phase_name}. "
                f"Remaining: {len(phase_memory.key_findings)}, "
                f"sub_memory_indices updated: {len(phase_memory.sub_memory_indices)}"
            )

        except Exception as e:
            logger.error(f"Memory pruning failed for {session_id}: {e}")
