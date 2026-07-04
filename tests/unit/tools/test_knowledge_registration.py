"""Unit tests for KnowledgeTool registration (Task 6).

TDD coverage:
- KB_ENABLED unset → "knowledge" NOT in TOOL_DESCRIPTORS
- KB_ENABLED=1 (force or re-register) → "knowledge" present
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest


class TestKnowledgeRegistration:
    def setup_method(self):
        """Clean 'knowledge' from TOOL_DESCRIPTORS before each test."""
        from tools.descriptors import TOOL_DESCRIPTORS
        TOOL_DESCRIPTORS.pop("knowledge", None)

    def teardown_method(self):
        """Clean up after each test."""
        from tools.descriptors import TOOL_DESCRIPTORS
        TOOL_DESCRIPTORS.pop("knowledge", None)

    def test_disabled_not_in_descriptors(self):
        """With KB_ENABLED unset (defaults OFF on server), 'knowledge' is not registered."""
        from tools.descriptors import TOOL_DESCRIPTORS
        from tools.knowledge_ingest import register_knowledge_tool

        # Ensure KB_ENABLED is unset and POLYROB_LOCAL is unset so default is OFF
        env = {k: v for k, v in os.environ.items()
               if k not in ("KB_ENABLED", "POLYROB_LOCAL")}
        with patch.dict(os.environ, env, clear=True):
            result = register_knowledge_tool(force=False)

        assert result is False
        assert "knowledge" not in TOOL_DESCRIPTORS

    def test_enabled_via_env_registers(self):
        """With KB_ENABLED=1, 'knowledge' is registered in TOOL_DESCRIPTORS."""
        from tools.descriptors import TOOL_DESCRIPTORS
        from tools.knowledge_ingest import register_knowledge_tool, KnowledgeTool

        with patch.dict(os.environ, {"KB_ENABLED": "1"}, clear=False):
            result = register_knowledge_tool(force=False)

        assert result is True
        assert "knowledge" in TOOL_DESCRIPTORS
        assert TOOL_DESCRIPTORS["knowledge"].tool_class is KnowledgeTool

    def test_force_registers_even_when_disabled(self):
        """force=True registers regardless of KB_ENABLED."""
        from tools.descriptors import TOOL_DESCRIPTORS
        from tools.knowledge_ingest import register_knowledge_tool, KnowledgeTool

        env = {k: v for k, v in os.environ.items()
               if k not in ("KB_ENABLED", "POLYROB_LOCAL")}
        with patch.dict(os.environ, env, clear=True):
            result = register_knowledge_tool(force=True)

        assert result is True
        assert "knowledge" in TOOL_DESCRIPTORS
        assert TOOL_DESCRIPTORS["knowledge"].tool_class is KnowledgeTool

    def test_descriptor_attributes(self):
        """Registered descriptor has the expected attributes."""
        from tools.descriptors import TOOL_DESCRIPTORS, ToolCategory
        from tools.knowledge_ingest import register_knowledge_tool

        register_knowledge_tool(force=True)

        desc = TOOL_DESCRIPTORS["knowledge"]
        assert desc.name == "knowledge"
        assert desc.category == ToolCategory.INTEGRATION
        assert desc.is_optional is True
        assert desc.init_priority == 80

    def test_registration_is_idempotent(self):
        """Re-registering does not overwrite an already-present descriptor."""
        from tools.descriptors import TOOL_DESCRIPTORS
        from tools.knowledge_ingest import register_knowledge_tool

        register_knowledge_tool(force=True)
        first_desc = TOOL_DESCRIPTORS["knowledge"]

        # Re-register — should not overwrite
        register_knowledge_tool(force=True)
        assert TOOL_DESCRIPTORS["knowledge"] is first_desc

    def test_kb_off_server_byte_identical(self):
        """Without KB_ENABLED or POLYROB_LOCAL, tool is absent (server-safe default)."""
        from tools.descriptors import TOOL_DESCRIPTORS
        from tools.knowledge_ingest import register_knowledge_tool

        # Simulate server environment: no KB_ENABLED, no POLYROB_LOCAL
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("KB_ENABLED", "POLYROB_LOCAL")}
        with patch.dict(os.environ, clean_env, clear=True):
            result = register_knowledge_tool(force=False)

        assert result is False
        assert "knowledge" not in TOOL_DESCRIPTORS
