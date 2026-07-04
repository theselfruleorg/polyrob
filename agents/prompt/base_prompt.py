"""Base class for prompt management."""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Union
from agents.personality.character import Character


class BasePromptManager(ABC):
    """Abstract base class for prompt management.
    
    This class defines the interface for prompt management systems. All prompt managers
    must implement these methods to ensure consistent behavior across the system.
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the prompt manager.
        
        This method should be called before any other operations.
        It should handle loading prompts from storage and setting up any required resources.
        """
        pass

    @abstractmethod
    async def cleanup(self) -> None:
        """Clean up resources.
        
        This method should be called when the prompt manager is no longer needed.
        It should handle saving prompts and cleaning up any resources.
        """
        pass

    @abstractmethod
    async def get_prompt(
        self,
        role: str,
        model_type: Optional[str] = None,
        character: Optional[Character] = None
    ) -> Union[str, Dict[str, Any]]:
        """Get a prompt formatted for the specified model type.
        
        Args:
            role: The role/key identifying the prompt to retrieve
            model_type: Optional model type to format the prompt for
            character: Optional character to integrate into the prompt
            
        Returns:
            The prompt string or a model-specific prompt structure
        """
        pass

    @abstractmethod
    async def set_prompt(self, role: str, prompt: str) -> None:
        """Set a prompt for a specific role.
        
        Args:
            role: The role/key identifying where to store the prompt
            prompt: The prompt text to store
        """
        pass

    @abstractmethod
    async def format_for_model(
        self,
        prompt: str,
        model_type: str,
        character: Optional[Character] = None
    ) -> Union[str, Dict[str, Any]]:
        """Format a prompt for a specific model type.
        
        Args:
            prompt: The prompt text to format
            model_type: The type of model to format for (e.g., "openai", "anthropic")
            character: Optional character to integrate
            
        Returns:
            Formatted prompt structure for the specified model
        """
        pass

    @abstractmethod
    async def integrate_character(self, prompt: str, character: Character) -> str:
        """Integrate character information into a prompt.
        
        Args:
            prompt: The base prompt to integrate the character into
            character: The character information to integrate
            
        Returns:
            The prompt with character information integrated
        """
        pass