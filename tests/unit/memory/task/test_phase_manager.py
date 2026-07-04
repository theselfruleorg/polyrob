"""Unit tests for PhaseManager."""

import pytest
from unittest.mock import MagicMock

from modules.memory.task.phase_manager import PhaseManager
from modules.memory.task.hierarchical_memory import (
    HierarchicalMemory,
    PhaseMemory,
    Step,
)


class TestPhaseManager:
    """Test PhaseManager functionality."""

    @pytest.fixture
    def memory(self):
        """Create test hierarchical memory."""
        return HierarchicalMemory(
            session_id="test-session-123",
            task="Research API providers",
            current_phase="discovery",
            progress="0/50",
            phases_completed=[],
            phase_memories=[],
            recent_steps=[]
        )

    @pytest.fixture
    def phase_manager(self, memory):
        """Create phase manager."""
        return PhaseManager(memory)

    def test_initialization(self, phase_manager):
        """Test phase manager initialization.

        previous_phase now seeds from the memory's current_phase ("discovery"),
        and transition bookkeeping lives in _phase_transition_history (empty at
        start) rather than a standalone transition_step attribute.
        """
        assert phase_manager.memory is not None
        assert phase_manager.previous_phase == "discovery"
        assert phase_manager._phase_transition_history == []

    def test_add_first_step(self, phase_manager):
        """Test adding the first step."""
        brain_state = {
            "phase": "discovery",
            "memory": "Starting research",
            "next_goal": "Find providers"
        }

        step = phase_manager.add_step(
            step_number=1,
            brain_state=brain_state,
            action_summary="goto(example.com)",
            finding="Found homepage"
        )

        assert step.step == 1
        assert step.phase == "discovery"
        assert step.finding == "Found homepage"

        # Verify phase was created
        assert "discovery" in phase_manager.memory.phase_index
        assert phase_manager.memory.current_phase == "discovery"
        assert phase_manager.previous_phase == "discovery"

    def test_add_step_same_phase(self, phase_manager):
        """Test adding steps in same phase."""
        brain_state = {"phase": "discovery"}

        # Add first step
        phase_manager.add_step(1, brain_state, "action_1", "finding_1")

        # Add second step in same phase
        step = phase_manager.add_step(2, brain_state, "action_2", "finding_2")

        assert step.step == 2
        assert step.phase == "discovery"

        # Should still only have one phase
        assert len(phase_manager.memory.phase_memories) == 1
        assert "discovery" in phase_manager.memory.phase_index

        # Should have 2 findings
        assert len(phase_manager.memory.get_phase_by_name("discovery").key_findings) == 2

    def test_phase_transition_detected(self, phase_manager):
        """Test phase transition is detected.

        The smart-transition guard requires a minimum number of steps in the
        current phase (default 3) before honoring a phase change, so we spend
        3 steps in discovery before requesting collection. transition_step is
        now recorded in _phase_transition_history rather than an attribute.
        """
        # Spend enough steps in discovery to satisfy the min-steps guard
        brain_state_1 = {"phase": "discovery"}
        phase_manager.add_step(1, brain_state_1, "action_1", "finding_1")
        phase_manager.add_step(2, brain_state_1, "action_2", "finding_2")
        phase_manager.add_step(3, brain_state_1, "action_3", "finding_3")

        assert phase_manager.memory.current_phase == "discovery"
        assert phase_manager.previous_phase == "discovery"

        # Add step in collection phase (transition!)
        brain_state_2 = {"phase": "collection"}
        step = phase_manager.add_step(4, brain_state_2, "action_4", "finding_4")

        # Verify transition occurred
        assert phase_manager.memory.current_phase == "collection"
        assert phase_manager.previous_phase == "collection"
        assert phase_manager._phase_transition_history[-1].to_phase == "collection"
        assert phase_manager._phase_transition_history[-1].step == 4

        # Verify old phase was completed
        assert "discovery" in phase_manager.memory.phases_completed
        assert phase_manager.memory.get_phase_by_name("discovery").status == "completed"
        # discovery ended on the step before the transition (step 4 -> ended 3)
        assert phase_manager.memory.get_phase_by_name("discovery").ended_step == 3

        # Verify new phase was created
        assert "collection" in phase_manager.memory.phase_index
        assert phase_manager.memory.get_phase_by_name("collection").status == "active"
        assert phase_manager.memory.get_phase_by_name("collection").started_step == 4

    def test_multiple_phase_transitions(self, phase_manager):
        """Test multiple phase transitions.

        The smart-transition guard requires a minimum number of steps (default 3)
        in each phase before a transition is honored, so we spend 3 steps in
        every phase before moving on.
        """
        phases = ["discovery", "collection", "processing", "documentation"]
        steps_per_phase = 3  # satisfy the min-steps-before-transition guard

        step_number = 0
        for i, phase_name in enumerate(phases):
            brain_state = {"phase": phase_name}
            for _ in range(steps_per_phase):
                step_number += 1
                phase_manager.add_step(
                    step_number=step_number,
                    brain_state=brain_state,
                    action_summary=f"action_{step_number}",
                    finding=f"finding_{step_number}"
                )

        # Verify all phases except last are completed
        assert len(phase_manager.memory.phases_completed) == 3
        assert "discovery" in phase_manager.memory.phases_completed
        assert "collection" in phase_manager.memory.phases_completed
        assert "processing" in phase_manager.memory.phases_completed

        # Verify last phase is active
        assert phase_manager.memory.current_phase == "documentation"
        assert phase_manager.memory.get_phase_by_name("documentation").status == "active"

    def test_phase_missing_defaults_to_discovery(self, phase_manager):
        """Test missing phase field defaults to discovery."""
        brain_state = {"memory": "No phase field"}

        step = phase_manager.add_step(
            step_number=1,
            brain_state=brain_state,
            action_summary="action_1",
            finding="finding_1"
        )

        assert step.phase == "discovery"
        assert phase_manager.memory.current_phase == "discovery"

    def test_phase_re_entry(self, phase_manager):
        """Test re-entering a previous phase."""
        # Start in discovery
        phase_manager.add_step(1, {"phase": "discovery"}, "action_1", "finding_1")

        # Move to collection
        phase_manager.add_step(2, {"phase": "collection"}, "action_2", "finding_2")

        # Return to discovery
        step = phase_manager.add_step(3, {"phase": "discovery"}, "action_3", "finding_3")

        # Verify re-entry is handled
        assert phase_manager.memory.current_phase == "discovery"

        # Should have created a new phase entry or updated existing
        assert "discovery" in phase_manager.memory.phase_index

    def test_finding_without_content(self, phase_manager):
        """Test adding step without finding."""
        brain_state = {"phase": "discovery"}

        step = phase_manager.add_step(
            step_number=1,
            brain_state=brain_state,
            action_summary="goto(example.com)",
            finding=None
        )

        assert step.finding is None

        # Phase should still be created
        assert "discovery" in phase_manager.memory.phase_index

    def test_progress_tracking(self, phase_manager):
        """Test progress is tracked correctly.

        Progress is owned by ``PhaseManager.update_progress`` (the memory's
        ``progress`` field is just a formatted string), so we drive it
        explicitly alongside each step.
        """
        brain_state = {"phase": "discovery"}

        # Add steps and update progress with total_steps provided
        phase_manager.add_step(1, brain_state, "action_1", "finding_1")
        phase_manager.update_progress(1, total_steps=10)
        phase_manager.add_step(2, brain_state, "action_2", "finding_2")
        phase_manager.update_progress(2, total_steps=10)

        assert phase_manager.memory.progress == "2/10"

        phase_manager.add_step(5, brain_state, "action_5", "finding_5")
        phase_manager.update_progress(5, total_steps=10)

        assert phase_manager.memory.progress == "5/10"

    def test_phase_summary_generation(self, phase_manager):
        """Test phase summary is generated."""
        brain_state = {"phase": "discovery"}

        # Add multiple steps
        phase_manager.add_step(1, brain_state, "goto(provider1.com)", "Found Provider 1")
        phase_manager.add_step(2, brain_state, "goto(provider2.com)", "Found Provider 2")
        phase_manager.add_step(3, brain_state, "goto(provider3.com)", "Found Provider 3")

        phase = phase_manager.memory.get_phase_by_name("discovery")

        # Summary should be generated from findings
        assert phase.summary is not None
        assert len(phase.key_findings) == 3

    def test_transition_with_empty_phase(self, phase_manager):
        """Test transitioning from a phase with no findings.

        Spend the minimum required steps in discovery (all with no finding) so
        the smart-transition guard permits the move to collection.
        """
        brain_state_1 = {"phase": "discovery"}
        phase_manager.add_step(1, brain_state_1, "action_1", None)
        phase_manager.add_step(2, brain_state_1, "action_2", None)
        phase_manager.add_step(3, brain_state_1, "action_3", None)

        brain_state_2 = {"phase": "collection"}
        phase_manager.add_step(4, brain_state_2, "action_4", "finding_4")

        # Should still handle transition correctly
        assert "discovery" in phase_manager.memory.phases_completed
        assert phase_manager.memory.current_phase == "collection"

    def test_step_count_per_phase(self, phase_manager):
        """Test tracking step count per phase."""
        # Discovery: 3 steps
        for i in range(3):
            phase_manager.add_step(i + 1, {"phase": "discovery"}, f"action_{i}", f"finding_{i}")

        # Collection: 5 steps
        for i in range(5):
            phase_manager.add_step(i + 4, {"phase": "collection"}, f"action_{i}", f"finding_{i}")

        discovery_phase = phase_manager.memory.get_phase_by_name("discovery")
        collection_phase = phase_manager.memory.get_phase_by_name("collection")

        assert discovery_phase.started_step == 1
        assert discovery_phase.ended_step == 3

        assert collection_phase.started_step == 4
        # Collection is still active, so ended_step should be None
        assert collection_phase.ended_step is None

    def test_recent_steps_rolling_window(self, phase_manager):
        """Test recent steps maintains rolling window across phases.

        The default rolling window is now 100 steps (HierarchicalMemory.add_step
        max_steps default), so we add 120 steps total to exercise the cap. The
        intent is unchanged: the window stays bounded, keeps the most recent
        steps, and spans phase boundaries.
        """
        # Add 60 steps in discovery
        for i in range(60):
            phase_manager.add_step(i + 1, {"phase": "discovery"}, f"action_{i}", f"finding_{i}")

        # Add 60 steps in collection (120 total)
        for i in range(60):
            phase_manager.add_step(i + 61, {"phase": "collection"}, f"action_{i}", f"finding_{i}")

        # With default rolling window of 100, should only have last 100 steps
        assert len(phase_manager.memory.recent_steps) == 100

        # First step should be step 21 (120 total - 100 + 1)
        assert phase_manager.memory.recent_steps[0].step == 21

        # Last step should be step 120
        assert phase_manager.memory.recent_steps[-1].step == 120

        # Recent steps should span both phases
        phases_in_recent = {step.phase for step in phase_manager.memory.recent_steps}
        assert "discovery" in phases_in_recent
        assert "collection" in phases_in_recent
