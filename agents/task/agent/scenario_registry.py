"""
Scenario Registry - Manages multi-agent scenarios for the AutoV2 system.

Scenarios define execution patterns for multiple agents including sequential,
parallel, and handoff modes. Scenarios are loaded from JSON/YAML files in
data/task/scenarios/ directory.
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
import yaml

from agents.task.config import ScenarioModel, ScenarioStepModel
from agents.task.logging_config import get_task_logger

logger = get_task_logger('scenario_registry')

# Cache for loaded scenarios
_scenario_cache: Dict[str, ScenarioModel] = {}
_scenarios_loaded = False


def get_scenarios_dir() -> Path:
    """Get the scenarios directory path."""
    # Start from the current file's location and go up to project root
    current_file = Path(__file__)
    project_root = current_file.parent.parent.parent.parent  # Up to project root
    scenarios_dir = project_root / "data" / "task" / "scenarios"
    
    # Create directory if it doesn't exist
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    return scenarios_dir


def load_scenarios_from_disk() -> Dict[str, ScenarioModel]:
    """Load all scenarios from disk into cache."""
    global _scenario_cache, _scenarios_loaded
    
    if _scenarios_loaded:
        return _scenario_cache
    
    scenarios_dir = get_scenarios_dir()
    _scenario_cache = {}
    
    # Load JSON files
    for json_file in scenarios_dir.glob("*.json"):
        try:
            with open(json_file, 'r') as f:
                data = json.load(f)
                scenario = ScenarioModel(**data)
                _scenario_cache[scenario.id] = scenario
                logger.info(f"Loaded scenario '{scenario.id}' from {json_file.name}")
        except Exception as e:
            logger.error(f"Failed to load scenario from {json_file}: {e}")
    
    # Load YAML files
    for yaml_file in scenarios_dir.glob("*.yaml"):
        try:
            with open(yaml_file, 'r') as f:
                data = yaml.safe_load(f)
                scenario = ScenarioModel(**data)
                _scenario_cache[scenario.id] = scenario
                logger.info(f"Loaded scenario '{scenario.id}' from {yaml_file.name}")
        except Exception as e:
            logger.error(f"Failed to load scenario from {yaml_file}: {e}")
    
    # Create default scenarios if none exist
    if not _scenario_cache:
        logger.info("No scenarios found, creating defaults")
        _scenario_cache = create_default_scenarios()
    
    _scenarios_loaded = True
    return _scenario_cache


def create_default_scenarios() -> Dict[str, ScenarioModel]:
    """Create default scenarios if none exist."""
    scenarios = {}
    
    # Single executor scenario (default)
    single_executor = ScenarioModel(
        id="single_executor",
        name="Single Executor",
        description="Single agent handles the entire task",
        mode="sequential",
        steps=[
            ScenarioStepModel(
                step_id="executor",
                profile_id="executor",
                max_steps=100,
                overrides=None,
                share_controller=False
            )
        ],
        parallel_params={"max_concurrency": 1}
    )
    scenarios["single_executor"] = single_executor
    
    # Planner + Executor scenario removed - deprecated in favor of unified agent approach
    # The single executor can now handle both planning and execution internally
    
    # Parallel duo scenario
    parallel_duo = ScenarioModel(
        id="parallel_duo",
        name="Parallel Duo",
        description="Two executors work on the task in parallel",
        mode="parallel",
        steps=[
            ScenarioStepModel(
                step_id="executor1",
                profile_id="executor",
                max_steps=50,
                overrides={"agent_name": "executor1"},
                share_controller=False
            ),
            ScenarioStepModel(
                step_id="executor2",
                profile_id="executor",
                max_steps=50,
                overrides={"agent_name": "executor2"},
                share_controller=False
            )
        ],
        parallel_params={"max_concurrency": 2}
    )
    scenarios["parallel_duo"] = parallel_duo
    
    # Research handoff scenario
    research_handoff = ScenarioModel(
        id="research_handoff",
        name="Research Handoff",
        description="Researcher gathers info, then writer creates report",
        mode="handoff",
        steps=[
            ScenarioStepModel(
                step_id="research",
                profile_id="executor",
                max_steps=30,
                overrides={"agent_name": "researcher"},
                share_controller=False
            ),
            ScenarioStepModel(
                step_id="write",
                profile_id="executor",
                max_steps=30,
                overrides={"agent_name": "writer"},
                share_controller=True
            )
        ],
        parallel_params={"max_concurrency": 1}
    )
    scenarios["research_handoff"] = research_handoff
    
    # Save default scenarios to disk
    save_default_scenarios(scenarios)
    
    return scenarios


def save_default_scenarios(scenarios: Dict[str, ScenarioModel]) -> None:
    """Save default scenarios to disk."""
    from agents.task.utils import save_registry_items
    scenarios_dir = get_scenarios_dir()
    save_registry_items(scenarios, scenarios_dir, "scenario")


def get_scenario(scenario_id: str) -> Optional[ScenarioModel]:
    """Get a scenario by ID with robust error handling.
    
    Args:
        scenario_id: The scenario identifier
        
    Returns:
        The scenario model if found, None otherwise
    """
    try:
        scenarios = load_scenarios_from_disk()
        scenario = scenarios.get(scenario_id)
        
        if not scenario:
            logger.warning(f"Scenario '{scenario_id}' not found. Available scenarios: {list(scenarios.keys())}")
            # Try to return a default single_executor scenario if requested scenario not found
            if scenario_id != "single_executor" and "single_executor" in scenarios:
                logger.info(f"Falling back to default single_executor scenario")
                return scenarios["single_executor"]
        
        return scenario
    except Exception as e:
        logger.error(f"Error loading scenario '{scenario_id}': {e}")
        # Return a minimal default scenario on error
        try:
            return ScenarioModel(
                id=scenario_id,
                name=f"Fallback {scenario_id}",
                description="Fallback scenario due to loading error",
                mode="sequential",
                steps=[
                    ScenarioStepModel(
                        step_id="executor",
                        profile_id="executor",
                        max_steps=50
                    )
                ]
            )
        except:
            return None


def list_scenarios() -> List[str]:
    """List all available scenario IDs.
    
    Returns:
        List of scenario identifiers
    """
    scenarios = load_scenarios_from_disk()
    return list(scenarios.keys())


def reload_scenarios() -> None:
    """Force reload scenarios from disk."""
    global _scenarios_loaded
    _scenarios_loaded = False
    load_scenarios_from_disk()
    logger.info("Scenarios reloaded from disk")