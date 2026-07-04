"""Character model for agents."""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Union, Optional
from core.base_component import BaseComponent
from core.config import BotConfig
from core.logging import get_component_logger

class Character(BaseComponent):
    """Character model with personality traits and settings."""
    
    def __init__(self, name: str, config: BotConfig, container=None):
        """Initialize character."""
        if not name or not config:
            raise ValueError("Name and config are required")
        
        # Initialize base component first
        super().__init__(name=name, config=config, container=container)
        self.logger.debug(f"Creating character {name}")
        
        # Initialize character attributes
        try:
            self._initialize_attributes()
            self.logger.debug("Character attributes initialized")
        except Exception as e:
            self.logger.error(f"Error during character initialization: {e}")
            self.logger.error("Stack trace:", exc_info=True)
            raise

    def _initialize_attributes(self) -> None:
        """Initialize character attributes."""
        self.modelProvider = "anthropic"  # Default provider
        self.clients = ["anthropic"]  # Default clients
        self.settings = {}  # Empty settings to avoid overriding client settings
        self.bio = ""
        self.lore = []
        self.knowledge = []
        self.messageExamples = []
        self.postExamples = []
        self.topics = []
        self.adjectives = []
        self.style = {}

    async def _initialize(self) -> None:
        """Initialize character resources."""
        try:
            self.logger.debug(f"Starting character initialization for {self.name}")
            
            # Validate required attributes
            if not all(hasattr(self, attr) for attr in ['modelProvider', 'clients', 'settings']):
                self._initialize_attributes()
            
            # Set default values if not provided
            if not self.bio:
                self.bio = f"I am {self.name}, an AI assistant."
            
            # Convert bio to string if it's a list
            if isinstance(self.bio, list):
                self.bio = "\n".join(self.bio)
            
            self.logger.debug("Character initialization complete")
            self.logger.info(f"Character {self.name} initialized")
            
        except Exception as e:
            self.logger.error(f"Error initializing character {self.name}: {e}")
            self.logger.error("Stack trace:", exc_info=True)
            raise

    async def _cleanup(self) -> None:
        """Clean up character resources."""
        try:
            # Currently no specific cleanup needed
            self.logger.info(f"Character {self.name} cleaned up")
        except Exception as e:
            self.logger.error(f"Error cleaning up character {self.name}: {e}")
            raise

    def to_dict(self) -> Dict[str, Any]:
        """Convert character to dictionary."""
        return {
            'name': self.name,
            'modelProvider': self.modelProvider,
            'clients': self.clients,
            'settings': self.settings,
            'bio': self.bio,
            'lore': self.lore,
            'knowledge': self.knowledge,
            'messageExamples': self.messageExamples,
            'postExamples': self.postExamples,
            'topics': self.topics,
            'adjectives': self.adjectives,
            'style': self.style
        }
        
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Character':
        """Create character from dictionary."""
        character = cls(
            name=data.get('name', 'Unknown'),
            config=BotConfig(),  # Create minimal config
            container=None
        )
        # Set attributes from data
        character.modelProvider = data.get('modelProvider', 'anthropic')
        character.clients = data.get('clients', ['anthropic'])
        character.settings = data.get('settings', {})
        character.bio = data.get('bio', '')
        character.lore = data.get('lore', [])
        character.knowledge = data.get('knowledge', [])
        character.messageExamples = data.get('messageExamples', [])
        character.postExamples = data.get('postExamples', [])
        character.topics = data.get('topics', [])
        character.adjectives = data.get('adjectives', [])
        character.style = data.get('style', {})
        return character
        
    def __hash__(self):
        """Make character hashable."""
        return hash(self.name) 