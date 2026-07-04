"""Character management system."""

import json
import logging
from typing import Optional, Dict, List
from pathlib import Path

from core.config import BotConfig
from core.exceptions import ConfigurationError
from .character import Character
from core.logging import get_component_logger
from core.container import DependencyContainer

class CharacterManager:
    """Manages character personalities."""
    
    def __init__(self, name: str, config: BotConfig, container: DependencyContainer):
        """Initialize character manager."""
        self.name = name
        self.config = config
        self.container = container
        self.characters: Dict[str, Character] = {}
        self._active_character = None
        self._default_character = None
        
        # Fix 1-B: Check for characters in data directory first, then package directory
        data_chars_dir = Path(config.data_dir) / "characters" if hasattr(config, 'data_dir') else None
        package_chars_dir = Path(__file__).parent / "characters"
        
        # Use data directory if it exists and has character files, otherwise use package directory
        if data_chars_dir and data_chars_dir.exists() and list(data_chars_dir.glob("*.character.json")):
            self.characters_dir = data_chars_dir
        else:
            self.characters_dir = package_chars_dir
            
        self.logger = get_component_logger(f"CharacterManager.{name}")
        self._initialized = False
        
    async def initialize(self) -> None:
        """Initialize character manager."""
        if self._initialized:
            return
        
        try:
            if not self.config:
                raise ConfigurationError("No configuration provided")
            
            self.logger.debug("Starting character manager initialization")
            
            # Ensure characters directory exists
            if not self.characters_dir.exists():
                self.logger.error(f"Characters directory not found: {self.characters_dir}")
                raise ConfigurationError(f"Characters directory not found: {self.characters_dir}")
            
            # Create default character first
            default_name = self.config.get('personality.default_character', 'rob')
            self.logger.debug(f"Using default character name: {default_name}")
            
            # Load character from JSON file
            char_file = self.characters_dir / f"{default_name}.character.json"
            if not char_file.exists():
                self.logger.error(f"Default character file not found: {char_file}")
                raise ConfigurationError(f"Default character file not found: {char_file}")
            
            try:
                default_character = await self._load_character(char_file)
                if not default_character:
                    raise ConfigurationError("Failed to create default character")
                
                self.characters[default_name] = default_character
                self._default_character = default_character
                
            except Exception as e:
                self.logger.error(f"Error creating default character: {e}", exc_info=True)
                raise ConfigurationError(f"Failed to create default character: {e}")
            
            # Load additional characters
            for char_file in self.characters_dir.glob("*.character.json"):
                if char_file.stem != default_name:
                    try:
                        character = await self._load_character(char_file)
                        if character:
                            self.characters[character.name] = character
                    except Exception as e:
                        self.logger.error(f"Error loading character from {char_file}: {e}")
                        continue
            
            self._initialized = True
            self.logger.info(f"Loaded {len(self.characters)} characters")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize character manager: {e}")
            self.logger.error("Stack trace:", exc_info=True)
            raise

    def _load_characters(self) -> None:
        """Load all characters from JSON files."""
        try:
            if not self.characters_dir.exists():
                self.logger.error(f"Characters directory not found: {self.characters_dir}")
                raise ConfigurationError(f"Characters directory not found: {self.characters_dir}")

            for char_file in self.characters_dir.glob("*.character.json"):
                try:
                    with char_file.open('r', encoding='utf-8') as f:
                        char_data = json.load(f)
                        character = Character(
                            name=char_data.get('name', char_file.stem.replace('.character', '')),
                            **{k: v for k, v in char_data.items() if k != 'name'}
                        )
                        self.characters[character.name] = character
                        self.logger.debug(f"Loaded character: {character.name}")
                except Exception as e:
                    self.logger.error(f"Error loading character from {char_file}: {e}")
                    continue

            self.logger.info(f"Loaded {len(self.characters)} characters")

        except Exception as e:
            self.logger.error(f"Error loading character files: {e}")
            raise

    async def _load_character(self, char_file: Path) -> Optional[Character]:
        """Load a character from file."""
        try:
            with char_file.open('r', encoding='utf-8') as f:
                char_data = json.load(f)
                
            # Create character with proper initialization
            character = Character(
                name=char_data.get('name', char_file.stem),
                config=self.config,
                container=self.container
            )
            
            # Set character attributes
            character.modelProvider = char_data.get('modelProvider', 'anthropic')
            character.clients = char_data.get('clients', ['anthropic'])
            
            # We'll pass through settings for backward compatibility but avoid settings like
            # temperature and maxTokens that should be controlled at the client level
            character.settings = {
                k: v for k, v in char_data.get('settings', {}).items() 
                if k not in ['temperature', 'maxTokens']
            }
            
            character.bio = char_data.get('bio', '')
            character.lore = char_data.get('lore', [])
            character.knowledge = char_data.get('knowledge', [])
            character.messageExamples = char_data.get('messageExamples', [])
            character.postExamples = char_data.get('postExamples', [])
            character.topics = char_data.get('topics', [])
            character.adjectives = char_data.get('adjectives', [])
            character.style = char_data.get('style', {})
            
            # Initialize the character
            await character.initialize()
            return character
            
        except Exception as e:
            self.logger.error(f"Error loading character {char_file}: {e}")
            return None

    async def cleanup(self) -> None:
        """Clean up character manager resources."""
        if not self._initialized:
            return
        
        try:
            # Clean up all loaded characters
            for character in self.characters.values():
                await character.cleanup()
            self.characters.clear()
            
            self.logger.info("Character manager cleaned up")
            self._initialized = False
            
        except Exception as e:
            self.logger.error(f"Error during character manager cleanup: {e}")

    def _copy_default_characters(self) -> None:
        """Copy default character files from package."""
        if self.package_chars_dir.exists():
            for char_file in self.package_chars_dir.glob("*.character.json"):
                dest = self.characters_dir / char_file.name
                if not dest.exists():
                    dest.write_text(char_file.read_text())
                    self.logger.info(f"Copied default character: {char_file.name}")

    async def get_character(self, character_name: str) -> Optional[Character]:
        """Get a character by name."""
        if character_name in self.characters:
            return self.characters[character_name]

        # Try loading from characters directory
        char_path = self.characters_dir / f"{character_name}.character.json"
        if char_path.exists():
            try:
                with char_path.open('r', encoding='utf-8') as f:
                    char_data = json.load(f)
                    character = await self._load_character(char_path)
                    if character:
                        self.characters[character_name] = character
                        self.logger.info(f"Loaded character {character_name}")
                        return character
            except Exception as e:
                self.logger.error(f"Error loading character {character_name}: {e}")

        self.logger.warning(f"Character {character_name} not found")
        return None

    async def get_character_for_role(self, role_name: str) -> Optional[Character]:
        """Get character for specific role."""
        try:
            # First check if we have a role-specific character
            role_character = getattr(self.config, f'{role_name}_character', None)
            if role_character:
                return await self.get_character(role_character)
            
            # If no role-specific character, use default
            if not self._default_character:
                self._default_character = await self.get_default_character()
            
            return self._default_character
            
        except Exception as e:
            self.logger.error(f"Error getting character for role {role_name}: {e}")
            return None

    async def get_default_character(self) -> Optional[Character]:
        """Get default character."""
        try:
            if not self._default_character:
                default_name = getattr(self.config, 'default_character', 'rob')
                char_file = self.characters_dir / f"{default_name}.character.json"
                
                if not char_file.exists():
                    self.logger.error(f"Default character file not found: {char_file}")
                    return None
                    
                self._default_character = await self._load_character(char_file)
                
            return self._default_character
            
        except Exception as e:
            self.logger.error(f"Error getting default character: {e}")
            return None

    async def list_characters(self) -> List[str]:
        """Get list of available character names."""
        try:
            characters = []
            for char_file in self.characters_dir.glob("*.character.json"):
                characters.append(char_file.stem.replace('.character', ''))
            return sorted(characters)
        except Exception as e:
            self.logger.error(f"Error listing characters: {e}")
            return []

    async def reload_character(self, character_name: str) -> Optional[Character]:
        """Reload a character from disk."""
        try:
            # Remove from loaded characters if exists
            if character_name in self.characters:
                await self.characters[character_name].cleanup()
                del self.characters[character_name]
            
            # Load fresh from disk
            return await self.get_character(character_name)
            
        except Exception as e:
            self.logger.error(f"Error reloading character {character_name}: {e}")
            return None

    @property
    def required_services(self) -> Dict[str, str]:
        """Get required services."""
        return {}  # No required services

    def _validate_dependencies(self) -> None:
        """Validate dependencies."""
        pass  # No dependencies to validate 

    @property
    def active_character(self) -> Optional[Character]:
        """Get currently active character."""
        return self._active_character

    async def set_active_character(self, character_name: str) -> bool:
        """Set active character."""
        character = await self.get_character(character_name)
        if character:
            self._active_character = character
            return True
        return False

    async def get_character_for_role(self, role: str) -> Optional[Character]:
        """Get appropriate character for a specific role."""
        role_character_map = {
            'simulator': 'trump',  # Simulator agent uses Trump character
            'main': 'rob',        # Main agent uses Rob character
            'auto': 'rob'         # Auto agent uses Rob character
        }
        
        character_name = role_character_map.get(role)
        if not character_name:
            return self._active_character  # Fall back to active character
            
        return await self.get_character(character_name) 