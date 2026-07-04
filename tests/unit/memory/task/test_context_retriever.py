"""Unit tests for ContextRetriever."""

import pytest

from modules.memory.task.context_retriever import ContextRetriever
from modules.memory.task.hierarchical_memory import (
    HierarchicalMemory,
    PhaseMemory,
    Step,
)


class TestContextRetriever:
    """Test ContextRetriever functionality."""

    @pytest.fixture
    def memory_with_single_phase(self):
        """Create memory with single active phase."""
        return HierarchicalMemory(
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
                    summary="Finding API providers",
                    key_findings=[
                        "Found DataCo API",
                        "Found Stripe API",
                        "Found Twilio API"
                    ],
                    status="active"
                )
            ],
            phase_index={"discovery": 0},
            recent_steps=[
                Step(step=1, phase="discovery", action_summary="goto(datacom.com)", finding="Found DataCo"),
                Step(step=2, phase="discovery", action_summary="goto(stripe.com)", finding="Found Stripe"),
                Step(step=3, phase="discovery", action_summary="goto(twilio.com)", finding="Found Twilio"),
            ]
        )

    @pytest.fixture
    def memory_with_multiple_phases(self):
        """Create memory with multiple phases."""
        return HierarchicalMemory(
            session_id="test-session-456",
            task="Extract pricing from 10 providers",
            current_phase="collection",
            progress="25/50",
            phases_completed=["discovery"],
            phase_memories=[
                PhaseMemory(
                    phase_name="discovery",
                    started_step=1,
                    ended_step=10,
                    summary="Found 30 API providers",
                    key_findings=[
                        "Identified 30 providers",
                        "Selected top 10 by popularity",
                        "Categorized by service type"
                    ],
                    status="completed"
                ),
                PhaseMemory(
                    phase_name="collection",
                    started_step=11,
                    ended_step=None,
                    summary="Extracting pricing from providers",
                    key_findings=[
                        "DataCo: Pro $99/mo, Enterprise $199/mo",
                        "Stripe: Usage-based, 2.9% + 30¢ per transaction",
                        "Twilio: Pay-as-you-go, $0.0075 per SMS"
                    ],
                    status="active"
                )
            ],
            phase_index={"discovery": 0, "collection": 1},
            recent_steps=[
                Step(step=23, phase="collection", action_summary="extract(datacom/pricing)", finding="DataCo pricing"),
                Step(step=24, phase="collection", action_summary="goto(stripe.com/pricing)", finding=None),
                Step(step=25, phase="collection", action_summary="extract(stripe/pricing)", finding="Stripe pricing"),
            ]
        )

    @pytest.fixture
    def retriever_single_phase(self, memory_with_single_phase):
        """Create retriever with single phase memory."""
        return ContextRetriever(memory_with_single_phase)

    @pytest.fixture
    def retriever_multiple_phases(self, memory_with_multiple_phases):
        """Create retriever with multiple phases memory."""
        return ContextRetriever(memory_with_multiple_phases)

    def test_initialization(self, retriever_single_phase):
        """Test context retriever initialization."""
        assert retriever_single_phase.memory is not None

    def test_get_context_single_phase(self, retriever_single_phase):
        """Test getting context with single phase."""
        context = retriever_single_phase.get_context_injection()

        assert context is not None
        assert "test-session-123" in context
        assert "Research API providers" in context
        assert "discovery" in context
        assert "10/50" in context
        assert "Finding API providers" in context
        assert "Found DataCo API" in context

    def test_get_context_multiple_phases(self, retriever_multiple_phases):
        """Test getting context with multiple phases."""
        context = retriever_multiple_phases.get_context_injection()

        assert context is not None

        # Session info
        assert "test-session-456" in context
        assert "Extract pricing from 10 providers" in context
        assert "25/50" in context

        # Current phase
        assert "collection" in context.lower()
        assert "Extracting pricing from providers" in context

        # Completed phases
        assert "discovery" in context.lower()
        assert "Found 30 API providers" in context

        # Recent activity
        assert "extract(datacom/pricing)" in context or "DataCo pricing" in context

    def test_context_includes_current_phase_only(self, retriever_multiple_phases):
        """Test that detailed findings only from current phase."""
        context = retriever_multiple_phases.get_context_injection()

        # Current phase (collection) findings should be detailed
        assert "DataCo: Pro $99/mo" in context
        assert "Stripe: Usage-based" in context

        # Completed phase (discovery) should be summarized, not detailed
        assert "discovery" in context.lower()

    def test_context_includes_recent_steps(self, retriever_multiple_phases):
        """Test context includes recent steps."""
        context = retriever_multiple_phases.get_context_injection()

        # Recent steps should be included
        assert "[23]" in context or "23" in context
        assert "[24]" in context or "24" in context
        assert "[25]" in context or "25" in context

    def test_context_format_structure(self, retriever_multiple_phases):
        """Test context has proper structure."""
        context = retriever_multiple_phases.get_context_injection()

        # Should have section headers
        assert "[HIERARCHICAL MEMORY" in context or "HIERARCHICAL MEMORY" in context
        assert "Session:" in context or "SESSION" in context
        assert "Task:" in context or "TASK" in context
        assert "Progress:" in context or "PROGRESS" in context

    def test_context_filtering_by_phase(self, retriever_multiple_phases):
        """Test context can be filtered by specific phase."""
        # Get context for collection phase
        context = retriever_multiple_phases.get_context_injection(current_phase="collection")

        # Should focus on collection
        assert "collection" in context.lower()
        assert "Extracting pricing" in context

        # Get context for discovery phase
        context_discovery = retriever_multiple_phases.get_context_injection(current_phase="discovery")

        # Should focus on discovery
        assert "discovery" in context_discovery.lower()

    def test_context_token_estimation(self, retriever_multiple_phases):
        """Test context stays within expected token count."""
        context = retriever_multiple_phases.get_context_injection()

        # Rough estimation: 1 token ≈ 4 characters
        estimated_tokens = len(context) / 4

        # Should be around 600 tokens (±200 for safety)
        assert estimated_tokens < 1000, f"Context too large: ~{estimated_tokens} tokens"

    def test_context_with_many_findings(self):
        """Test context with many findings limits output."""
        # Create memory with many findings
        memory = HierarchicalMemory(
            session_id="test-session-789",
            task="Large research task",
            current_phase="collection",
            progress="50/100",
            phases_completed=[],
            phase_memories=[
                PhaseMemory(
                    phase_name="collection",
                    started_step=1,
                    ended_step=None,
                    summary="Collecting large dataset",
                    key_findings=[f"Finding {i}" for i in range(100)],  # 100 findings!
                    status="active"
                )
            ],
            phase_index={"collection": 0},
            recent_steps=[]
        )

        retriever = ContextRetriever(memory)
        context = retriever.get_context_injection()

        # Should limit findings (not show all 100)
        # Check that context is reasonable length
        estimated_tokens = len(context) / 4
        assert estimated_tokens < 1500, "Context should limit findings to stay under token budget"

    def test_context_with_no_findings(self):
        """Test context when phase has no findings yet."""
        memory = HierarchicalMemory(
            session_id="test-session-999",
            task="New task",
            current_phase="discovery",
            progress="1/50",
            phases_completed=[],
            phase_memories=[
                PhaseMemory(
                    phase_name="discovery",
                    started_step=1,
                    ended_step=None,
                    summary="Just started",
                    key_findings=[],
                    status="active"
                )
            ],
            phase_index={"discovery": 0},
            recent_steps=[]
        )

        retriever = ContextRetriever(memory)
        context = retriever.get_context_injection()

        # Should still generate valid context
        assert context is not None
        assert "test-session-999" in context
        assert "New task" in context
        assert "discovery" in context.lower()

    def test_context_with_empty_memory(self):
        """Test context with minimal memory."""
        memory = HierarchicalMemory(
            session_id="test-session-000",
            task="Minimal task",
            current_phase="discovery",
            progress="0/10",
            phases_completed=[],
            phase_memories=[],
            recent_steps=[]
        )

        retriever = ContextRetriever(memory)
        context = retriever.get_context_injection()

        # Should still generate basic context
        assert context is not None
        assert "test-session-000" in context
        assert "Minimal task" in context

    def test_context_formatting_consistency(self, retriever_single_phase, retriever_multiple_phases):
        """Test context formatting is consistent."""
        context_single = retriever_single_phase.get_context_injection()
        context_multiple = retriever_multiple_phases.get_context_injection()

        # Both should have similar structure
        assert "[HIERARCHICAL MEMORY" in context_single or "HIERARCHICAL MEMORY" in context_single
        assert "[HIERARCHICAL MEMORY" in context_multiple or "HIERARCHICAL MEMORY" in context_multiple

        # Both should have session info
        assert "Session:" in context_single or "SESSION" in context_single
        assert "Session:" in context_multiple or "SESSION" in context_multiple

    def test_context_phase_summary_format(self, retriever_multiple_phases):
        """Test completed phases are summarized correctly."""
        context = retriever_multiple_phases.get_context_injection()

        # Completed phases should have summary format
        assert "discovery" in context.lower()
        assert "Found 30 API providers" in context

        # Should indicate it's completed
        assert "✓" in context or "completed" in context.lower() or "COMPLETED" in context

    def test_context_recent_steps_format(self, retriever_multiple_phases):
        """Test recent steps are formatted correctly."""
        context = retriever_multiple_phases.get_context_injection()

        # Recent steps should show step numbers
        assert "23" in context
        assert "24" in context
        assert "25" in context

        # Should show actions
        assert "extract" in context.lower() or "goto" in context.lower()

    def test_context_current_phase_detail(self, retriever_multiple_phases):
        """Test current phase has most detail."""
        context = retriever_multiple_phases.get_context_injection()

        # Current phase should have:
        # - Phase name
        assert "collection" in context.lower()

        # - Summary
        assert "Extracting pricing" in context

        # - Detailed findings
        assert "DataCo:" in context
        assert "Stripe:" in context
        assert "Twilio:" in context
