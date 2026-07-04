"""Hierarchical memory models for task agents.

Implements H-MEM (Hierarchical Memory) 3-layer architecture:
    Layer 1: Session Summary (always in context)
    Layer 2: Phase Memories (list with position indices)
    Layer 3: Recent Steps (rolling window, last 100)

Key Enhancements:
    Phase 1: Position index encoding for efficient hierarchical retrieval
    Phase 2: Mandatory semantic search with cross-phase navigation

Reference:
    H-MEM Paper: https://arxiv.org/pdf/2507.22925
    Section 3.1: "H-MEM adopts a hierarchical memory structure, dividing memory
    into four levels according to the degree of semantic abstraction"
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field, ConfigDict

logger = logging.getLogger(__name__)


class Step(BaseModel):
    """Individual step in the task execution (Episode Layer - H-MEM Layer 4).

    Represents a single action taken by the agent, tagged with its phase
    for hierarchical organization.

    NEW (H-MEM Paper Completion):
        Added step_embedding for semantic search at episode layer.
        This enables hierarchical retrieval down to individual steps.

    Attributes:
        step: Step number in the session
        phase: Phase this step belongs to (discovery, collection, processing, documentation)
        action_summary: Brief description of the action taken
        finding: Key finding or result from this step (if any)
        step_embedding: Vector embedding of action_summary (for semantic search)
        timestamp: When this step was executed
    """
    model_config = ConfigDict(extra='forbid')

    step: int = Field(..., description="Step number in session")
    phase: str = Field(..., description="Phase this step belongs to")
    action_summary: str = Field(..., description="Brief summary of action taken")
    finding: Optional[str] = Field(None, description="Key finding from this step")
    step_embedding: List[float] = Field(
        default_factory=list,
        description="Vector embedding of action summary (H-MEM Layer 4)"
    )
    timestamp: datetime = Field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary with ISO timestamp."""
        return {
            "step": self.step,
            "phase": self.phase,
            "action_summary": self.action_summary,
            "finding": self.finding,
            "step_embedding": self.step_embedding,
            "timestamp": self.timestamp.isoformat()
        }


class PhaseMemory(BaseModel):
    """Memory for a single phase of task execution (Category Layer - H-MEM Layer 2).

    Groups all findings and steps for one phase (e.g., discovery, collection).

    KEY ENHANCEMENTS:
        Phase 1: sub_memory_indices for hierarchical routing (H-MEM Section 3.2)
        Paper Completion: phase_embedding + importance scoring (H-MEM Section 3.3)

    H-MEM Paper Section 3.3 - Memory Update Mechanisms:
        - Importance scoring: α·recency + β·relevance + γ·frequency
        - Reflection: Triggered every N memories
        - Forgetting: Remove low-importance memories

    Attributes:
        phase_name: Name of the phase (discovery, collection, processing, documentation)
        started_step: Step number where this phase started
        ended_step: Step number where this phase ended (None if still active)
        summary: High-level summary of what was accomplished in this phase
        phase_embedding: Vector embedding of phase summary (H-MEM Layer 2)
        key_findings: List of important findings from this phase (strings only, simple)
        finding_embeddings: Vector embeddings for findings (for semantic search)
        sub_memory_indices: Position indices to semantically related findings (H-MEM core)
        finding_importance: Importance scores for each finding (H-MEM Section 3.3)
        finding_last_accessed: Last access timestamp for each finding (for recency)
        finding_access_count: Access count for each finding (for frequency)
        status: Whether phase is "active" or "completed"
    """
    model_config = ConfigDict(extra='forbid')

    phase_name: str = Field(..., description="Name of the phase")
    started_step: int = Field(..., description="Step where phase started")
    ended_step: Optional[int] = Field(None, description="Step where phase ended")
    summary: str = Field(default="", description="Summary of phase accomplishments")

    # NEW: Phase-level embedding (H-MEM Layer 2)
    phase_embedding: List[float] = Field(
        default_factory=list,
        description="Vector embedding of phase summary (for hierarchical search)"
    )

    key_findings: List[str] = Field(default_factory=list, description="Key findings from this phase")
    finding_embeddings: List[List[float]] = Field(
        default_factory=list,
        description="Vector embeddings for findings (parallel to key_findings)"
    )

    # Position indices for hierarchical routing (H-MEM paper core innovation)
    sub_memory_indices: List[int] = Field(
        default_factory=list,
        description="Indices of semantically related findings (for efficient retrieval)"
    )

    # NEW: Importance scoring fields (H-MEM Section 3.3)
    finding_importance: List[float] = Field(
        default_factory=list,
        description="Importance scores: α·recency + β·relevance + γ·frequency"
    )
    finding_last_accessed: List[datetime] = Field(
        default_factory=list,
        description="Last access timestamps for recency scoring"
    )
    finding_access_count: List[int] = Field(
        default_factory=list,
        description="Access counts for frequency scoring"
    )

    status: str = Field(default="active", description="active or completed")
    created_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = Field(None)

    def finalize(self, end_step: int, summary: Optional[str] = None) -> None:
        """Mark phase as completed.

        Args:
            end_step: Final step number of this phase
            summary: Optional summary override (if None, keeps existing)
        """
        self.ended_step = end_step
        self.status = "completed"
        self.completed_at = datetime.now()
        if summary:
            self.summary = summary

    def add_finding(self, finding: str, embedding: Optional[List[float]] = None) -> None:
        """Add a key finding to this phase with importance tracking.

        FIX #1: Transactional integrity for parallel arrays.
        All arrays must stay in sync - if any append fails, rollback all.

        Args:
            finding: Finding to add
            embedding: Optional vector embedding for semantic search
        """
        if finding and finding not in self.key_findings:
            # FIX #1: Track original lengths for rollback
            original_len = len(self.key_findings)

            try:
                self.key_findings.append(finding)

                # Add embedding if provided (parallel array)
                if embedding is not None:
                    self.finding_embeddings.append(embedding)
                else:
                    # Add empty placeholder to maintain parallel structure
                    self.finding_embeddings.append([])

                # Initialize importance tracking (H-MEM Section 3.3)
                self.finding_importance.append(1.0)  # Start with max importance
                self.finding_last_accessed.append(datetime.now())
                self.finding_access_count.append(0)  # Will increment on first access

                # FIX #1: Validate all arrays stayed in sync
                expected_len = original_len + 1
                if not (len(self.key_findings) == expected_len and
                        len(self.finding_embeddings) == expected_len and
                        len(self.finding_importance) == expected_len and
                        len(self.finding_last_accessed) == expected_len and
                        len(self.finding_access_count) == expected_len):
                    raise RuntimeError("Parallel array lengths diverged during add_finding")

            except Exception as e:
                # FIX #1: Rollback on failure - restore arrays to original length
                self.key_findings = self.key_findings[:original_len]
                self.finding_embeddings = self.finding_embeddings[:original_len]
                self.finding_importance = self.finding_importance[:original_len]
                self.finding_last_accessed = self.finding_last_accessed[:original_len]
                self.finding_access_count = self.finding_access_count[:original_len]
                logger.error(f"add_finding failed, rolled back: {e}")
                raise

    def calculate_importance(
        self,
        finding_idx: int,
        query_embedding: Optional[List[float]] = None,
        alpha: float = 0.4,
        beta: float = 0.4,
        gamma: float = 0.2
    ) -> float:
        """Calculate importance score per H-MEM paper formula (Section 3.3).

        Formula: S_importance = α·S_recency + β·S_relevance + γ·S_frequency

        Args:
            finding_idx: Index of finding to score
            query_embedding: Optional query vector for relevance calculation
            alpha: Weight for recency (default: 0.4)
            beta: Weight for relevance (default: 0.4)
            gamma: Weight for frequency (default: 0.2)

        Returns:
            Importance score between 0 and 1
        """
        import numpy as np

        # Validate index
        if finding_idx < 0 or finding_idx >= len(self.key_findings):
            return 0.0

        # 1. Recency score (0-1, newer = higher)
        if finding_idx < len(self.finding_last_accessed):
            days_old = (datetime.now() - self.finding_last_accessed[finding_idx]).days
            s_recency = 1.0 / (1.0 + days_old)
        else:
            s_recency = 0.5  # Default if no timestamp

        # 2. Relevance score (0-1, cosine similarity)
        if (query_embedding and
            finding_idx < len(self.finding_embeddings) and
            self.finding_embeddings[finding_idx]):

            # Cosine similarity
            emb = self.finding_embeddings[finding_idx]
            dot_product = np.dot(query_embedding, emb)
            norm_query = np.linalg.norm(query_embedding)
            norm_emb = np.linalg.norm(emb)

            if norm_query > 0 and norm_emb > 0:
                s_relevance = dot_product / (norm_query * norm_emb)
                s_relevance = max(0.0, min(1.0, s_relevance))  # Clamp to [0, 1]
            else:
                s_relevance = 0.5
        else:
            s_relevance = 0.5  # Neutral if no query

        # 3. Frequency score (0-1, normalized access count)
        if finding_idx < len(self.finding_access_count):
            max_access = max(self.finding_access_count) if self.finding_access_count else 1
            s_frequency = self.finding_access_count[finding_idx] / max(max_access, 1)
        else:
            s_frequency = 0.0

        # Combined importance score
        importance = alpha * s_recency + beta * s_relevance + gamma * s_frequency

        # FIX #10: Strict bounds checking instead of silent extension
        # Silent extension masks index errors - fail fast instead
        if finding_idx < len(self.finding_importance):
            self.finding_importance[finding_idx] = importance
        else:
            # Log error instead of silently extending
            logger.error(
                f"calculate_importance: finding_idx {finding_idx} out of bounds "
                f"(importance array len={len(self.finding_importance)}). "
                f"This indicates parallel arrays are out of sync."
            )
            # Don't extend - this is a bug that should be fixed at the source

        return importance

    def update_access(self, finding_idx: int) -> None:
        """Update access tracking when finding is retrieved (H-MEM Section 3.3).

        FIX #10: Strict bounds checking - no silent array extension.

        Args:
            finding_idx: Index of finding that was accessed
        """
        if finding_idx < 0 or finding_idx >= len(self.key_findings):
            return

        # FIX #10: Validate arrays are in sync before updating
        if finding_idx >= len(self.finding_last_accessed):
            logger.error(
                f"update_access: finding_idx {finding_idx} out of bounds for "
                f"finding_last_accessed (len={len(self.finding_last_accessed)})"
            )
            return

        if finding_idx >= len(self.finding_access_count):
            logger.error(
                f"update_access: finding_idx {finding_idx} out of bounds for "
                f"finding_access_count (len={len(self.finding_access_count)})"
            )
            return

        # Update last accessed timestamp
        self.finding_last_accessed[finding_idx] = datetime.now()

        # Increment access count
        self.finding_access_count[finding_idx] += 1

    def validate_parallel_arrays(self) -> tuple[bool, list[str]]:
        """Validate all parallel arrays are in sync.

        FIX (Jan 2026): Comprehensive validation of parallel array integrity.

        Returns:
            Tuple of (is_valid, list_of_errors)
        """
        errors = []
        expected_len = len(self.key_findings)

        arrays = [
            ('finding_embeddings', self.finding_embeddings),
            ('finding_importance', self.finding_importance),
            ('finding_last_accessed', self.finding_last_accessed),
            ('finding_access_count', self.finding_access_count),
        ]

        for name, arr in arrays:
            if len(arr) != expected_len:
                errors.append(
                    f"{name}: expected {expected_len}, got {len(arr)}"
                )

        # Validate sub_memory_indices
        for idx in self.sub_memory_indices:
            if idx < 0 or idx >= expected_len:
                errors.append(f"sub_memory_indices: invalid index {idx}")

        if errors:
            logger.warning(f"Parallel array validation failed for phase '{self.phase_name}': {errors}")

        return len(errors) == 0, errors

    def repair_parallel_arrays(self) -> int:
        """Repair misaligned parallel arrays.

        FIX (Jan 2026): Automatic repair of parallel array inconsistencies.
        Extends short arrays with safe defaults, truncates long arrays.

        Returns:
            Number of repairs made
        """
        repairs = 0
        target_len = len(self.key_findings)

        # Extend short arrays with defaults
        while len(self.finding_embeddings) < target_len:
            self.finding_embeddings.append([])
            repairs += 1

        while len(self.finding_importance) < target_len:
            self.finding_importance.append(0.5)  # Default importance
            repairs += 1

        while len(self.finding_last_accessed) < target_len:
            self.finding_last_accessed.append(datetime.now())
            repairs += 1

        while len(self.finding_access_count) < target_len:
            self.finding_access_count.append(0)
            repairs += 1

        # Truncate long arrays (should not happen, but handle it)
        if len(self.finding_embeddings) > target_len:
            repairs += len(self.finding_embeddings) - target_len
            self.finding_embeddings = self.finding_embeddings[:target_len]

        if len(self.finding_importance) > target_len:
            repairs += len(self.finding_importance) - target_len
            self.finding_importance = self.finding_importance[:target_len]

        if len(self.finding_last_accessed) > target_len:
            repairs += len(self.finding_last_accessed) - target_len
            self.finding_last_accessed = self.finding_last_accessed[:target_len]

        if len(self.finding_access_count) > target_len:
            repairs += len(self.finding_access_count) - target_len
            self.finding_access_count = self.finding_access_count[:target_len]

        # Fix invalid sub_memory_indices
        valid_indices = [i for i in self.sub_memory_indices if 0 <= i < target_len]
        if len(valid_indices) != len(self.sub_memory_indices):
            repairs += len(self.sub_memory_indices) - len(valid_indices)
            self.sub_memory_indices = valid_indices

        if repairs > 0:
            logger.info(f"Repaired {repairs} parallel array issues in phase '{self.phase_name}'")

        return repairs

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary with ISO timestamps."""
        return {
            "phase_name": self.phase_name,
            "started_step": self.started_step,
            "ended_step": self.ended_step,
            "summary": self.summary,
            "phase_embedding": self.phase_embedding,  # NEW: Layer 2 embedding
            "key_findings": self.key_findings,
            "finding_embeddings": self.finding_embeddings,
            "sub_memory_indices": self.sub_memory_indices,
            # NEW: Importance tracking (H-MEM Section 3.3)
            "finding_importance": self.finding_importance,
            "finding_last_accessed": [
                ts.isoformat() for ts in self.finding_last_accessed
            ],
            "finding_access_count": self.finding_access_count,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None
        }


class HierarchicalMemory(BaseModel):
    """3-layer H-MEM hierarchical memory.

    Layer 1: Session Summary (this level)
        - Task description, progress, current phase
        - Always in context (~100 tokens)

    Layer 2: Phase Memories (list with position indices)
        - One PhaseMemory per phase (discovery, collection, etc.)
        - Position-based access enables efficient hierarchical navigation
        - Only current phase context injected (~300-400 tokens)

    Layer 3: Recent Steps (rolling window)
        - Last 100 steps with phase tags (increased from 20 for long tasks)
        - Most recent 50 in context (~5,000 tokens)

    Total context: ~4,000-6,000 tokens adaptive (scaled for 200-1000 step tasks)

    Attributes:
        session_id: Unique session identifier
        task: Task description
        current_phase: Current active phase name
        progress: Progress string (e.g., "25/50")
        phases_completed: List of completed phase names
        phase_memories: List of phase memories (position-indexed for H-MEM)
        phase_index: Mapping from phase name to position (backward compatibility)
        recent_steps: Recent steps (max 100, rolling window)
        created_at: Session creation time
        updated_at: Last update time
    """
    model_config = ConfigDict(extra='forbid')

    session_id: str = Field(..., description="Unique session ID")
    task: str = Field(..., description="Task description")
    current_phase: str = Field(default="discovery", description="Current active phase")
    progress: str = Field(default="0/?", description="Progress string like '25/50'")
    phases_completed: List[str] = Field(default_factory=list, description="Completed phases")

    # Layer 2: Phase list (H-MEM position indices!)
    phase_memories: List[PhaseMemory] = Field(
        default_factory=list,
        description="Phase memories as list (position-indexed for efficient retrieval)"
    )

    # Backward compatibility: Map phase name → position index
    phase_index: Dict[str, int] = Field(
        default_factory=dict,
        description="Maps phase name to position in phase_memories list"
    )

    # Layer 3: Recent steps (rolling window)
    recent_steps: List[Step] = Field(
        default_factory=list,
        description="Recent steps (max 20)"
    )
    
    # Loop detection metrics (informational only, no intervention)
    consecutive_rejections: int = Field(
        default=0,
        description="Count of consecutive finding rejections"
    )
    rejection_threshold: int = Field(
        default=10,
        description="Threshold for loop signal logging"
    )

    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    def get_current_phase_memory(self) -> Optional[PhaseMemory]:
        """Get memory for current active phase (position-indexed access).

        Returns:
            PhaseMemory if current phase exists, None otherwise
        """
        # Use phase_index to get position, then access list
        if self.current_phase in self.phase_index:
            idx = self.phase_index[self.current_phase]
            return self.phase_memories[idx]
        return None

    def get_phase_by_name(self, phase_name: str) -> Optional[PhaseMemory]:
        """Get phase memory by name using position index.

        Args:
            phase_name: Name of phase to retrieve

        Returns:
            PhaseMemory if found, None otherwise
        """
        if phase_name in self.phase_index:
            idx = self.phase_index[phase_name]
            return self.phase_memories[idx]
        return None

    def get_phase_position(self, phase_name: str) -> Optional[int]:
        """Get position index for a phase name.

        Args:
            phase_name: Phase name to look up

        Returns:
            Position index, or None if not found
        """
        return self.phase_index.get(phase_name)

    def add_step(self, step: Step, max_steps: int = 100) -> None:
        """Add a step to recent steps (rolling window).
        
        PHASE 1 FIX (Nov 4, 2025): Increased from 20 to 100 steps
        
        Rationale:
        - Tasks can run 200-1000+ steps
        - Previous 20-step window covered only 2-10% of task
        - 100 steps = ~10-50% coverage, reasonable memory footprint
        - With 1M context: 100 steps × 100 tokens avg = 10K tokens (1% of window)
        
        Rolling Window Strategy:
        - Keep most recent 100 steps verbatim
        - Older steps summarized in phase findings
        - Phase summaries + recent steps = complete picture

        Args:
            step: Step to add
            max_steps: Maximum steps to keep (default 100, was 20)
        """
        self.recent_steps.append(step)

        # Maintain rolling window
        if len(self.recent_steps) > max_steps:
            self.recent_steps.pop(0)  # Remove oldest (LRU)

        self.updated_at = datetime.now()

    def start_or_resume_phase(self, phase_name: str, start_step: int) -> PhaseMemory:
        """Start a new phase OR resume existing phase (FIXED for H-MEM semantic grouping).

        H-MEM phases are semantic categories that can be revisited multiple times.
        Don't create duplicates - reuse existing PhaseMemory if it exists.

        This fixes the critical bug where agent transitions like:
        collection → discovery → collection would create TWO collection phases,
        fragmenting memory and orphaning earlier findings.

        Args:
            phase_name: Name of the phase to start/resume
            start_step: Step number where (re)starting

        Returns:
            PhaseMemory (new or existing)
        """
        # Check if phase already exists
        existing_idx = self.phase_index.get(phase_name)

        if existing_idx is not None and existing_idx < len(self.phase_memories):
            # RESUME existing phase
            phase_memory = self.phase_memories[existing_idx]

            # Reactivate if was completed
            if phase_memory.status == "completed":
                phase_memory.status = "active"
                phase_memory.ended_step = None  # Clear end marker
                logger.info(
                    f"♻️  Resumed existing phase '{phase_name}' at step {start_step} "
                    f"(position {existing_idx}, originally started at step {phase_memory.started_step})"
                )
            else:
                logger.debug(
                    f"↪️  Continuing active phase '{phase_name}' at step {start_step} "
                    f"(position {existing_idx})"
                )

            self.current_phase = phase_name
            self.updated_at = datetime.now()
            return phase_memory

        else:
            # CREATE new phase (first time seeing this phase_name)
            phase_memory = PhaseMemory(
                phase_name=phase_name,
                started_step=start_step,
                status="active"
            )

            # Append to list and record position in index
            position = len(self.phase_memories)
            self.phase_memories.append(phase_memory)
            self.phase_index[phase_name] = position

            self.current_phase = phase_name
            self.updated_at = datetime.now()

            logger.info(
                f"🆕 Started NEW phase '{phase_name}' at step {start_step} (position {position})"
            )
            return phase_memory

    def complete_phase(self, phase_name: str, end_step: int, summary: Optional[str] = None) -> None:
        """Complete a phase (position-indexed access).

        Args:
            phase_name: Name of phase to complete
            end_step: Final step number
            summary: Optional summary override
        """
        if phase_name in self.phase_index:
            idx = self.phase_index[phase_name]
            self.phase_memories[idx].finalize(end_step, summary)
            if phase_name not in self.phases_completed:
                self.phases_completed.append(phase_name)
            self.updated_at = datetime.now()

            logger.info(f"Completed phase '{phase_name}' at step {end_step}")

    def validate_no_duplicate_phases(self) -> bool:
        """Validate no duplicate phase names exist in phase_memories list.

        This detects the critical bug where the same phase name appears multiple times,
        causing memory fragmentation and context loss.

        Returns:
            True if no duplicates, False if duplicates found
        """
        phase_names = [p.phase_name for p in self.phase_memories]
        seen = set()
        duplicates = []

        for name in phase_names:
            if name in seen:
                duplicates.append(name)
            seen.add(name)

        if duplicates:
            logger.error(f"🚨 DUPLICATE PHASES DETECTED: {duplicates}")
            logger.error(f"   Phase list: {phase_names}")
            logger.error(f"   Phase index: {self.phase_index}")
            return False

        return True

    def add_finding_to_phase(
        self,
        phase_name: str,
        finding: str,
        embedding: Optional[List[float]] = None,
        similarity_threshold: float = 0.85
    ) -> bool:
        """Add a finding to a specific phase with intelligent deduplication.

        FIX #9 (Nov 26, 2025): Lowered threshold from 0.92 to 0.85
        - 0.92 was too strict, rejecting semantically different findings with similar wording
        - 0.85 provides better balance: rejects true duplicates, accepts varied findings
        - Example: "Found 5 researchers" vs "Found 3 researchers" now both kept

        Deduplication Strategy:
        - Exact text match: Always reject (100% duplicate)
        - High semantic similarity (>85%): Reject near-duplicates
        - Moderate similarity (70-85%): Allow - these are related but distinct

        Args:
            phase_name: Phase to add finding to
            finding: Finding to add
            embedding: Optional vector embedding for semantic search
            similarity_threshold: Cosine similarity threshold (default 0.85)

        Returns:
            True if finding was added, False if duplicate was rejected
        """
        if phase_name not in self.phase_index:
            logger.warning(f"Cannot add finding to unknown phase: {phase_name}")
            return False

        idx = self.phase_index[phase_name]
        phase_memory = self.phase_memories[idx]

        # DEDUPLICATION CHECK 1: Exact Text Match (100% duplicate)
        if finding in phase_memory.key_findings:
            logger.debug(f"❌ Rejected exact duplicate finding: {finding[:80]}...")
            self.consecutive_rejections += 1
            if self.consecutive_rejections >= self.rejection_threshold:
                logger.info(f"📊 H-MEM: {self.consecutive_rejections} consecutive rejections")
            return False

        # DEDUPLICATION CHECK 2: Semantic Similarity (near-duplicate)
        if embedding and len(embedding) > 0 and phase_memory.finding_embeddings:
            import numpy as np

            query_emb = np.array(embedding)
            query_norm = np.linalg.norm(query_emb)

            if query_norm > 0:  # Valid embedding
                for existing_idx, existing_emb in enumerate(phase_memory.finding_embeddings):
                    if existing_emb and len(existing_emb) > 0:
                        existing_emb_np = np.array(existing_emb)
                        existing_norm = np.linalg.norm(existing_emb_np)

                        if existing_norm > 0:
                            # Cosine similarity: dot(A,B) / (||A|| * ||B||)
                            dot_product = np.dot(query_emb, existing_emb_np)
                            similarity = dot_product / (query_norm * existing_norm)

                            if similarity >= similarity_threshold:
                                existing_finding = phase_memory.key_findings[existing_idx]
                                logger.info(
                                    f"❌ Rejected semantically similar finding ({similarity:.1%} match):\n"
                                    f"   New: {finding[:80]}...\n"
                                    f"   Existing: {existing_finding[:80]}..."
                                )
                                self.consecutive_rejections += 1
                                if self.consecutive_rejections >= self.rejection_threshold:
                                    logger.info(f"📊 H-MEM: {self.consecutive_rejections} semantic rejections")
                                return False

        # Threat scan (opt-in): reject obviously-injected findings before they become
        # recallable memory. Default off => no behaviour change.
        from core.env import bool_env as _bool_env
        if _bool_env("MEMORY_THREAT_SCAN", False):
            from modules.memory.task.threat_scan import is_suspicious
            if is_suspicious(finding):
                logger.warning(f"🛡️ Rejected suspicious finding (threat scan): {finding[:80]}...")
                return False

        # Not a duplicate - add it
        self.consecutive_rejections = 0
        finding_idx = len(phase_memory.key_findings)
        phase_memory.add_finding(finding, embedding)
        logger.debug(f"Reset consecutive_rejections counter after successful finding add")

        # Update sub_memory_indices to point to this finding (for H-MEM routing)
        if embedding and len(embedding) > 0:
            phase_memory.sub_memory_indices.append(finding_idx)

        self.updated_at = datetime.now()
        logger.debug(f"✅ Added new finding to phase '{phase_name}': {finding[:80]}...")
        return True

    def is_showing_loop_signal(self) -> bool:
        """Check if consecutive rejections exceed threshold."""
        return self.consecutive_rejections >= self.rejection_threshold
    
    def get_loop_signal_info(self) -> Optional[str]:
        """Get loop signal info if active (informational only)."""
        if not self.is_showing_loop_signal():
            return None
        return f"{self.consecutive_rejections} consecutive finding rejections"
    
    def reset_loop_signal(self) -> None:
        """Reset the loop signal counter."""
        self.consecutive_rejections = 0
        logger.debug("Reset H-MEM loop signal counter")

    def get_all_findings_with_embeddings(self) -> Dict[str, List[str]]:
        """Get all findings from all phases for semantic search (position-indexed).

        Returns:
            Dictionary of {phase_name: [finding1, finding2, ...]}
            Only includes findings that have embeddings
        """
        findings_dict = {}
        # Iterate over list instead of dict
        for phase_memory in self.phase_memories:
            # Only include findings with valid embeddings
            findings_with_embeddings = []
            for i, finding in enumerate(phase_memory.key_findings):
                # Check if embedding exists and is not empty
                if (i < len(phase_memory.finding_embeddings) and
                    phase_memory.finding_embeddings[i]):
                    findings_with_embeddings.append(finding)

            if findings_with_embeddings:
                findings_dict[phase_memory.phase_name] = findings_with_embeddings

        return findings_dict

    def get_context_summary(self, include_recent: int = 5) -> str:
        """Get a context summary for injection.

        This is a basic summary - ContextRetriever provides the full
        formatted version with better control.

        Args:
            include_recent: Number of recent steps to include

        Returns:
            Formatted context string
        """
        lines = [
            f"Session: {self.session_id}",
            f"Task: {self.task}",
            f"Progress: {self.progress}",
            f"Current Phase: {self.current_phase}",
            ""
        ]

        # Current phase info
        current = self.get_current_phase_memory()
        if current:
            lines.append(f"Phase: {current.phase_name}")
            if current.summary:
                lines.append(f"Summary: {current.summary}")
            if current.key_findings:
                lines.append(f"Findings ({len(current.key_findings)}):")
                for finding in current.key_findings[-5:]:  # Last 5
                    lines.append(f"  - {finding}")
            lines.append("")

        # Completed phases
        if self.phases_completed:
            lines.append("Completed Phases:")
            for phase in self.phases_completed:
                if phase in self.phase_index:
                    idx = self.phase_index[phase]
                    pm = self.phase_memories[idx]
                    lines.append(f"  - {phase}: {pm.summary}")
            lines.append("")

        # Recent steps
        if self.recent_steps and include_recent > 0:
            recent = self.recent_steps[-include_recent:]
            lines.append(f"Recent Steps ({len(recent)}):")
            for step in recent:
                lines.append(f"  [{step.step}] {step.phase}: {step.action_summary}")
                if step.finding:
                    lines.append(f"      → {step.finding}")

        return "\n".join(lines)

    def save(self, path: Path) -> None:
        """Save to JSON file (position-indexed format).

        Args:
            path: Path to save to
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "session_id": self.session_id,
            "task": self.task,
            "current_phase": self.current_phase,
            "progress": self.progress,
            "phases_completed": self.phases_completed,
            # Save as list (position-indexed)
            "phase_memories": [
                memory.to_dict()
                for memory in self.phase_memories
            ],
            # Save index mapping for fast reconstruction
            "phase_index": self.phase_index,
            "recent_steps": [step.to_dict() for step in self.recent_steps],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat()
        }

        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

        logger.debug(f"Saved hierarchical memory to {path} ({len(self.phase_memories)} phases)")

    @classmethod
    def load(cls, path: Path) -> 'HierarchicalMemory':
        """Load from JSON file (supports old dict and new list formats).

        Backward-compatible: old JSON files that carry "domains", "domain_index",
        or "current_domain" keys are silently ignored.

        Args:
            path: Path to load from

        Returns:
            Loaded HierarchicalMemory

        Raises:
            FileNotFoundError: If file doesn't exist
            ValueError: If file is invalid
        """
        if not path.exists():
            raise FileNotFoundError(f"Hierarchical memory file not found: {path}")

        with open(path, 'r') as f:
            data = json.load(f)

        # Parse phase memories - handle both old dict and new list formats
        phase_memories_data = data.get("phase_memories", {})
        phase_memories = []
        phase_index = {}

        if isinstance(phase_memories_data, dict):
            # OLD FORMAT: Dict[str, PhaseMemory] - convert to list
            logger.info("Converting old dict-based memory to position-indexed format")
            for idx, (name, pm_data) in enumerate(phase_memories_data.items()):
                # Handle importance tracking fields with defaults (backward compatibility)
                finding_count = len(pm_data.get("key_findings", []))
                phase_memory = PhaseMemory(
                    phase_name=pm_data["phase_name"],
                    started_step=pm_data["started_step"],
                    ended_step=pm_data.get("ended_step"),
                    summary=pm_data.get("summary", ""),
                    phase_embedding=pm_data.get("phase_embedding", []),  # NEW
                    key_findings=pm_data.get("key_findings", []),
                    finding_embeddings=pm_data.get("finding_embeddings", []),
                    sub_memory_indices=pm_data.get("sub_memory_indices", []),
                    # NEW: Importance tracking (H-MEM Section 3.3)
                    finding_importance=pm_data.get("finding_importance", [1.0] * finding_count),
                    finding_last_accessed=pm_data.get("finding_last_accessed", [datetime.now()] * finding_count),
                    finding_access_count=pm_data.get("finding_access_count", [0] * finding_count),
                    status=pm_data.get("status", "active"),
                    created_at=datetime.fromisoformat(pm_data["created_at"]),
                    completed_at=datetime.fromisoformat(pm_data["completed_at"]) if pm_data.get("completed_at") else None
                )
                phase_memories.append(phase_memory)
                phase_index[name] = idx

        elif isinstance(phase_memories_data, list):
            # NEW FORMAT: List[PhaseMemory] - use as is
            for idx, pm_data in enumerate(phase_memories_data):
                # Handle importance tracking fields with defaults (backward compatibility)
                finding_count = len(pm_data.get("key_findings", []))

                # Parse finding_last_accessed timestamps
                finding_last_accessed = pm_data.get("finding_last_accessed", [])
                if finding_last_accessed and isinstance(finding_last_accessed[0], str):
                    finding_last_accessed = [datetime.fromisoformat(ts) for ts in finding_last_accessed]
                elif not finding_last_accessed:
                    finding_last_accessed = [datetime.now()] * finding_count

                phase_memory = PhaseMemory(
                    phase_name=pm_data["phase_name"],
                    started_step=pm_data["started_step"],
                    ended_step=pm_data.get("ended_step"),
                    summary=pm_data.get("summary", ""),
                    phase_embedding=pm_data.get("phase_embedding", []),  # NEW
                    key_findings=pm_data.get("key_findings", []),
                    finding_embeddings=pm_data.get("finding_embeddings", []),
                    sub_memory_indices=pm_data.get("sub_memory_indices", []),
                    # NEW: Importance tracking (H-MEM Section 3.3)
                    finding_importance=pm_data.get("finding_importance", [1.0] * finding_count),
                    finding_last_accessed=finding_last_accessed,
                    finding_access_count=pm_data.get("finding_access_count", [0] * finding_count),
                    status=pm_data.get("status", "active"),
                    created_at=datetime.fromisoformat(pm_data["created_at"]),
                    completed_at=datetime.fromisoformat(pm_data["completed_at"]) if pm_data.get("completed_at") else None
                )
                phase_memories.append(phase_memory)
                phase_index[pm_data["phase_name"]] = idx

            # Use saved index if available (optimization)
            if "phase_index" in data:
                phase_index = data["phase_index"]

        # Parse recent steps
        recent_steps = []
        for step_data in data.get("recent_steps", []):
            recent_steps.append(Step(
                step=step_data["step"],
                phase=step_data["phase"],
                action_summary=step_data["action_summary"],
                finding=step_data.get("finding"),
                step_embedding=step_data.get("step_embedding", []),  # NEW: Layer 4 embedding
                timestamp=datetime.fromisoformat(step_data["timestamp"])
            ))

        memory = cls(
            session_id=data["session_id"],
            task=data["task"],
            current_phase=data.get("current_phase", "discovery"),
            progress=data.get("progress", "0/?"),
            phases_completed=data.get("phases_completed", []),
            phase_memories=phase_memories,
            phase_index=phase_index,
            recent_steps=recent_steps,
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"])
        )

        logger.debug(f"Loaded hierarchical memory from {path} ({len(phase_memories)} phases)")
        return memory
