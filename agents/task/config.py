"""
Task Session Configuration

This module provides a centralized configuration model for Task sessions,
replacing environment variables with per-session configuration that is
persisted and isolated between sessions.
"""

from typing import Dict, Any, List, Optional, Union
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime, timezone
# Use timezone.utc for compatibility with older Python versions
UTC = timezone.utc
import json
from pathlib import Path
import logging

from agents.task.constants import LoopDetectionConfig
from core.env import bool_env as _bool_env
from core.version import get_version
import os

logger = logging.getLogger(__name__)

# Task Performance Mode Configuration
class TaskMode:
    """Performance preset modes for Task"""
    FAST = "fast"  # Minimal retries, quick responses, less validation
    BALANCED = "balanced"  # Current defaults
    THOROUGH = "thorough"  # More retries, detailed analysis

    @staticmethod
    def get_mode() -> str:
        """Get current mode from environment"""
        return os.getenv('TASK_MODE', TaskMode.BALANCED).lower()

    @staticmethod
    def get_config(mode: str = None) -> Dict[str, Any]:
        """Get configuration for a specific mode"""
        if mode is None:
            mode = TaskMode.get_mode()

        configs = {
            TaskMode.FAST: {
                "max_steps": 30,
                "max_failures": 2,
                "max_parse_retries": 1,
                "telemetry_enabled": False,
                "loop_detection_threshold": 20,
                "min_steps_before_done": 2,
                "confidence_check_before_done": False,
                "task_verification_required": False,
            },
            TaskMode.BALANCED: {
                "max_steps": 50,
                "max_failures": 5,
                "max_parse_retries": int(os.getenv('MAX_PARSE_RETRIES', '3')),
                "telemetry_enabled": False,
                "loop_detection_threshold": int(os.getenv('UNCHANGED_STATE_THRESHOLD', '20')),
                "min_steps_before_done": int(os.getenv('MIN_STEPS_BEFORE_DONE', '3')),
                "confidence_check_before_done": _bool_env('CONFIDENCE_CHECK_BEFORE_DONE', True),
                "task_verification_required": _bool_env('TASK_VERIFICATION_REQUIRED', True),
            },
            TaskMode.THOROUGH: {
                "max_steps": 100,
                "max_failures": 7,
                "max_parse_retries": 5,
                "telemetry_enabled": True,
                "loop_detection_threshold": 25,
                "min_steps_before_done": 5,
                "confidence_check_before_done": True,
                "task_verification_required": True,
            },
        }
        return configs.get(mode, configs[TaskMode.BALANCED])



# Planner config removed - single agent is the default
# Internal planning happens automatically in the base Agent class




class LLMConfigModel(BaseModel):
    """LLM configuration"""
    model_config = ConfigDict(extra='forbid')
    
    model: str = Field(default="gpt-5", description="LLM model to use")
    provider: str = Field(default="openai", description="LLM provider")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0, description="LLM temperature")
    use_vision: bool = Field(default=True, description="Enable vision capabilities")


class LimitsConfigModel(BaseModel):
    """Execution limits configuration.

    NOTE: Some fields are duplicated in MessageManagerConfig for message formatting.
    Agent creates MessageManagerConfig from these limits. See MessageManagerConfig
    docstring for details on the separation.
    """
    model_config = ConfigDict(extra='forbid')

    max_steps: int = Field(default=100, ge=1, le=1000, description="Maximum steps for execution")
    # DUPLICATE: Also in MessageManagerConfig (for message formatting)
    max_actions_per_step: int = Field(default=10, ge=1, le=50, description="Maximum actions per step")
    # DUPLICATE: Also in MessageManagerConfig (for context limits)
    max_input_tokens: Optional[int] = Field(default=None, ge=1000, description="Maximum input tokens (None=auto-detect from model)")
    max_failures: int = Field(default=5, ge=1, le=20, description="Maximum consecutive failures before stopping")

    # TODO management
    # NOTE: use_todo_manager=True enables the TODO tool (agent can create/read/update TODO lists).
    # This is an optional productivity feature for complex tasks. Simple tasks can ignore it.
    use_todo_manager: bool = Field(default=True, description="Enable TODO tool for complex task organization (agent MAY use, not required)")
    min_steps_before_done: int = Field(default=2, ge=1, le=10, description="Minimum steps before allowing task completion (prevents premature done() calls)")

    # Artifact requirements
    require_artifacts_for_done: bool = Field(default=False, description="Require output artifacts before completion")
    required_artifacts: List[str] = Field(default_factory=list, description="List of required artifact files")


class OrchestratorConfigModel(BaseModel):
    """Orchestrator intervention configuration"""
    model_config = ConfigDict(extra='forbid')

    # Loop detection and intervention
    enable_loop_detection: bool = Field(default=True, description="Enable orchestrator loop detection")
    loop_detection_threshold: int = Field(default=8, ge=3, le=50, description="Action repetitions before intervention")
    enable_loop_intervention: bool = Field(default=True, description="Enable automatic loop breaking interventions")

    # System prompt modifications
    enable_system_prompt_injection: bool = Field(default=False, description="Allow orchestrator to modify agent system prompts")
    enable_todo_enforcement: bool = Field(default=False, description="Force agents to use TODO files found in workspace (generally NOT recommended)")

    # Message history management
    enable_history_clearing: bool = Field(default=False, description="[DEPRECATED-2024] History clearing no longer supported; will be removed in future version")
    enable_guidance_injection: bool = Field(default=True, description="Allow orchestrator to inject guidance messages")

    # Forced action overrides
    enable_forced_actions: bool = Field(default=False, description="[DEPRECATED-2024] Forced action overrides no longer supported; will be removed in future version")
    enable_todo_forced_updates: bool = Field(default=False, description="[DEPRECATED-2024] Forced TODO updates no longer supported; will be removed in future version")

    # Performance mode integration
    respect_performance_mode: bool = Field(default=True, description="Adjust intervention aggressiveness based on TaskMode")

    @classmethod
    def from_mode(cls, mode: str) -> 'OrchestratorConfigModel':
        """Create orchestrator config based on Task performance mode."""
        if mode == TaskMode.FAST:
            # Minimal interventions for fast mode
            return cls(
                enable_loop_detection=True,
                loop_detection_threshold=15,
                enable_loop_intervention=False,
                enable_system_prompt_injection=False,
                enable_todo_enforcement=False,
                enable_history_clearing=False,
                enable_guidance_injection=False,
                enable_forced_actions=False,
                enable_todo_forced_updates=False,
            )
        elif mode == TaskMode.THOROUGH:
            # Full interventions for thorough mode
            return cls(
                enable_loop_detection=True,
                loop_detection_threshold=5,
                enable_loop_intervention=True,
                enable_system_prompt_injection=True,
                enable_todo_enforcement=True,
                enable_history_clearing=True,
                enable_guidance_injection=True,
                enable_forced_actions=True,
                enable_todo_forced_updates=True,
            )
        else:  # BALANCED mode (default)
            # Moderate interventions for balanced mode
            return cls(
                enable_loop_detection=True,
                loop_detection_threshold=8,
                enable_loop_intervention=True,
                enable_system_prompt_injection=False,
                enable_todo_enforcement=False,
                enable_history_clearing=False,
                enable_guidance_injection=True,
                enable_forced_actions=False,
                enable_todo_forced_updates=False,
            )




class AgentProfileModel(BaseModel):
    """Agent profile configuration for role-based behavior"""
    model_config = ConfigDict(extra='forbid')
    
    # Identity
    id: str = Field(description="Profile identifier")
    name: str = Field(description="Display name")
    description: Optional[str] = Field(default=None, description="Profile description")
    
    # Prompt configuration
    prompt: Dict[str, Any] = Field(
        default_factory=lambda: {
            "prompt_type": "system",  # system|custom
            "prompt_source": "builtin",  # builtin|prompt_manager:key|inline
            "prompt_params": {}
        },
        description="Prompt configuration"
    )
    
    # LLM configuration
    llm: Dict[str, Any] = Field(
        default_factory=lambda: {
            "model": "gpt-5",
            "provider": "openai",
            "temperature": 0.0,
            "use_vision": False
        },
        description="LLM configuration"
    )
    
    # Tools configuration
    tools: Dict[str, Any] = Field(
        default_factory=lambda: {
            "enabled_actions": [],  # Empty = all, or list specific actions
            "tool_calling_method": "auto"
        },
        description="Tools configuration"
    )
    
    # Limits configuration
    limits: Dict[str, Any] = Field(
        default_factory=lambda: {
            "max_steps": 50,
            "max_actions_per_step": 10,
            "max_input_tokens": None,
            "max_failures": 3
        },
        description="Execution limits"
    )
    
    # UI hints
    label: Optional[str] = Field(default=None, description="UI label")
    icon: Optional[str] = Field(default=None, description="UI icon")


class ScenarioStepModel(BaseModel):
    """A step in a scenario sequence"""
    model_config = ConfigDict(extra='forbid')
    
    step_id: str = Field(description="Step identifier")
    profile_id: str = Field(description="Profile to use for this step")
    max_steps: Optional[int] = Field(default=None, description="Override max steps for this step")
    overrides: Optional[Dict[str, Any]] = Field(default=None, description="Additional overrides")
    share_controller: bool = Field(default=True, description="Share controller with previous step")
    
    # LLM configuration overrides
    llm: Optional[Dict[str, Any]] = Field(
        default=None,
        description="LLM override: {provider, model, temperature, use_vision}"
    )


class ScenarioModel(BaseModel):
    """Multi-agent scenario definition"""
    model_config = ConfigDict(extra='forbid')
    
    id: str = Field(description="Scenario identifier")
    name: str = Field(description="Scenario name")
    description: Optional[str] = Field(default=None, description="Scenario description")
    mode: str = Field(default="sequential", description="Execution mode: sequential|parallel|handoff")
    steps: List[ScenarioStepModel] = Field(description="Steps to execute")
    parallel_params: Dict[str, Any] = Field(
        default_factory=lambda: {"max_concurrency": 2},
        description="Parameters for parallel execution"
    )


class TaskSessionConfig(BaseModel):
    """
    Complete configuration for an Task session.
    
    This configuration is created per session and persisted to ensure
    reproducibility and isolation between sessions.
    """
    model_config = ConfigDict(extra='forbid')
    
    # Configuration sections
    llm: LLMConfigModel = Field(default_factory=LLMConfigModel, description="LLM configuration")
    limits: LimitsConfigModel = Field(default_factory=LimitsConfigModel, description="Execution limits")
    orchestrator: OrchestratorConfigModel = Field(default_factory=OrchestratorConfigModel, description="Orchestrator intervention configuration")
    tools: List[str] = Field(default_factory=lambda: ["browser", "task"], description="Enabled tools")
    tools_config: Dict[str, Any] = Field(
        default_factory=dict,
        description="Tool-specific configuration (e.g., {'mcp': {'servers': ['anysite']}})"
    )

    # Profile and scenario configuration
    scenario_id: Optional[str] = Field(default=None, description="Scenario to run")
    agent_profiles: Optional[List[AgentProfileModel]] = Field(default=None, description="Agent profile overrides")
    default_profile_id: Optional[str] = Field(default="executor", description="Default profile for single-agent sessions")
    
    # Metadata
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    version: str = Field(default_factory=get_version, description="Config version for compatibility")
    
    @staticmethod
    def defaults() -> 'TaskSessionConfig':
        """
        Get default configuration using package constants.

        Returns:
            TaskSessionConfig with default values from package constants
        """
        # Get mode-specific configuration
        mode_config = TaskMode.get_config()

        return TaskSessionConfig(
            llm=LLMConfigModel(
                model="gpt-5",
                provider="openai",
                temperature=0.0,
                use_vision=True
            ),
            limits=LimitsConfigModel(
                max_steps=mode_config.get('max_steps', 50),
                max_actions_per_step=10,
                max_input_tokens=None,  # Auto-detect from model
                max_failures=mode_config.get('max_failures', 3)
            ),
            tools=["browser", "task"]
        )
    
    def merge(self, overrides: Dict[str, Any]) -> 'TaskSessionConfig':
        """
        Merge override values into this configuration.
        
        Args:
            overrides: Dictionary of override values, can be nested
            
        Returns:
            New TaskSessionConfig with merged values
        """
        # Get current config as dict
        current = self.model_dump()
        
        # Deep merge overrides
        def deep_merge(base: dict, override: dict) -> dict:
            result = base.copy()
            for key, value in override.items():
                if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                    result[key] = deep_merge(result[key], value)
                else:
                    result[key] = value
            return result
        
        merged = deep_merge(current, overrides)
        
        # Create new config from merged dict
        return TaskSessionConfig.model_validate(merged)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return self.model_dump()
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TaskSessionConfig':
        """Create from dictionary."""
        return cls.model_validate(data)
    
    def save(self, path: Path) -> None:
        """
        Save configuration to JSON file.
        
        Args:
            path: Path to save the configuration
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)
    
    @classmethod
    def load(cls, path: Path) -> 'TaskSessionConfig':
        """
        Load configuration from JSON file.
        
        Args:
            path: Path to load the configuration from
            
        Returns:
            TaskSessionConfig loaded from file
        """
        with open(path, 'r') as f:
            data = json.load(f)
        return cls.from_dict(data)
    

    

    
    def summary(self) -> str:
        """
        Get a human-readable summary of the configuration.
        
        Returns:
            String summary of key configuration values
        """
        lines = [
            f"Task Session Config (v{self.version})",
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"LLM: {self.llm.model} ({self.llm.provider})",
            f"Temperature: {self.llm.temperature}",
            f"Vision: {'✓' if self.llm.use_vision else '✗'}",
            f"",
            f"Limits:",
            f"  Max Steps: {self.limits.max_steps}",
            f"  Max Actions/Step: {self.limits.max_actions_per_step}",
            f"  Max Failures: {self.limits.max_failures}",
            f"",
            f"Tools: {', '.join(self.tools)}",
        ]

        # Add tools_config info if present
        if self.tools_config:
            if "mcp" in self.tools_config and "servers" in self.tools_config["mcp"]:
                servers = self.tools_config["mcp"]["servers"]
                lines.append(f"  MCP Servers: {', '.join(servers)}")


        

        return "\n".join(lines)