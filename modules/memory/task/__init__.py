"""Task memory system - Hierarchical memory and context management for task agents.

This module implements a simplified H-MEM (Hierarchical Memory) architecture
combined with Anthropic-style context engineering for efficient long-running task agents.

Architecture:
    Layer 1: Session Summary (always in context)
    Layer 2: Phase Memories (dictionary keyed by phase name)
    Layer 3: Recent Steps (rolling window of last 20)

Key Features:
    - Phase-based memory grouping (discovery, collection, processing, documentation)
    - Constant ~600 token context regardless of session length
    - Message-level compaction (Anthropic style)
    - Session-isolated storage (hierarchical_memory.json per session)

Components:
    - HierarchicalMemory: 3-layer memory models
    - PhaseManager: Phase transition detection and grouping
    - ContextRetriever: Phase-based context injection
    - CompactionManager: Message and step compaction
    - TaskContextManager: Orchestration and lifecycle management

Reference:
    H-MEM Paper: https://arxiv.org/pdf/2507.22925
    Anthropic Context Engineering: https://www.anthropic.com/engineering
"""

from .hierarchical_memory import (
    HierarchicalMemory,
    PhaseMemory,
    Step,
)

from .phase_manager import PhaseManager
from .context_retriever import ContextRetriever
from .compaction_manager import CompactionManager
from .semantic_retriever import SemanticRetriever
from .task_context_manager import TaskContextManager

__all__ = [
    'HierarchicalMemory',
    'PhaseMemory',
    'Step',
    'PhaseManager',
    'ContextRetriever',
    'CompactionManager',
    'SemanticRetriever',
    'TaskContextManager',
]

from core.version import __version__  # noqa: F401  (project version SSOT)
