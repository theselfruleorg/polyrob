"""Prompt data anchors.

H8: UnifiedPromptComposer + PromptPackage were dead code and were retired in the
prompt-stack consolidation. 2026-09-29 (review F16): SystemPromptManager and
BasePromptManager were retired too — nothing read their prompts, and the manager
rewrote data/prompts/system_prompts.json on every shutdown. The live prompt path
is the task agent's SystemPrompt builder (agents/task/agent/prompts.py).
"""

from pathlib import Path

# Default prompts directory — anchored to the install/repo root (this file is
# agents/prompt/__init__.py -> parents[2]), NOT the process CWD.
DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "data" / "prompts"
