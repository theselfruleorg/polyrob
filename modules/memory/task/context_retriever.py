"""Context retriever for phase-based and semantic memory injection.

The ContextRetriever provides phase-filtered context for injection into
the agent's prompt using H-MEM position-indexed routing.

Key Enhancement (Phase 1):
    Position-indexed lookup by phase name enables efficient hierarchical navigation.
    phase_index[phase_name] → position → phase_memories[position]

Expected token usage:
    - Session summary: ~100 tokens
    - Current phase: ~300 tokens
    - Semantic matches: ~100 tokens (cross-phase)
    - Previous phases summary: ~100 tokens
    - Recent steps: ~200 tokens
    Total: ~700-900 tokens adaptive (based on semantic relevance)
"""

import logging
from typing import Optional, List, Dict, Any
from .hierarchical_memory import HierarchicalMemory, PhaseMemory, Step
from modules.llm import count_tokens

logger = logging.getLogger(__name__)


class ContextRetriever:
    """Retrieves phase-based and semantic context for injection into agent prompts.

    The ContextRetriever formats hierarchical memory into a concise context
    string suitable for injection. It uses position-indexed lookup for efficient
    access and supports cross-phase semantic search.

    Enhancement (Phase 1):
        Uses position indices for O(1) phase access instead of dict iteration.
        phase_index mapping enables backward compatibility.

    Attributes:
        memory: HierarchicalMemory instance
        max_findings_per_phase: Maximum findings to include per phase
        max_recent_steps: Maximum recent steps to include
        include_completed_phases: Whether to include completed phases summary
        semantic_retriever: Optional SemanticRetriever for cross-phase search
    """

    def __init__(
        self,
        memory: HierarchicalMemory,
        max_findings_per_phase: int = 15,  # PHASE 1 FIX (Nov 4): Increased from 5 to 15
        max_recent_steps: int = 50,  # PHASE 1 FIX (Nov 4): Increased from 20 to 50 (show 50% of tracked 100)
        include_completed_phases: bool = True,
        semantic_retriever: Optional[Any] = None,  # Still optional for backward compat
        semantic_top_k: int = 50,  # PHASE 1 FIX (Nov 4): Increased from 10 to 50 for better context
        enable_cross_phase_search: bool = True,  # Phase 2 feature flag
        compress_recent_steps: bool = True  # NEW (Jan 2026): Compress recent steps to save ~4K tokens
    ):
        """Initialize context retriever with MASSIVELY expanded H-MEM context window.

        PHASE 1 FIX (Nov 4, 2025): Dramatically expanded for 200-1000+ step tasks!

        Context Budget (SCALED FOR LONG TASKS):
        - HierarchicalMemory NOW tracks 100 recent steps (hierarchical_memory.py:561)
        - Shows 50 most recent steps (50% of tracked, covers last ~25-250 actions)
        - More findings per phase: 15 (up from 5, 3x increase)
        - More semantic matches: 50 (up from 10, 5x increase)

        Token Impact:
        - Before Phase 1: ~600-900 tokens (20 steps, 5 findings, 10 semantic)
        - After Phase 1: ~4,000-6,000 tokens (50 steps, 15 findings, 50 semantic)
        - Still <0.6% of 1M context window (5K tokens / 1M = 0.5%)

        Rationale for 200-1000 Step Tasks:
        - Complex tasks run 200-1000+ steps over hours
        - Previous 20-step tracking = 2-10% coverage (SEVERE context loss!)
        - New 100-step tracking + 50-step display = 10-50% coverage
        - With 1M window: 6K tokens for H-MEM is TINY (0.6%), huge value
        
        Why Not More?
        - 100 tracked steps is sweet spot (memory vs coverage)
        - 50 displayed steps balances detail vs noise
        - Older context lives in phase summaries + semantic search
        - Can increase further if needed (monitor utilization)

        Args:
            memory: HierarchicalMemory instance
            max_findings_per_phase: Max findings to show per phase (default 15, was 5)
            max_recent_steps: Max recent steps to show (default 50, was 20)
            include_completed_phases: Include completed phases summary (default True)
            semantic_retriever: Optional SemanticRetriever (recommended)
            semantic_top_k: Number of semantic matches to retrieve (default 50, was 10)
            enable_cross_phase_search: Enable hierarchical cross-phase search (default True)
        """
        self.memory = memory
        self.max_findings_per_phase = max_findings_per_phase
        self.max_recent_steps = max_recent_steps
        self.include_completed_phases = include_completed_phases
        self.semantic_retriever = semantic_retriever
        self.semantic_top_k = semantic_top_k
        self.enable_cross_phase_search = enable_cross_phase_search
        self.compress_recent_steps = compress_recent_steps  # NEW (Jan 2026)

        # FIX #5: Explicit warning when semantic search is disabled
        if not semantic_retriever:
            if enable_cross_phase_search:
                logger.warning(
                    "⚠️ H-MEM SEMANTIC SEARCH DISABLED: SemanticRetriever not provided. "
                    "Cross-phase search will be limited to exact matches. "
                    "For best context retrieval, ensure the local embedding model is configured."
                )
            else:
                logger.info(
                    "ℹ️  Semantic search disabled by configuration (enable_cross_phase_search=False)."
                )
        else:
            logger.info(
                f"✅ Semantic search enabled: cross-phase queries will use vector similarity "
                f"(top_k={semantic_top_k})"
            )

        logger.debug(
            f"ContextRetriever initialized for session {memory.session_id} "
            f"(semantic={'enabled' if semantic_retriever else 'disabled'}, "
            f"cross_phase={'enabled' if enable_cross_phase_search else 'disabled'})"
        )

    def hierarchical_search(
        self,
        query: str,
        top_k_phases: int = 2,
        top_k_findings_per_phase: int = 3
    ) -> List[tuple]:
        """H-MEM hierarchical retrieval: Progressive layer-by-layer navigation.

        Implements the core H-MEM algorithm from Section 3.2:
        1. Semantic vector encoding of query
        2. Similarity with Phase Layer -> top-k phases
        3. For each selected phase: similarity with findings -> top-k findings

        This enables efficient filtering without exhaustive search.

        Args:
            query: Query text to search for
            top_k_phases: Number of most relevant phases to select (default 2)
            top_k_findings_per_phase: Findings to retrieve per phase (default 3)

        Returns:
            List of (finding, phase_name, score) tuples, sorted by score
        """
        if not self.semantic_retriever:
            logger.warning("⚠️ Hierarchical search requires semantic_retriever")
            return []

        if not self.memory.phase_memories:
            return []

        try:
            # Step 1: Embed the query
            from .semantic_retriever import SemanticRetriever
            query_embedding = self.semantic_retriever.embed_text(query)
            if query_embedding is None:
                return []

            # Step 2: Score all phases using position indices
            phase_scores = []
            for idx, phase_memory in enumerate(self.memory.phase_memories):
                if not phase_memory.finding_embeddings:
                    continue

                # Calculate average similarity to phase (aggregate of findings)
                similarities = []
                for finding_emb in phase_memory.finding_embeddings:
                    if finding_emb and len(finding_emb) > 0:
                        import numpy as np
                        finding_emb_np = np.array(finding_emb)
                        sim = SemanticRetriever.cosine_similarity(
                            query_embedding,
                            finding_emb_np
                        )
                        similarities.append(sim)

                if similarities:
                    avg_similarity = sum(similarities) / len(similarities)
                    phase_scores.append((idx, phase_memory.phase_name, avg_similarity))

            # Sort phases by similarity
            phase_scores.sort(key=lambda x: x[2], reverse=True)

            # Step 3: Select top-k phases
            top_phases = phase_scores[:top_k_phases]

            logger.debug(
                f"🔍 H-MEM Layer 2: Selected {len(top_phases)} phases from {len(phase_scores)} "
                f"(top: {top_phases[0][1] if top_phases else 'none'})"
            )

            # Step 4: For each selected phase, find top-k findings using position indices
            all_findings = []
            for phase_idx, phase_name, phase_score in top_phases:
                phase_memory = self.memory.phase_memories[phase_idx]

                # Use sub_memory_indices if available (H-MEM optimization)
                if phase_memory.sub_memory_indices:
                    # Only search indexed findings
                    search_indices = phase_memory.sub_memory_indices
                    logger.debug(
                        f"🎯 Using position indices: {len(search_indices)} of "
                        f"{len(phase_memory.key_findings)} findings"
                    )
                else:
                    # Fallback: search all findings
                    search_indices = list(range(len(phase_memory.key_findings)))

                finding_scores = []
                for finding_idx in search_indices:
                    if finding_idx >= len(phase_memory.key_findings):
                        continue

                    finding = phase_memory.key_findings[finding_idx]
                    if finding_idx < len(phase_memory.finding_embeddings):
                        finding_emb = phase_memory.finding_embeddings[finding_idx]
                        if finding_emb and len(finding_emb) > 0:
                            import numpy as np
                            finding_emb_np = np.array(finding_emb)
                            sim = SemanticRetriever.cosine_similarity(
                                query_embedding,
                                finding_emb_np
                            )
                            finding_scores.append((finding, phase_name, sim))

                # Sort findings by similarity
                finding_scores.sort(key=lambda x: x[2], reverse=True)

                # Take top-k findings from this phase
                top_findings = finding_scores[:top_k_findings_per_phase]
                all_findings.extend(top_findings)

                # FIX #4: Correct f-string conditional syntax
                best_score = top_findings[0][2] if top_findings else 0
                logger.debug(
                    f"📍 Phase '{phase_name}': {len(top_findings)} findings "
                    f"(best: {best_score:.2f})"
                )

            # Sort all findings globally by score
            all_findings.sort(key=lambda x: x[2], reverse=True)

            logger.info(
                f"✅ H-MEM hierarchical search: {len(all_findings)} findings "
                f"from {len(top_phases)} phases"
            )

            return all_findings

        except Exception as e:
            logger.error(f"❌ Hierarchical search failed: {e}")
            return []

    def get_context_injection(self, current_phase: Optional[str] = None, brain_state: Optional[Dict[str, Any]] = None) -> str:
        """Get formatted context for injection (phase-filtered).

        This is the main method - returns ~600-750 token context string with
        only relevant memories for current phase, plus semantically similar
        findings from other phases.

        Args:
            current_phase: Phase to get context for (if None, uses memory.current_phase)
            brain_state: Optional brain state for semantic query extraction

        Returns:
            Formatted context string ready for injection
        """
        phase = current_phase or self.memory.current_phase

        sections = []

        # Layer 1: Session summary (~100 tokens)
        sections.append(self._format_session_summary())

        # Layer 2: Current phase memory (~300 tokens)
        current_phase_context = self._format_current_phase(phase)
        if current_phase_context:
            sections.append(current_phase_context)

        # Semantic search across all phases (~100-200 tokens) - Phase 2 enhancement
        if self.enable_cross_phase_search and self.semantic_retriever and brain_state:
            semantic_context = self._format_semantic_matches_hierarchical(brain_state, phase)
            if not semantic_context:
                # Hierarchical (embedding) path produced nothing — happens when no embedder
                # is available (e.g. LexicalRetriever.embed_text returns None). Fall back to
                # the flat search path so section-3 still works via lexical retrieval.
                semantic_context = self._format_semantic_matches(brain_state, phase)
            if semantic_context:
                sections.append(semantic_context)
        elif self.semantic_retriever and brain_state:
            # Fallback to old method if cross_phase disabled
            semantic_context = self._format_semantic_matches(brain_state, phase)
            if semantic_context:
                sections.append(semantic_context)

        # Completed phases summary (~100 tokens) - BEFORE recent steps
        if self.include_completed_phases:
            completed_context = self._format_completed_phases()
            if completed_context:
                sections.append(completed_context)

        # Layer 3: Recent steps (compressed: ~200 tokens, full: ~5000 tokens)
        # OPTIMIZATION (Jan 2026): Use compressed format to save ~4000 tokens
        recent_context = self._format_recent_steps(compress=self.compress_recent_steps)
        if recent_context:
            sections.append(recent_context)

        return "\n\n".join(sections)

    def _format_session_summary(self) -> str:
        """Format Layer 1: Session summary.

        Returns:
            Formatted session summary string
        """
        lines = [
            "[HIERARCHICAL MEMORY - SESSION CONTEXT]",
            "",
            f"Session: {self.memory.session_id}",
            f"Task: {self.memory.task}",
            f"Progress: {self.memory.progress}",
            f"Current Phase: {self.memory.current_phase}"
        ]

        return "\n".join(lines)

    def _format_current_phase(self, phase_name: str) -> Optional[str]:
        """Format Layer 2: Current phase memory with importance-weighted retrieval.

        OPTIMIZATION (Nov 14, 2025): Smart finding selection
            - Ranks findings by importance (not just recency)
            - Shows most relevant findings first
            - Better context quality

        This is the KEY benefit - only inject current phase, not all phases!

        Args:
            phase_name: Phase to format

        Returns:
            Formatted phase context, or None if phase doesn't exist
        """
        # Position-indexed lookup via helper method
        phase_memory = self.memory.get_phase_by_name(phase_name)

        if not phase_memory:
            return None

        lines = [
            f"[CURRENT PHASE: {phase_name.upper()}]",
            ""
        ]

        # Phase summary
        if phase_memory.summary:
            lines.append(f"Summary: {phase_memory.summary}")
            lines.append("")

        # OPTIMIZATION: Smart finding selection by importance
        if phase_memory.key_findings:
            n = len(phase_memory.key_findings)
            # D5 FIX (2026-07-11): add_finding stamps every finding 1.0 and nothing
            # recalculates below the prune trigger, so the importance sort (stable)
            # kept insertion order and the top-N slice froze on the OLDEST findings
            # for phases between the display cap and the prune cap. When importance
            # carries no signal (all stored values identical), fall back to recency
            # so the newest findings win the slice.
            stored = phase_memory.finding_importance[:n]
            importance_is_flat = len(set(stored)) <= 1

            # Calculate importance scores for all findings
            finding_scores = []
            for i in range(n):
                # Use existing importance or calculate it
                if i < len(stored) and not importance_is_flat:
                    importance = stored[i]
                else:
                    # Fallback: recency-based importance
                    recency_score = (i + 1) / n
                    importance = recency_score

                finding_scores.append((i, importance, phase_memory.key_findings[i]))
            
            # Sort by importance (descending)
            finding_scores.sort(key=lambda x: x[1], reverse=True)
            
            # Take top N by importance (not just last N)
            top_findings = finding_scores[:self.max_findings_per_phase]
            
            # Re-sort by index to maintain chronological order
            top_findings.sort(key=lambda x: x[0])
            
            lines.append(
                f"Key Findings (top {len(top_findings)} by importance of {len(phase_memory.key_findings)} total):"
            )

            for rank, (idx, score, finding) in enumerate(top_findings, 1):
                # FIX #17: Update access tracking when finding is retrieved
                # This improves importance scoring for future retrievals
                phase_memory.update_access(idx)
                
                # Show importance indicator
                importance_icon = "⭐" if score > 0.8 else "•"
                lines.append(f"  {importance_icon} {finding}")

        return "\n".join(lines)

    def _format_semantic_matches_hierarchical(
        self,
        brain_state: Dict[str, Any],
        current_phase: str
    ) -> Optional[str]:
        """Format semantically similar findings using H-MEM hierarchical search (Phase 2).

        Uses hierarchical retrieval to efficiently find relevant findings across
        all phases using position indices.

        Args:
            brain_state: Brain state with 'memory', 'next', 'reasoning' fields
            current_phase: Current phase name (for reference)

        Returns:
            Formatted semantic matches string, or None if no matches
        """
        try:
            # Extract query from brain state
            query_parts = []
            if brain_state.get('memory'):
                query_parts.append(brain_state['memory'])
            if brain_state.get('next'):
                query_parts.append(brain_state['next'])
            if brain_state.get('reasoning'):
                query_parts.append(brain_state['reasoning'])

            if not query_parts:
                return None

            query = " ".join(query_parts)

            # Use hierarchical search (H-MEM Phase 2)
            matches = self.hierarchical_search(
                query=query,
                top_k_phases=2,  # Search top 2 most relevant phases
                top_k_findings_per_phase=3  # Get 3 findings per phase
            )

            if not matches:
                return None

            # Filter out findings from current phase (optional - may want to keep them)
            # For now, show ALL matches including current phase for better context
            # cross_phase_matches = [
            #     (finding, phase, score)
            #     for finding, phase, score in matches
            #     if phase != current_phase
            # ]

            # Format matches (including current phase)
            lines = [
                "[SEMANTICALLY RELATED MEMORIES (H-MEM Hierarchical Search)]",
                ""
            ]

            for finding, phase, score in matches[:self.semantic_top_k]:
                # Format: [phase] finding (similarity%)
                similarity_pct = int(score * 100)
                phase_marker = "📍" if phase == current_phase else "🔗"
                lines.append(f"{phase_marker} [{phase}] {finding} ({similarity_pct}% match)")

            logger.debug(
                f"🔍 Hierarchical search: {len(matches)} findings "
                f"({sum(1 for _, p, _ in matches if p != current_phase)} cross-phase)"
            )

            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"⚠️ Hierarchical semantic search failed: {e}")
            return None

    def _format_semantic_matches(self, brain_state: Dict[str, Any], current_phase: str) -> Optional[str]:
        """Format semantically similar findings from other phases (legacy method).

        Uses flat semantic search across all findings. This is the fallback
        when hierarchical search is disabled.

        Args:
            brain_state: Brain state with 'memory', 'next', 'reasoning' fields
            current_phase: Current phase name (to exclude from results)

        Returns:
            Formatted semantic matches string, or None if no matches
        """
        try:
            # Extract query from brain state
            # Combine memory, next action, and reasoning for context
            query_parts = []
            if brain_state.get('memory'):
                query_parts.append(brain_state['memory'])
            if brain_state.get('next'):
                query_parts.append(brain_state['next'])
            if brain_state.get('reasoning'):
                query_parts.append(brain_state['reasoning'])

            if not query_parts:
                return None

            query = " ".join(query_parts)

            # Get findings for search.  Prefer the pre-embedded set (faster for
            # SemanticRetriever); fall back to all findings so that LexicalRetriever
            # (which does not need pre-stored embeddings) can still search.
            findings_dict = self.memory.get_all_findings_with_embeddings()
            if not findings_dict:
                findings_dict = {
                    pm.phase_name: pm.key_findings[:]
                    for pm in self.memory.phase_memories
                    if pm.key_findings
                }

            if not findings_dict:
                return None

            # Search for semantic matches
            matches = self.semantic_retriever.search_similar(
                query=query,
                findings=findings_dict,
                top_k=self.semantic_top_k
            )

            if not matches:
                return None

            # Filter out findings from current phase (already shown)
            cross_phase_matches = [
                (finding, phase, score)
                for finding, phase, score in matches
                if phase != current_phase
            ]

            if not cross_phase_matches:
                return None

            # Format matches
            lines = [
                "[SEMANTICALLY RELATED FROM OTHER PHASES]",
                ""
            ]

            for finding, phase, score in cross_phase_matches:
                # Format: [phase] finding (similarity%)
                similarity_pct = int(score * 100)
                lines.append(f"• [{phase}] {finding} ({similarity_pct}% match)")

            logger.debug(f"🔍 Found {len(cross_phase_matches)} semantic matches across phases")

            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"⚠️ Semantic search failed: {e}")
            return None

    def _format_completed_phases(self) -> Optional[str]:
        """Format completed phases summary.

        Brief summary of what was accomplished in earlier phases.

        Returns:
            Formatted completed phases string, or None if no completed phases
        """
        if not self.memory.phases_completed:
            return None

        lines = [
            "[COMPLETED PHASES]",
            ""
        ]

        for phase_name in self.memory.phases_completed:
            phase_memory = self.memory.get_phase_by_name(phase_name)
            if phase_memory:
                findings_count = len(phase_memory.key_findings)
                lines.append(
                    f"✓ {phase_name}: {phase_memory.summary} "
                    f"({findings_count} findings)"
                )

        return "\n".join(lines)

    def _format_recent_steps(self, compress: bool = False) -> Optional[str]:
        """Format Layer 3: Recent steps with optional compression.

        Shows most recent actions across all phases (helps with continuity).
        
        OPTIMIZATION (Jan 2026): When compress=True, groups steps by phase
        for 5-10x token reduction (5000 tokens -> 500 tokens).

        Args:
            compress: If True, use compressed format grouping by phase

        Returns:
            Formatted recent steps string, or None if no steps
        """
        if not self.memory.recent_steps:
            return None

        # Get most recent N steps
        steps_to_show = self.memory.recent_steps[-self.max_recent_steps:]

        if not steps_to_show:
            return None

        # Use compressed format if requested and many steps
        if compress and len(steps_to_show) > 10:
            return self._format_recent_steps_compressed(steps_to_show)

        # Standard detailed format
        lines = [
            f"[RECENT ACTIVITY - Last {len(steps_to_show)} steps]",
            ""
        ]

        for step in steps_to_show:
            # Format: [Step #] phase: action
            line = f"[{step.step}] {step.phase}: {step.action_summary}"
            lines.append(line)

            # Include finding if present (indented)
            if step.finding:
                lines.append(f"     → {step.finding}")

        return "\n".join(lines)

    def _format_recent_steps_compressed(self, steps: list) -> str:
        """Compressed recent steps format - groups by phase.
        
        Reduces tokens by ~10x while preserving key information.
        
        Instead of:
            [1] discovery: Searched google
            [2] discovery: Clicked result
            [3] collection: Extracted data
            ...
            
        Produces:
            [RECENT: 50 steps]
            steps 1-15 (discovery): searched, clicked, navigated to 5 sites
            steps 16-40 (collection): extracted 8 pages, saved to files
            steps 41-50 (processing): analyzed data, created summary
        
        Args:
            steps: List of Step objects to format
            
        Returns:
            Compressed recent steps string
        """
        from collections import defaultdict
        
        # Group steps by phase with step ranges
        phase_ranges = defaultdict(lambda: {"start": float('inf'), "end": 0, "actions": set(), "count": 0})
        
        for step in steps:
            phase = step.phase
            phase_ranges[phase]["start"] = min(phase_ranges[phase]["start"], step.step)
            phase_ranges[phase]["end"] = max(phase_ranges[phase]["end"], step.step)
            phase_ranges[phase]["count"] += 1
            
            # Extract key action (first word or phrase)
            action_key = step.action_summary[:40].split(',')[0].strip()
            if action_key:
                phase_ranges[phase]["actions"].add(action_key)
        
        lines = [f"[RECENT: {len(steps)} steps grouped by phase]", ""]
        
        # Sort phases by start step
        sorted_phases = sorted(phase_ranges.items(), key=lambda x: x[1]["start"])
        
        for phase, data in sorted_phases:
            step_range = f"steps {data['start']}-{data['end']}" if data['start'] != data['end'] else f"step {data['start']}"
            # Take first 5 unique actions
            action_samples = list(data["actions"])[:5]
            actions_str = ", ".join(action_samples)
            if len(data["actions"]) > 5:
                actions_str += f"... (+{len(data['actions']) - 5} more)"
            
            lines.append(f"• {step_range} ({phase}, {data['count']} steps): {actions_str}")
        
        return "\n".join(lines)

    def get_phase_context(self, phase_name: str) -> Optional[str]:
        """Get context for a specific phase (not current).

        Useful for reviewing past phases or cross-phase analysis.

        Args:
            phase_name: Phase to get context for

        Returns:
            Formatted phase context, or None if phase doesn't exist
        """
        return self._format_current_phase(phase_name)

    def get_all_findings(self, phase_name: Optional[str] = None) -> List[str]:
        """Get all findings for a phase (or all phases) - position-indexed.

        Args:
            phase_name: Phase to get findings for (if None, returns all)

        Returns:
            List of finding strings
        """
        if phase_name:
            phase_memory = self.memory.get_phase_by_name(phase_name)
            return phase_memory.key_findings if phase_memory else []

        # All phases - iterate over list
        all_findings = []
        for phase_memory in self.memory.phase_memories:
            all_findings.extend(phase_memory.key_findings)

        return all_findings

    def get_context_token_estimate(self) -> int:
        """Estimate token count for context injection.

        Uses rough approximation: ~4 characters per token.

        Returns:
            Estimated token count
        """
        context = self.get_context_injection()
        # Use generic model name - will fall back to character-based estimation
        return count_tokens(context, "default")

    def get_context_statistics(self) -> Dict[str, Any]:
        """Get statistics about context.

        Returns:
            Dictionary with context statistics
        """
        context = self.get_context_injection()

        return {
            "total_phases": len(self.memory.phase_memories),
            "completed_phases": len(self.memory.phases_completed),
            "current_phase": self.memory.current_phase,
            "total_findings": sum(
                len(pm.key_findings)
                for pm in self.memory.phase_memories  # Now a list
            ),
            "recent_steps_count": len(self.memory.recent_steps),
            "context_length_chars": len(context),
            "context_length_tokens_estimate": len(context) // 4
        }

    def format_for_display(self, include_steps: bool = True) -> str:
        """Format memory for human-readable display.

        Args:
            include_steps: Include recent steps (default True)

        Returns:
            Formatted string for display/debugging
        """
        lines = [
            "=" * 80,
            "HIERARCHICAL MEMORY SNAPSHOT",
            "=" * 80,
            "",
            f"Session: {self.memory.session_id}",
            f"Task: {self.memory.task}",
            f"Progress: {self.memory.progress}",
            "",
            f"Current Phase: {self.memory.current_phase}",
            f"Total Phases: {len(self.memory.phase_memories)}",
            f"Completed: {len(self.memory.phases_completed)}",
            "",
            "-" * 80,
            "PHASES",
            "-" * 80,
            ""
        ]

        # Show all phases (position-indexed iteration)
        for phase_memory in self.memory.phase_memories:
            status_icon = "✓" if phase_memory.status == "completed" else "→"
            lines.append(f"{status_icon} {phase_memory.phase_name.upper()}")
            lines.append(f"  Steps: {phase_memory.started_step}-{phase_memory.ended_step or '?'}")
            lines.append(f"  Summary: {phase_memory.summary or '(none)'}")
            lines.append(f"  Findings: {len(phase_memory.key_findings)}")
            # Show sub_memory_indices if present
            if phase_memory.sub_memory_indices:
                lines.append(f"  Sub-indices: {phase_memory.sub_memory_indices[:10]}")  # First 10

            if phase_memory.key_findings:
                for finding in phase_memory.key_findings[-3:]:  # Last 3
                    lines.append(f"    • {finding}")

            lines.append("")

        if include_steps and self.memory.recent_steps:
            lines.append("-" * 80)
            lines.append("RECENT STEPS")
            lines.append("-" * 80)
            lines.append("")

            for step in self.memory.recent_steps[-10:]:  # Last 10
                lines.append(f"[{step.step}] {step.phase}: {step.action_summary}")
                if step.finding:
                    lines.append(f"  → {step.finding}")

        lines.append("")
        lines.append("=" * 80)

        return "\n".join(lines)
