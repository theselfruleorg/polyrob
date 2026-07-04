"""
Profile Registry - Manages agent profiles for the AutoV2 system.

Profiles define reusable agent configurations including prompts, LLM settings,
tools, and execution limits. Profiles are loaded from JSON/YAML files in
data/task/profiles/ directory.
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
import yaml

from agents.task.config import AgentProfileModel
from agents.task.logging_config import get_task_logger

logger = get_task_logger('profile_registry')

# Cache for loaded profiles
_profile_cache: Dict[str, AgentProfileModel] = {}
_profiles_loaded = False


def get_profiles_dir() -> Path:
    """Get the profiles directory path."""
    # Start from the current file's location and go up to project root
    current_file = Path(__file__)
    project_root = current_file.parent.parent.parent.parent  # Up to project root
    profiles_dir = project_root / "data" / "task" / "profiles"
    
    # Create directory if it doesn't exist
    profiles_dir.mkdir(parents=True, exist_ok=True)
    return profiles_dir


def load_profiles_from_disk() -> Dict[str, AgentProfileModel]:
    """Load all profiles from disk into cache."""
    global _profile_cache, _profiles_loaded
    
    if _profiles_loaded:
        return _profile_cache
    
    profiles_dir = get_profiles_dir()
    _profile_cache = {}
    
    # Load JSON files
    for json_file in profiles_dir.glob("*.json"):
        try:
            with open(json_file, 'r') as f:
                data = json.load(f)
                profile = AgentProfileModel(**data)
                _profile_cache[profile.id] = profile
                logger.info(f"Loaded profile '{profile.id}' from {json_file.name}")
        except Exception as e:
            logger.error(f"Failed to load profile from {json_file}: {e}")
    
    # Load YAML files
    for yaml_file in profiles_dir.glob("*.yaml"):
        try:
            with open(yaml_file, 'r') as f:
                data = yaml.safe_load(f)
                profile = AgentProfileModel(**data)
                _profile_cache[profile.id] = profile
                logger.info(f"Loaded profile '{profile.id}' from {yaml_file.name}")
        except Exception as e:
            logger.error(f"Failed to load profile from {yaml_file}: {e}")
    
    # Create default profiles if none exist
    if not _profile_cache:
        logger.info("No profiles found, creating defaults")
        _profile_cache = create_default_profiles()
    
    _profiles_loaded = True
    return _profile_cache


def create_default_profiles() -> Dict[str, AgentProfileModel]:
    """Create default profiles if none exist."""
    profiles = {}
    
    # Default executor profile
    executor = AgentProfileModel(
        id="executor",
        name="Executor Agent",
        description="General purpose task executor with full capabilities",
        prompt={
            "prompt_type": "system",
            "prompt_source": "builtin",
            "prompt_params": {}
        },
        llm={
            "model": "gpt-5",
            "provider": "openai",
            "temperature": 0.0,
            "use_vision": True
        },
        tools={
            "enabled_actions": [],  # All actions enabled
            "tool_calling_method": "auto"
        },
        limits={
            "max_steps": 50,
            "max_actions_per_step": 10,
            "max_input_tokens": None,
            "max_failures": 3
        }
    )
    profiles["executor"] = executor
    # Planner profile removed - deprecated in favor of unified agent approach
    # Save default profiles to disk
    save_default_profiles(profiles)
    
    return profiles


def save_default_profiles(profiles: Dict[str, AgentProfileModel]) -> None:
    """Save default profiles to disk."""
    from agents.task.utils import save_registry_items
    profiles_dir = get_profiles_dir()
    save_registry_items(profiles, profiles_dir, "profile")


def get_profile(profile_id: str) -> Optional[AgentProfileModel]:
    """Get a profile by ID with robust error handling.
    
    Args:
        profile_id: The profile identifier
        
    Returns:
        The profile model if found, None otherwise
    """
    try:
        profiles = load_profiles_from_disk()
        profile = profiles.get(profile_id)
        
        if not profile:
            logger.warning(f"Profile '{profile_id}' not found. Available profiles: {list(profiles.keys())}")
            # Try to return a default executor profile if requested profile not found
            if profile_id != "executor" and "executor" in profiles:
                logger.info(f"Falling back to default executor profile")
                return profiles["executor"]
        
        return profile
    except Exception as e:
        logger.error(f"Error loading profile '{profile_id}': {e}")
        # Return a minimal default profile on error
        try:
            return AgentProfileModel(
                id=profile_id,
                name=f"Fallback {profile_id}",
                description="Fallback profile due to loading error"
            )
        except:
            return None


def list_profiles() -> List[str]:
    """List all available profile IDs.
    
    Returns:
        List of profile identifiers
    """
    profiles = load_profiles_from_disk()
    return list(profiles.keys())


def reload_profiles() -> None:
    """Force reload profiles from disk."""
    global _profiles_loaded
    _profiles_loaded = False
    load_profiles_from_disk()
    logger.info("Profiles reloaded from disk")