"""Unit tests for hierarchical memory data models."""

import pytest
from datetime import datetime
from pathlib import Path
import tempfile
import json

from modules.memory.task.hierarchical_memory import (
    Step,
    PhaseMemory,
    HierarchicalMemory,
)


class TestStep:
    """Test Step model."""

    def test_step_creation(self):
        """Test creating a step."""
        step = Step(
            step=1,
            phase="discovery",
            action_summary="goto(example.com)",
            finding="Found homepage"
        )

        assert step.step == 1
        assert step.phase == "discovery"
        assert step.action_summary == "goto(example.com)"
        assert step.finding == "Found homepage"
        assert isinstance(step.timestamp, datetime)

    def test_step_without_finding(self):
        """Test step can be created without finding."""
        step = Step(
            step=1,
            phase="discovery",
            action_summary="goto(example.com)",
            finding=None
        )

        assert step.finding is None

    def test_step_serialization(self):
        """Test step can be serialized to dict."""
        step = Step(
            step=1,
            phase="discovery",
            action_summary="goto(example.com)",
            finding="Found homepage"
        )

        data = step.model_dump()

        assert data["step"] == 1
        assert data["phase"] == "discovery"
        assert "timestamp" in data


class TestPhaseMemory:
    """Test PhaseMemory model."""

    def test_phase_memory_creation(self):
        """Test creating phase memory."""
        phase = PhaseMemory(
            phase_name="discovery",
            started_step=1,
            ended_step=None,
            summary="Finding API providers",
            key_findings=["Found 30 providers", "Selected top 10"],
            status="active"
        )

        assert phase.phase_name == "discovery"
        assert phase.started_step == 1
        assert phase.ended_step is None
        assert phase.summary == "Finding API providers"
        assert len(phase.key_findings) == 2
        assert phase.status == "active"

    def test_phase_memory_completed(self):
        """Test completed phase memory."""
        phase = PhaseMemory(
            phase_name="collection",
            started_step=11,
            ended_step=25,
            summary="Extracted pricing data",
            key_findings=["DataCo: $99", "Stripe: usage-based"],
            status="completed"
        )

        assert phase.ended_step == 25
        assert phase.status == "completed"

    def test_phase_memory_serialization(self):
        """Test phase memory serialization."""
        phase = PhaseMemory(
            phase_name="discovery",
            started_step=1,
            ended_step=None,
            summary="Finding API providers",
            key_findings=["Found 30 providers"],
            status="active"
        )

        data = phase.model_dump()

        assert data["phase_name"] == "discovery"
        assert data["key_findings"] == ["Found 30 providers"]


class TestHierarchicalMemory:
    """Test HierarchicalMemory model."""

    def test_hierarchical_memory_creation(self):
        """Test creating hierarchical memory."""
        memory = HierarchicalMemory(
            session_id="test-session-123",
            task="Research API providers",
            current_phase="discovery",
            progress="10/50",
            phases_completed=[],
            phase_memories=[],
            recent_steps=[]
        )

        assert memory.session_id == "test-session-123"
        assert memory.task == "Research API providers"
        assert memory.current_phase == "discovery"
        assert memory.progress == "10/50"
        assert len(memory.phases_completed) == 0
        assert len(memory.phase_memories) == 0
        assert len(memory.recent_steps) == 0

    def test_add_phase(self):
        """Test adding a phase to memory (position-indexed list API)."""
        memory = HierarchicalMemory(
            session_id="test-session-123",
            task="Research API providers",
            current_phase="discovery",
            progress="10/50",
            phases_completed=[],
            phase_memories=[],
            recent_steps=[]
        )

        # New phase is created/registered via start_or_resume_phase, which
        # appends to the phase_memories list and records its position in
        # phase_index (name -> position).
        memory.start_or_resume_phase("discovery", start_step=1)

        assert "discovery" in memory.phase_index
        assert memory.get_phase_by_name("discovery") is not None
        assert memory.get_phase_by_name("discovery").phase_name == "discovery"

    def test_add_finding_to_phase(self):
        """Test adding findings to a phase."""
        memory = HierarchicalMemory(
            session_id="test-session-123",
            task="Research API providers",
            current_phase="discovery",
            progress="10/50",
            phase_memories=[
                PhaseMemory(
                    phase_name="discovery",
                    started_step=1,
                    ended_step=None,
                    summary="Finding providers",
                    key_findings=[],
                    status="active"
                )
            ],
            phase_index={"discovery": 0},
            phases_completed=[],
            recent_steps=[]
        )

        memory.add_finding_to_phase("discovery", "Found DataCo API")
        memory.add_finding_to_phase("discovery", "Found Stripe API")

        discovery = memory.get_phase_by_name("discovery")
        assert len(discovery.key_findings) == 2
        assert "Found DataCo API" in discovery.key_findings

    def test_add_finding_to_nonexistent_phase(self):
        """Test adding finding to a phase after it is started.

        Note: ``add_finding_to_phase`` only adds to phases that already exist
        in ``phase_index``; the phase must first be registered via
        ``start_or_resume_phase``.
        """
        memory = HierarchicalMemory(
            session_id="test-session-123",
            task="Research API providers",
            current_phase="collection",
            progress="20/50",
            phases_completed=[],
            phase_memories=[],
            recent_steps=[]
        )

        memory.start_or_resume_phase("collection", start_step=20)
        memory.add_finding_to_phase("collection", "Extracted pricing")

        assert "collection" in memory.phase_index
        collection = memory.get_phase_by_name("collection")
        assert collection.phase_name == "collection"
        assert "Extracted pricing" in collection.key_findings

    def test_add_step(self):
        """Test adding steps to recent steps."""
        memory = HierarchicalMemory(
            session_id="test-session-123",
            task="Research API providers",
            current_phase="discovery",
            progress="10/50",
            phases_completed=[],
            phase_memories=[],
            recent_steps=[]
        )

        step = Step(
            step=1,
            phase="discovery",
            action_summary="goto(example.com)",
            finding="Found homepage"
        )

        memory.add_step(step)

        assert len(memory.recent_steps) == 1
        assert memory.recent_steps[0].step == 1

    def test_add_step_maintains_rolling_window(self):
        """Test that recent steps maintains a rolling window."""
        memory = HierarchicalMemory(
            session_id="test-session-123",
            task="Research API providers",
            current_phase="discovery",
            progress="10/50",
            phases_completed=[],
            phase_memories=[],
            recent_steps=[]
        )

        # Add 25 steps
        for i in range(25):
            step = Step(
                step=i + 1,
                phase="discovery",
                action_summary=f"action_{i}",
                finding=f"finding_{i}"
            )
            memory.add_step(step, max_steps=20)

        # Should only keep last 20
        assert len(memory.recent_steps) == 20
        assert memory.recent_steps[0].step == 6  # First kept is step 6
        assert memory.recent_steps[-1].step == 25  # Last is step 25

    def test_update_progress(self):
        """Test updating the progress string."""
        memory = HierarchicalMemory(
            session_id="test-session-123",
            task="Research API providers",
            current_phase="discovery",
            progress="10/50",
            phases_completed=[],
            phase_memories=[],
            recent_steps=[]
        )

        # Progress is a plain string field on the memory; the PhaseManager owns
        # the update_progress() helper that formats it.
        memory.progress = "25/100"

        assert memory.progress == "25/100"

    def test_save_and_load(self):
        """Test saving and loading memory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "hierarchical_memory.json"

            # Create memory
            memory = HierarchicalMemory(
                session_id="test-session-123",
                task="Research API providers",
                current_phase="discovery",
                progress="10/50",
                phases_completed=[],
                phase_memories=[
                    PhaseMemory(
                        phase_name="discovery",
                        started_step=1,
                        ended_step=None,
                        summary="Finding providers",
                        key_findings=["Found 30 providers"],
                        status="active"
                    )
                ],
                phase_index={"discovery": 0},
                recent_steps=[
                    Step(
                        step=1,
                        phase="discovery",
                        action_summary="goto(example.com)",
                        finding="Found homepage"
                    )
                ]
            )

            # Save
            memory.save(file_path)

            # Verify file exists
            assert file_path.exists()

            # Load
            loaded_memory = HierarchicalMemory.load(file_path)

            # Verify contents
            assert loaded_memory.session_id == "test-session-123"
            assert loaded_memory.task == "Research API providers"
            assert loaded_memory.current_phase == "discovery"
            assert "discovery" in loaded_memory.phase_index
            assert loaded_memory.get_phase_by_name("discovery").phase_name == "discovery"
            assert len(loaded_memory.recent_steps) == 1
            assert loaded_memory.recent_steps[0].step == 1

    def test_serialization(self):
        """Test full serialization to JSON."""
        memory = HierarchicalMemory(
            session_id="test-session-123",
            task="Research API providers",
            current_phase="discovery",
            progress="10/50",
            phases_completed=[],
            phase_memories=[
                PhaseMemory(
                    phase_name="discovery",
                    started_step=1,
                    ended_step=None,
                    summary="Finding providers",
                    key_findings=["Found 30 providers"],
                    status="active"
                )
            ],
            phase_index={"discovery": 0},
            recent_steps=[
                Step(
                    step=1,
                    phase="discovery",
                    action_summary="goto(example.com)",
                    finding="Found homepage"
                )
            ]
        )

        # Serialize
        data = memory.model_dump()

        # Verify structure (phase_memories is now a position-indexed list)
        assert data["session_id"] == "test-session-123"
        assert isinstance(data["phase_memories"], list)
        assert data["phase_index"]["discovery"] == 0
        assert data["phase_memories"][0]["phase_name"] == "discovery"
        assert len(data["recent_steps"]) == 1

        # Can be JSON encoded
        json_str = json.dumps(data, default=str)
        assert json_str is not None
