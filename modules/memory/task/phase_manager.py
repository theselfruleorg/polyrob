"""Phase manager for detecting transitions and grouping memories by phase.

The PhaseManager handles the lifecycle of phases in a task agent session:
    - Detects when agent transitions between phases
    - Creates new PhaseMemory objects for new phases
    - Adds findings to current phase
    - Finalizes completed phases with summaries

Key Insight:
    Agent's brain_state already contains phase information. We just need to
    track it and create phase boundaries when it changes. No complex ML needed!

Typical Phase Flow:
    discovery → collection → processing → documentation

OPTIMIZATION (Nov 14, 2025): Smart phase intelligence to prevent fragmentation
    - Transition history tracking
    - Oscillation detection
    - Progress monitoring
    - Intelligent phase resumption validation
"""

import logging
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
from .hierarchical_memory import HierarchicalMemory, PhaseMemory, Step

logger = logging.getLogger(__name__)


class PhaseTransition:
    """Represents a single phase transition for tracking."""
    def __init__(self, from_phase: str, to_phase: str, step: int):
        self.from_phase = from_phase
        self.to_phase = to_phase
        self.step = step
        self.timestamp = datetime.now()


class PhaseManager:
    """Manages phase transitions and memory grouping with 199 IQ intelligence.

    The PhaseManager watches for phase changes in brain_state and automatically
    creates/finalizes phase memories. This provides natural grouping of memories
    by the agent's workflow phases.
    
    OPTIMIZATION (Nov 14, 2025): Added smart phase transition detection
        - Prevents rapid oscillation between phases
        - Detects suspicious transitions
        - Tracks progress within phases
        - Forces intelligent resumption over fragmentation

    Attributes:
        memory: HierarchicalMemory instance to manage
        previous_phase: Last seen phase name
        semantic_retriever: Optional SemanticRetriever for embedding findings
        _phase_transition_history: Recent transitions for oscillation detection
        _steps_in_current_phase: Counter for progress tracking
        _findings_in_current_phase: Counter for productivity tracking
        _min_steps_before_transition: Minimum steps before allowing phase change
    """

    def __init__(self, memory: HierarchicalMemory, semantic_retriever: Optional[Any] = None):
        """Initialize phase manager with smart transition detection.

        Args:
            memory: HierarchicalMemory instance to manage
            semantic_retriever: Optional SemanticRetriever for embedding findings
        """
        self.memory = memory
        self.previous_phase = memory.current_phase
        self.semantic_retriever = semantic_retriever
        
        # OPTIMIZATION: Smart phase transition tracking
        self._phase_transition_history: List[PhaseTransition] = []
        self._steps_in_current_phase = 0
        self._findings_in_current_phase = 0
        self._min_steps_before_transition = 3  # Require at least 3 steps before allowing transition
        self._oscillation_window = 10  # Look back 10 transitions for oscillation
        
        logger.debug(
            f"PhaseManager initialized for session {memory.session_id} "
            f"(semantic_retriever={'enabled' if semantic_retriever else 'disabled'}, "
            f"smart_transitions=enabled)"
        )

    def add_step(
        self,
        step_number: int,
        brain_state: Dict[str, Any],
        action_summary: str,
        finding: Optional[str] = None
    ) -> Step:
        """Add a step and detect phase transitions with intelligent validation.

        OPTIMIZATION (Nov 14, 2025): Smart phase transition detection
            - Prevents rapid oscillation between phases
            - Requires minimum steps before transition
            - Detects and prevents suspicious phase loops
            - Tracks progress and productivity in each phase

        This is the main entry point. Call this after each agent step to:
        1. Extract phase from brain_state
        2. Validate phase transition (NEW: smart detection)
        3. Detect if phase changed
        4. Finalize old phase if changed
        5. Start new phase if needed
        6. Add finding to current phase
        7. Add step to recent steps

        Args:
            step_number: Current step number
            brain_state: Brain state from agent (contains 'phase' field)
            action_summary: Summary of action taken
            finding: Optional finding from this step

        Returns:
            Created Step object
        """
        # Extract phase from brain_state (default to 'discovery' if None or missing)
        requested_phase = brain_state.get("phase") or "discovery"

        # OPTIMIZATION: Smart phase transition validation
        current_phase = self._validate_phase_transition(
            requested_phase=requested_phase,
            step_number=step_number,
            finding=finding
        )

        # Update step counter for current phase
        if current_phase == self.previous_phase:
            self._steps_in_current_phase += 1
        else:
            # Phase actually changed (after validation)
            self._steps_in_current_phase = 1
            self._findings_in_current_phase = 0

        # Track finding count for productivity monitoring
        if finding:
            self._findings_in_current_phase += 1

        # Detect phase transition
        if current_phase != self.previous_phase:
            # Record transition in history
            transition = PhaseTransition(
                from_phase=self.previous_phase,
                to_phase=current_phase,
                step=step_number
            )
            self._phase_transition_history.append(transition)
            
            # Keep only recent transitions (for oscillation detection)
            if len(self._phase_transition_history) > 20:
                self._phase_transition_history = self._phase_transition_history[-20:]

            self._handle_phase_transition(
                old_phase=self.previous_phase,
                new_phase=current_phase,
                transition_step=step_number
            )

        # Ensure current phase exists (use start_or_resume_phase to prevent duplicates)
        if current_phase not in self.memory.phase_index:
            self.memory.start_or_resume_phase(current_phase, step_number)

        # OPTIMIZATION: Check for stall (many steps without findings)
        if self._steps_in_current_phase > 50 and self._findings_in_current_phase == 0:
            logger.warning(
                f"⚠️ POTENTIAL STALL: {self._steps_in_current_phase} steps in "
                f"phase '{current_phase}' without any findings. Agent may be stuck."
            )

        # Add finding to current phase with embedding
        if finding:
            embedding = None
            if self.semantic_retriever:
                try:
                    # Embed the finding for semantic search
                    embedding_array = self.semantic_retriever.embed_text(finding)
                    if embedding_array is not None:
                        # Convert numpy array to list for JSON serialization
                        embedding = embedding_array.tolist()
                except Exception as e:
                    logger.warning(f"⚠️ Failed to embed finding: {e}")
                    # Continue without embedding

            # LOW-7: surface rejections (dedup or opt-in threat-scan) so dropped findings
            # are observable instead of being silently swallowed by the discarded bool.
            if self.memory.add_finding_to_phase(current_phase, finding, embedding) is False:
                logger.info(f"ℹ️ Finding not added to '{current_phase}' (rejected): {str(finding)[:80]}")

        # Create and add step with embedding (H-MEM Layer 4)
        step_embedding = []
        if self.semantic_retriever:
            try:
                # Embed the action summary for step-level semantic search
                embedding_array = self.semantic_retriever.embed_text(action_summary)
                if embedding_array is not None:
                    step_embedding = embedding_array.tolist()
            except Exception as e:
                logger.warning(f"⚠️ Failed to embed step action: {e}")

        step = Step(
            step=step_number,
            phase=current_phase,
            action_summary=action_summary,
            finding=finding,
            step_embedding=step_embedding
        )
        self.memory.add_step(step)

        # Update previous phase
        self.previous_phase = current_phase

        logger.debug(
            f"Added step {step_number} to phase '{current_phase}' "
            f"(steps_in_phase: {self._steps_in_current_phase}, "
            f"findings: {self._findings_in_current_phase})"
        )
        return step
    
    def _validate_phase_transition(
        self,
        requested_phase: str,
        step_number: int,
        finding: Optional[str]
    ) -> str:
        """Validate requested phase transition using smart detection.

        OPTIMIZATION (Nov 14, 2025): 199 IQ phase intelligence
            - Prevents rapid phase oscillation
            - Requires minimum productivity before transition
            - Detects suspicious transition patterns
            - Forces phase resumption over fragmentation
        
        FIX #6 (Dec 2, 2025): Allow transitions when loop signals detected
            - If H-MEM indicates agent is stuck, allow phase changes
            - Loop recovery takes precedence over anti-oscillation rules

        Args:
            requested_phase: Phase requested by agent's brain_state
            step_number: Current step number
            finding: Finding from current step (if any)

        Returns:
            Validated phase (may differ from requested_phase)
        """
        # If same phase, no validation needed
        if requested_phase == self.previous_phase:
            return requested_phase

        # FIX #6: Check if H-MEM is signaling a loop - if so, allow transition
        # Loop recovery takes precedence over anti-oscillation rules
        if self.memory.is_showing_loop_signal():
            logger.info(
                f"🔄 H-MEM loop signal active - allowing phase transition "
                f"({self.previous_phase} → {requested_phase}) to help agent recover."
            )
            # FIX #5: Reset loop signal after allowing transition
            # Prevents the signal from persisting indefinitely
            self.memory.reset_loop_signal()
            return requested_phase

        # RULE 1: Minimum steps before transition
        if self._steps_in_current_phase < self._min_steps_before_transition:
            logger.info(
                f"🛡️ Prevented premature phase transition after only "
                f"{self._steps_in_current_phase} steps: "
                f"{self.previous_phase} → {requested_phase}. "
                f"Staying in {self.previous_phase}."
            )
            return self.previous_phase

        # RULE 2: Detect phase oscillation (A→B→A→B→A...)
        if self._detect_phase_oscillation(requested_phase):
            logger.warning(
                f"🛡️ Detected phase oscillation pattern. "
                f"Preventing transition to {requested_phase}. "
                f"Staying in {self.previous_phase}."
            )
            return self.previous_phase

        # RULE 3: Prevent transition to recently visited phase without progress
        if self._is_premature_revisit(requested_phase):
            logger.warning(
                f"🛡️ Premature revisit to phase '{requested_phase}' "
                f"without sufficient progress. Staying in {self.previous_phase}."
            )
            return self.previous_phase

        # Transition validated - allow it
        logger.info(
            f"✅ Phase transition validated: {self.previous_phase} → {requested_phase} "
            f"(after {self._steps_in_current_phase} steps, "
            f"{self._findings_in_current_phase} findings)"
        )
        return requested_phase

    def _detect_phase_oscillation(self, requested_phase: str) -> bool:
        """Detect if agent is oscillating between phases.

        Oscillation pattern: A→B→A→B or A→B→C→A→B→C (rapid cycling)

        Args:
            requested_phase: Phase being requested

        Returns:
            True if oscillation detected, False otherwise
        """
        if len(self._phase_transition_history) < 4:
            # Not enough history
            return False

        # Get last N transitions
        recent = self._phase_transition_history[-self._oscillation_window:]

        # Count transitions to/from requested phase
        to_requested = sum(
            1 for t in recent if t.to_phase == requested_phase
        )
        from_requested = sum(
            1 for t in recent if t.from_phase == requested_phase
        )

        # If we've visited this phase multiple times recently, it's oscillation
        if to_requested >= 3:
            logger.debug(
                f"Oscillation detected: {to_requested} transitions to "
                f"'{requested_phase}' in last {len(recent)} transitions"
            )
            return True

        # Check for A→B→A pattern in last 3 transitions
        if len(recent) >= 3:
            last_three_to = [t.to_phase for t in recent[-3:]]
            # Pattern: [A, B, A] or similar
            if last_three_to[0] == last_three_to[2] and last_three_to[0] == requested_phase:
                logger.debug(
                    f"Oscillation pattern detected: "
                    f"{last_three_to[0]} → {last_three_to[1]} → {last_three_to[2]}"
                )
                return True

        return False

    def _is_premature_revisit(self, requested_phase: str) -> bool:
        """Check if revisiting a phase too soon without progress.

        Args:
            requested_phase: Phase being requested

        Returns:
            True if premature revisit, False otherwise
        """
        # Check if this phase exists
        if requested_phase not in self.memory.phase_index:
            # New phase, not a revisit
            return False

        # Get the phase memory
        phase_memory = self.memory.get_phase_by_name(requested_phase)
        if not phase_memory:
            return False

        # If phase was just active (in last 10 steps), check for progress
        recent_transitions = self._phase_transition_history[-5:]
        phase_recently_active = any(
            t.from_phase == requested_phase or t.to_phase == requested_phase
            for t in recent_transitions
        )

        if phase_recently_active:
            # Check if we made progress (added findings)
            if self._findings_in_current_phase < 2:
                # Minimal progress, prevent premature revisit
                logger.debug(
                    f"Premature revisit detected: phase '{requested_phase}' "
                    f"recently active, insufficient progress "
                    f"({self._findings_in_current_phase} findings)"
                )
                return True

        return False

    def _generate_strategic_clue(
        self,
        from_phase: str,
        to_phase: str,
        old_memory: PhaseMemory
    ) -> str:
        """Generate strategic clue for next phase (OPTIMIZATION: Task 3 - Nov 14, 2025)"""

        findings_count = len(old_memory.key_findings)

        # Extract quantities from findings
        import re
        quantities = []
        for finding in old_memory.key_findings:
            # Look for "N items", "N/M progress", etc.
            matches = re.findall(r'(\d+)\s*/?\s*(\d+)?\s+(items?|researchers?|profiles?|results?)', finding.lower())
            if matches:
                if matches[0][1]:  # Has denominator (e.g., "2/100")
                    quantities.append(f"{matches[0][0]}/{matches[0][1]} {matches[0][2]}")
                else:
                    quantities.append(f"{matches[0][0]} {matches[0][2]}")

        # Build clue based on transition
        if from_phase == "discovery":
            clue = f"Discovery: {findings_count} insights. "
            if quantities:
                clue += f"Noted: {', '.join(quantities[:2])}. "
            clue += "Proceed with verified tools and methods."

        elif from_phase == "collection":
            clue = f"Collection: {findings_count} events. "
            if quantities:
                clue += f"Progress: {', '.join(quantities[-2:])}. "
            clue += "Verify data integrity and exact counts before processing."

        elif from_phase == "processing":
            clue = f"Processing: {findings_count} insights generated. "
            clue += "Document final results with exact counts and verification."

        else:
            clue = f"{from_phase.title()}: {findings_count} findings complete. Review for {to_phase} context."

        return clue

    def _handle_phase_transition(
        self,
        old_phase: str,
        new_phase: str,
        transition_step: int
    ) -> None:
        """Handle transition from one phase to another with clue generation.

        Args:
            old_phase: Phase we're transitioning from
            new_phase: Phase we're transitioning to
            transition_step: Step number where transition occurs
        """
        logger.info(f"Phase transition detected: '{old_phase}' → '{new_phase}' at step {transition_step}")

        # Finalize old phase (use get_phase_by_name for list-based structure)
        old_memory = self.memory.get_phase_by_name(old_phase)
        strategic_clue = None

        if old_memory:
            # Generate summary from findings if not already set
            if not old_memory.summary:
                summary = self._generate_phase_summary(old_memory)
                old_memory.summary = summary

            # Generate phase embedding from summary (H-MEM Layer 2)
            if self.semantic_retriever and old_memory.summary:
                try:
                    embedding_array = self.semantic_retriever.embed_text(old_memory.summary)
                    if embedding_array is not None:
                        old_memory.phase_embedding = embedding_array.tolist()
                        logger.debug(f"Generated phase embedding for '{old_phase}'")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to embed phase summary: {e}")

            # OPTIMIZATION: Generate strategic clue (Task 3 - Nov 14, 2025)
            strategic_clue = self._generate_strategic_clue(
                from_phase=old_phase,
                to_phase=new_phase,
                old_memory=old_memory
            )

            # Mark as completed
            self.memory.complete_phase(
                phase_name=old_phase,
                end_step=transition_step - 1,
                summary=old_memory.summary
            )

        # Start or resume phase (prevents duplicate phase creation)
        self.memory.start_or_resume_phase(new_phase, transition_step)

        # OPTIMIZATION: Add strategic clue to new phase (Task 3 - Nov 14, 2025)
        if strategic_clue:
            clue_finding = f"[STRATEGIC CLUE FROM {old_phase.upper()}] {strategic_clue}"

            # Embed clue
            clue_embedding = None
            if self.semantic_retriever:
                try:
                    emb = self.semantic_retriever.embed_text(clue_finding)
                    if emb is not None:
                        clue_embedding = emb.tolist()
                except:
                    pass

            # Add to new phase as special finding
            self.memory.add_finding_to_phase(new_phase, clue_finding, clue_embedding)

            logger.info(f"💡 Strategic clue added to {new_phase}: {strategic_clue[:60]}...")

    def _generate_phase_summary(self, phase_memory: PhaseMemory) -> str:
        """Generate a summary for a phase from its findings.

        This is a simple implementation that concatenates findings.
        In the future, this could use LLM to generate better summaries.

        Args:
            phase_memory: Phase memory to summarize

        Returns:
            Generated summary string
        """
        findings = phase_memory.key_findings

        if not findings:
            return f"Completed {phase_memory.phase_name} phase"

        if len(findings) == 1:
            return findings[0]

        if len(findings) <= 3:
            return "; ".join(findings)

        # For many findings, list count
        return f"Completed {phase_memory.phase_name}: {len(findings)} findings"

    def finalize_current_phase(self, final_step: int, summary: Optional[str] = None) -> None:
        """Manually finalize the current active phase.

        Use this when ending a session or forcing a phase completion.

        Args:
            final_step: Final step number
            summary: Optional summary override
        """
        current_phase = self.memory.current_phase

        # Use get_phase_by_name for list-based structure
        phase_memory = self.memory.get_phase_by_name(current_phase)
        if phase_memory:
            # Generate summary if not provided
            if not summary:
                summary = self._generate_phase_summary(phase_memory)

            self.memory.complete_phase(current_phase, final_step, summary)
            logger.info(f"Manually finalized phase '{current_phase}' at step {final_step}")

    def get_current_phase_info(self) -> Optional[Dict[str, Any]]:
        """Get information about current phase.

        Returns:
            Dictionary with phase info, or None if no current phase
        """
        current = self.memory.get_current_phase_memory()
        if not current:
            return None

        return {
            "phase_name": current.phase_name,
            "started_step": current.started_step,
            "findings_count": len(current.key_findings),
            "status": current.status,
            "summary": current.summary
        }

    def get_all_phases_info(self) -> List[Dict[str, Any]]:
        """Get information about all phases.

        Returns:
            List of phase info dictionaries
        """
        phases_info = []

        # Iterate over list instead of dict.items()
        for phase_memory in self.memory.phase_memories:
            phases_info.append({
                "phase_name": phase_memory.phase_name,
                "started_step": phase_memory.started_step,
                "ended_step": phase_memory.ended_step,
                "findings_count": len(phase_memory.key_findings),
                "status": phase_memory.status,
                "summary": phase_memory.summary
            })

        # Sort by started_step
        phases_info.sort(key=lambda x: x["started_step"])
        return phases_info

    def update_progress(self, current_step: int, total_steps: Optional[int] = None) -> None:
        """Update session progress string.

        Args:
            current_step: Current step number
            total_steps: Total expected steps (if known)
        """
        if total_steps:
            self.memory.progress = f"{current_step}/{total_steps}"
        else:
            self.memory.progress = f"{current_step}/?"

        logger.debug(f"Updated progress to {self.memory.progress}")

    def get_phase_statistics(self) -> Dict[str, Any]:
        """Get statistics about phases.

        Returns:
            Dictionary with phase statistics
        """
        total_phases = len(self.memory.phase_memories)
        completed_phases = len(self.memory.phases_completed)
        active_phases = total_phases - completed_phases

        # Iterate over list instead of dict.values()
        total_findings = sum(
            len(pm.key_findings)
            for pm in self.memory.phase_memories
        )

        return {
            "total_phases": total_phases,
            "completed_phases": completed_phases,
            "active_phases": active_phases,
            "total_findings": total_findings,
            "current_phase": self.memory.current_phase,
            "phases_completed": self.memory.phases_completed
        }
