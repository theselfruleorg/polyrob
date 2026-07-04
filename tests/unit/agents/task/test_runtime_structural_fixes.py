"""Unit tests for the 2026-06-14 runtime/LLM structural fixes.

Covers the cleanly-isolatable pieces:
- P-1: per-model-family operational guidance in the system prompt.
- Tool-schema memoization in the controller Registry.
- R1: non-blocking send_message is tagged as a conversational reply.
- Dead response_cache removal on the LLM base client.

The R1 loop exit and R2 renderer dedup are exercised at a finer grain than a full
agent run allows; their logic is asserted via the building blocks they depend on.
"""
import pytest
from types import SimpleNamespace

from agents.task.agent.prompts import SystemPrompt


def _result(metadata=None, error=None, is_done=False):
    """Lightweight stand-in for an ActionResult for policy tests."""
    return SimpleNamespace(metadata=metadata, error=error, is_done=is_done)


# ----------------------------------------------------------------- R1 policy
class TestConversationalExitPolicy:
    def _reply(self):
        return _result(metadata={"conversational_reply": True})

    def _tool(self):
        return _result(metadata=None)

    def _planning(self):
        return _result(metadata={"planning_turn": True})

    def test_reply_only_step_detected(self):
        from agents.task.agent.core.conversational_exit import is_reply_only_step
        assert is_reply_only_step([self._reply()]) is True

    def test_empty_step_is_not_reply_only(self):
        from agents.task.agent.core.conversational_exit import is_reply_only_step
        assert is_reply_only_step([]) is False

    def test_mixed_step_is_not_reply_only(self):
        # A step that ran a tool AND sent a message is real work, not a chat-only reply.
        from agents.task.agent.core.conversational_exit import is_reply_only_step
        assert is_reply_only_step([self._tool(), self._reply()]) is False

    def test_planning_step_is_not_reply_only(self):
        from agents.task.agent.core.conversational_exit import is_reply_only_step
        assert is_reply_only_step([self._planning()]) is False

    def test_exit_only_after_two_consecutive_replies(self):
        from agents.task.agent.core.conversational_exit import should_conversational_exit
        assert should_conversational_exit(1, is_sub_agent=False) is False  # single reply: keep going
        assert should_conversational_exit(2, is_sub_agent=False) is True   # re-greet: end turn

    def test_subagent_never_exits(self):
        from agents.task.agent.core.conversational_exit import should_conversational_exit
        assert should_conversational_exit(5, is_sub_agent=True) is False

    def test_leading_status_message_then_work_is_not_cut(self):
        # Simulate: step1 = standalone status message, step2 = real tool work.
        # The counter must reset on the productive step so the task is NOT ended.
        from agents.task.agent.core.conversational_exit import (
            is_reply_only_step, should_conversational_exit,
        )
        n = 0
        # step 1: reply-only
        n = n + 1 if is_reply_only_step([self._reply()]) else 0
        assert should_conversational_exit(n, False) is False  # don't exit after 1
        # step 2: productive tool work -> reset
        n = n + 1 if is_reply_only_step([self._tool()]) else 0
        assert n == 0
        assert should_conversational_exit(n, False) is False  # task continues


# --------------------------------------------------------------------------- P-1
class TestModelFamilyInstructions:
    def _instr(self, model_name: str) -> str:
        return SystemPrompt(action_description="", model_name=model_name)._get_model_specific_instructions()

    def test_kimi_family_note_present(self):
        out = self._instr("moonshotai/kimi-k2.6")
        assert "MODEL NOTE (Kimi)" in out
        assert "tool-call" in out.lower()

    def test_gemini_family_note_present(self):
        out = self._instr("gemini-2.5-pro")
        assert "MODEL NOTE (Gemini)" in out

    def test_gpt_family_note_present(self):
        out = self._instr("gpt-4o")
        assert "MODEL NOTE (GPT)" in out

    def test_claude_gets_no_family_note(self):
        # The prompt is authored for Claude; no extra family note.
        out = self._instr("claude-opus-4-8")
        assert "MODEL NOTE" not in out

    def test_first_match_wins_single_block(self):
        # A name containing two needles should only get one block.
        out = self._instr("gemini-gpt-frankenmodel")
        assert out.count("MODEL NOTE") == 1


# --------------------------------------------------------------------------- E8
class TestGrokMcpBlockGating:
    def _instr(self, model_name: str, mcp_servers=None) -> str:
        return SystemPrompt(
            action_description="", model_name=model_name, mcp_servers=mcp_servers
        )._get_model_specific_instructions()

    def test_grok_block_absent_without_mcp(self):
        # A Grok model with NO MCP tools loaded must not get the MCP argument block.
        out = self._instr("grok-4")
        assert "MCP TOOL CALL FORMAT" not in out

    def test_grok_block_present_with_mcp(self):
        out = self._instr("grok-4", mcp_servers={"anysite": ["search"]})
        assert "MCP TOOL CALL FORMAT" in out

    def test_grok_block_teaches_direct_not_nested_form(self):
        out = self._instr("grok-4", mcp_servers={"anysite": ["search"]})
        # Teaches the direct {server}_{tool} flat-param form...
        assert "anysite_api(" in out
        # ...and marks the deprecated nested form as WRONG (not as the recommended form).
        wrong_idx = out.find("WRONG")
        nested_idx = out.find("mcp_execute_tool")
        assert wrong_idx != -1 and nested_idx != -1 and wrong_idx < nested_idx, (
            "the nested mcp_execute_tool form must appear only under the WRONG example"
        )

    def test_non_grok_model_with_mcp_gets_no_block(self):
        out = self._instr("claude-opus-4-8", mcp_servers={"anysite": ["search"]})
        assert "MCP TOOL CALL FORMAT" not in out


# --------------------------------------------------------- tool-schema memoization
class TestRegistrySchemaMemoization:
    def _registry(self):
        from tools.controller.registry.service import Registry
        return Registry()

    def test_same_call_returns_cached_identity(self):
        reg = self._registry()

        @reg.action("noop action")
        def noop_one(value: str = "x"):
            return value

        first = reg.get_all_actions_for_provider("openai")
        second = reg.get_all_actions_for_provider("openai")
        # Cache hit returns the very same list object (memoized).
        assert first is second

    def test_registering_action_busts_cache(self):
        reg = self._registry()

        @reg.action("first action")
        def act_a(value: str = "x"):
            return value

        before = reg.get_all_actions_for_provider("openai")

        @reg.action("second action")
        def act_b(value: str = "y"):
            return value

        after = reg.get_all_actions_for_provider("openai")
        # New action registered -> key changed -> fresh (different) list.
        assert after is not before
        assert len(after) == len(before) + 1

    def test_per_provider_keys_are_independent(self):
        reg = self._registry()

        @reg.action("noop action")
        def noop_two(value: str = "x"):
            return value

        openai_list = reg.get_all_actions_for_provider("openai")
        anthropic_list = reg.get_all_actions_for_provider("anthropic")
        assert openai_list is not anthropic_list
        # And each provider is independently memoized.
        assert reg.get_all_actions_for_provider("openai") is openai_list


# ------------------------------------------------------------- dead-cache removal
class TestDeadResponseCacheRemoved:
    def test_base_client_has_no_response_cache_attr(self):
        import modules.llm.llm_client as mod
        # The dead in-process response cache and its key builder are gone.
        assert not hasattr(mod.LLMClient, "_get_cache_key")
        src = open(mod.__file__).read()
        assert "self.response_cache" not in src


# ----------------------------------------------------------------- R2 renderer
class TestRendererBubbleDedup:
    def _msg_step(self, text: str, step: int):
        from cli.ui.events import Step
        return Step(
            step=step,
            actions=[{
                "action_type": "send_message",
                "name": "message",
                "params": {"text": text, "wait_for_response": False},
            }],
        )

    def _renderer(self):
        from rich.console import Console
        from cli.ui.rich_renderer import RichRenderer
        from cli.ui.state import SessionState
        console = Console(record=True, file=__import__("io").StringIO(), no_color=True)
        return RichRenderer(SessionState(), console=console), console

    def test_identical_repeat_bubble_suppressed(self):
        renderer, console = self._renderer()
        renderer._render_step(self._msg_step("hey there", 1))
        renderer._render_step(self._msg_step("hey there", 2))  # exact repeat
        text = console.export_text()
        # The duplicate greeting must appear only once.
        assert text.count("hey there") == 1

    def test_distinct_bubbles_both_render(self):
        renderer, console = self._renderer()
        renderer._render_step(self._msg_step("first message", 1))
        renderer._render_step(self._msg_step("second message", 2))
        text = console.export_text()
        assert "first message" in text
        assert "second message" in text


# ------------------------------------------------------- S-1 skill progressive disclosure
class TestSkillCatalog:
    def _skills(self):
        from agents.task.agent.skill_manager import MatchedSkill
        return [
            MatchedSkill(
                skill_id="linkedin-research", priority=1, match_reasons=["keyword:linkedin"],
                content="# LinkedIn Research\n" + ("body " * 500), description="Research people via MCP",
            ),
            MatchedSkill(
                skill_id="deck-builder", priority=2, match_reasons=[],
                content="# Deck\n" + ("body " * 500), description="",
            ),
        ]

    def test_catalog_lists_ids_and_references_load_skill(self):
        from agents.task.agent.skill_manager import SkillManager
        cat = SkillManager().format_skill_catalog(self._skills())
        assert "<skill-catalog>" in cat and "</skill-catalog>" in cat
        assert 'id="linkedin-research"' in cat
        assert 'id="deck-builder"' in cat
        assert "load_skill" in cat
        # No full bodies leak into the catalog.
        assert "body body" not in cat

    def test_catalog_is_far_smaller_than_full_bodies(self):
        from agents.task.agent.skill_manager import SkillManager
        sm = SkillManager()
        skills = self._skills()
        catalog = sm.format_skill_catalog(skills)
        full = sm.format_skills_for_prompt(skills)
        assert len(catalog) < len(full) / 5  # ~order of magnitude smaller

    def test_empty_catalog(self):
        from agents.task.agent.skill_manager import SkillManager
        assert SkillManager().format_skill_catalog([]) == ""

    def test_flag_default_on(self, monkeypatch):
        # P1-1: progressive disclosure is now ON by default (catalog + load_skill).
        # Explicitly OFF only when SKILL_PROGRESSIVE_DISCLOSURE is set to a falsey value.
        # Access-time function (not an import-bound constant) — see FL-D2.
        import agents.task.constants as c
        monkeypatch.delenv("SKILL_PROGRESSIVE_DISCLOSURE", raising=False)
        assert c.skill_progressive_disclosure() is True
        monkeypatch.setenv("SKILL_PROGRESSIVE_DISCLOSURE", "false")
        assert c.skill_progressive_disclosure() is False


class TestLoadSkillResult:
    def _skills(self):
        from agents.task.agent.skill_manager import MatchedSkill
        return {
            "linkedin-research": MatchedSkill(
                skill_id="linkedin-research", priority=1, match_reasons=[],
                content="# LinkedIn\nfull workflow body", description="x",
            )
        }

    def test_known_skill_returns_body(self):
        from tools.controller.service import build_load_skill_result
        res = build_load_skill_result(self._skills(), "linkedin-research")
        assert res.error is None
        assert "full workflow body" in res.extracted_content
        assert res.metadata == {"skill_loaded": "linkedin-research"}
        assert res.include_in_memory is True

    def test_unknown_skill_returns_error_with_available(self):
        from tools.controller.service import build_load_skill_result
        res = build_load_skill_result(self._skills(), "does-not-exist")
        assert res.error is not None
        assert "linkedin-research" in res.error  # lists what's available
        assert res.extracted_content is None

    def test_quoted_id_is_normalized(self):
        from tools.controller.service import build_load_skill_result
        res = build_load_skill_result(self._skills(), '"linkedin-research"')
        assert res.error is None
        assert res.metadata["skill_loaded"] == "linkedin-research"

    def test_empty_session_skills(self):
        from tools.controller.service import build_load_skill_result
        res = build_load_skill_result({}, "anything")
        assert res.error is not None
        assert "(none)" in res.error
