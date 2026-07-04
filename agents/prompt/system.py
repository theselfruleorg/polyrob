"""System prompt management with character integration."""

import json
import logging
import asyncio
from typing import Optional, Dict, Any, Union, TYPE_CHECKING
from pathlib import Path
from core.config import BotConfig
from core.base_component import BaseComponent
from agents.personality.character import Character
from .base_prompt import BasePromptManager
from utils.message_utils import send_long_message
from utils.markdown_utils import escape_markdown

# aiogram (Telegram SDK) is heavy (~1.3s import) and used ONLY in the Telegram-only
# display_prompts() path. Keep it out of module load so importing this prompt system
# (every CLI invocation / server worker boot) does not pull aiogram.
# See docs/plans/2026-06-26-runtime-architecture-finalization-FUSION.md (P0a).
if TYPE_CHECKING:
    from aiogram import types


class SystemPromptManager(BaseComponent, BasePromptManager):
    """Manages system prompts with character integration."""
    
    def __init__(self, name: str, config: BotConfig, container=None, json_file_path: Optional[str] = None):
        """Initialize the system prompt manager."""
        if not name:
            raise ValueError("Name is required")
        if not config:
            raise ValueError("Config is required")
        
        super().__init__(name=name, config=config, container=container)
        self.config = config
        
        # Determine the correct file path
        if json_file_path:
            self.json_file_path = Path(json_file_path)
        elif config and hasattr(config, 'data_dir'):
            self.json_file_path = Path(config.data_dir) / "prompts" / "system_prompts.json"
        else:
            self.json_file_path = (
                Path(__file__).resolve().parents[2] / "data" / "prompts" / "system_prompts.json"
            )
        
        self._prompts = {}
        self._lock = asyncio.Lock()
        
        # Log the file path being used
        self.logger.info(f"Using prompts file path: {self.json_file_path}")
        
        # Default prompts as fallback
        self.default_prompts = {
            "chat_agent": """You are an AI assistant focused on helpful and accurate responses.
            
Key Guidelines:
1. Provide clear and accurate information
2. Be helpful while maintaining appropriate boundaries
3. Acknowledge uncertainty when present
4. Stay focused on the user's needs
5. Maintain a professional and friendly tone"""
        }

    async def _initialize(self) -> None:
        """Initialize the prompt manager."""
        try:
            # Ensure the prompts directory exists
            self.json_file_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Load prompts from JSON if file exists
            if self.json_file_path.exists():
                try:
                    self.logger.info(f"Loading prompts from: {self.json_file_path}")
                    with self.json_file_path.open('r', encoding='utf-8') as f:
                        loaded_prompts = json.load(f)
                        # Store loaded prompts
                        self._prompts = loaded_prompts
                        self.logger.info(f"Loaded {len(self._prompts)} prompts from {self.json_file_path}")
                        
                        # Log loaded roles
                        for role in self._prompts:
                            self.logger.info(f"Loaded prompt for role: {role}")
                except json.JSONDecodeError as e:
                    self.logger.error(f"Error parsing prompts file {self.json_file_path}: {e}")
                    self._prompts = {}
                except Exception as e:
                    self.logger.error(f"Error loading prompts from {self.json_file_path}: {e}")
                    self._prompts = {}
            else:
                self.logger.warning(f"No prompts file found at {self.json_file_path}")
                self._prompts = {}
            
        except Exception as e:
            self.logger.error(f"Failed to initialize system prompt manager: {e}")
            raise

    async def _cleanup(self) -> None:
        """Clean up resources."""
        try:
            # Save any pending changes
            await self._save_prompts()
            self._prompts.clear()
            self.logger.info("System prompt manager cleaned up successfully")
        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")
            raise

    def _read_prompts_sync(self) -> Dict[str, str]:
        """Read prompts from JSON file synchronously."""
        try:
            if not self.json_file_path.exists():
                return {}
                
            with self.json_file_path.open('r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            self.logger.error(f"Error reading prompts: {e}")
            return {}

    async def _save_prompts(self) -> None:
        """Save prompts to JSON file."""
        try:
            async with self._lock:
                with self.json_file_path.open('w', encoding='utf-8') as f:
                    json.dump(self._prompts, f, indent=2)
                self.logger.debug(f"Saved prompts to {self.json_file_path}")
        except Exception as e:
            self.logger.error(f"Error saving prompts: {e}")
            raise

    async def get_prompt(
        self,
        role: str,
        model_type: Optional[str] = None,
        character: Optional[Character] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Get system prompt with optional character integration."""
        try:
            if not self._initialized:
                await self.initialize()

            # First try to get prompt from JSON-loaded prompts
            base_prompt = self._prompts.get(role)
            
            # Log prompt source
            if base_prompt:
                self.logger.debug(f"Using JSON prompt for role: {role}")
            else:
                self.logger.warning(f"No JSON prompt found for role {role}, using default")
                base_prompt = self.default_prompts.get(role, "")
            
            if not base_prompt:
                self.logger.warning(f"No prompt template found for role: {role}")
                return ""
            
            # Start building final prompt
            prompt_parts = []
            
            # Add system prompt if available
            if base_prompt:
                prompt_parts.append(base_prompt)
            
            # Add character context if available
            if character:
                char_context = self._build_character_context(character)
                if char_context:
                    prompt_parts.append(char_context)
                    self.logger.debug(f"Added character context for {character.name}")
            
            # Add additional context from metadata if available
            if metadata:
                context = self._build_additional_context(metadata)
                if context:
                    prompt_parts.append(context)
                    self.logger.debug("Added metadata context")
            
            # Combine all parts with proper spacing
            final_prompt = "\n\n".join(filter(None, prompt_parts))
            
            # Log prompt statistics for troubleshooting
            self.logger.debug(f"Built system prompt for {role}, length: {len(final_prompt)} chars, with character: {character.name if character else 'None'}")
            
            return final_prompt
            
        except Exception as e:
            self.logger.error(f"Error building system prompt: {e}")
            return self.default_prompts.get(role, "")

    def _build_character_context(self, character: Character) -> str:
        """Build character context section."""
        try:
            sections = [
                f"CHARACTER PROFILE:",
                f"Name: {character.name}",
                f"Background: {character.bio}"
            ]
            
            if character.adjectives:
                sections.append(f"Personality: {', '.join(character.adjectives)}")
                
            if character.style.get('speaking'):
                sections.append("Speaking Style:")
                sections.extend(f"- {style}" for style in character.style['speaking'])
                
            if character.knowledge:
                sections.append("Knowledge & Expertise:")
                sections.extend(f"- {k}" for k in character.knowledge)
                
            if character.topics:
                sections.append("Topics of Interest:")
                sections.extend(f"- {t}" for t in character.topics)
                
            return "\n".join(sections)
            
        except Exception as e:
            self.logger.error(f"Error building character context: {e}")
            return ""

    def _build_additional_context(self, metadata: Dict[str, Any]) -> str:
        """Build additional context section from metadata."""
        try:
            sections = []
            
            # Add knowledge context if available
            if metadata.get('knowledge_context'):
                sections.append("RELEVANT KNOWLEDGE:")
                sections.append(metadata['knowledge_context'])
            
            # Add conversation mode if available
            if metadata.get('conversation_mode'):
                sections.append(f"Conversation Mode: {metadata['conversation_mode']}")
            
            # Add any other relevant metadata
            if metadata.get('additional_instructions'):
                sections.append("Additional Instructions:")
                sections.append(metadata['additional_instructions'])
            
            return "\n\n".join(sections) if sections else ""
            
        except Exception as e:
            self.logger.error(f"Error building additional context: {e}")
            return ""

    async def set_prompt(self, role: str, prompt: str) -> None:
        """Set a prompt for a specific role."""
        try:
            if not self._initialized:
                await self.initialize()

            # First, reload any existing prompts from disk to avoid overwriting
            try:
                with self.json_file_path.open('r', encoding='utf-8') as f:
                    current_prompts = json.load(f)
                    # Update our in-memory prompts with latest from disk
                    self._prompts.update(current_prompts)
                    self.logger.info(f"Reloaded current prompts before update, found {len(current_prompts)} roles")
            except (FileNotFoundError, json.JSONDecodeError) as e:
                self.logger.warning(f"Could not reload existing prompts: {e}")

            async with self._lock:
                # Log previous value if available for debugging
                previous = self._prompts.get(role, "")
                self.logger.info(f"Updating prompt for role '{role}', previous length: {len(previous)}, new length: {len(prompt)}")
                
                # Update prompt in memory
                self._prompts[role] = prompt.strip()
                
                # Save to file, ensuring directory exists
                self.json_file_path.parent.mkdir(parents=True, exist_ok=True)
                
                # Log what we're about to save
                self.logger.info(f"Saving {len(self._prompts)} prompts to {self.json_file_path}")
                for r in self._prompts:
                    self.logger.debug(f"Saving prompt for role: {r}, length: {len(self._prompts[r])}")
                
                # Save to file
                with self.json_file_path.open('w', encoding='utf-8') as f:
                    json.dump(self._prompts, f, indent=2, ensure_ascii=False)
                self.logger.info(f"Successfully saved prompt for role: {role}")
            
        except Exception as e:
            self.logger.error(f"Error setting prompt for role '{role}': {e}", exc_info=True)
            raise

    def _format_for_model_sync(
        self,
        prompt: str,
        model_type: str
    ) -> Union[str, Dict[str, Any]]:
        """Format the prompt for specific model types synchronously."""
        try:
            if model_type == "anthropic":
                return {
                    "system": prompt,
                    "messages": []
                }
            elif model_type == "openai":
                return {
                    "messages": [
                        {"role": "system", "content": prompt}
                    ]
                }
            else:
                return prompt
                
        except Exception as e:
            self.logger.error(f"Error formatting prompt for model {model_type}: {e}")
            return prompt

    def _integrate_character_sync(self, prompt: str, character: Character) -> str:
        """Integrate character information into the prompt synchronously."""
        try:
            # Build character profile section
            char_sections = [
                "\nCHARACTER PROFILE:",
                f"Name: {character.name}",
                f"Background: {character.bio}",
                f"Personality: {', '.join(character.adjectives)}",
                "\nSPEAKING STYLE:",
                *[f"- {style}" for style in character.style.get('speaking', [])],
                "\nKNOWLEDGE & EXPERTISE:",
                *[f"- {k}" for k in character.knowledge],
                "\nTOPICS OF INTEREST:",
                *[f"- {t}" for t in character.topics],
                "\nEXAMPLE MESSAGES:",
                *[f"- {ex}" for ex in character.messageExamples[:3]]
            ]
            
            char_profile = "\n".join(char_sections)
            
            # Add behavior guidelines
            guidelines = [
                "\nBEHAVIOR GUIDELINES:",
                "1. Maintain this personality consistently",
                "2. Use your expertise and knowledge naturally",
                "3. Express yourself in your unique voice",
                "4. Stay true to your character while being helpful",
                "5. Keep your responses focused and relevant"
            ]
            
            guidelines_text = "\n".join(guidelines)
            
            # Combine all sections
            return f"{prompt}\n\n{char_profile}\n\n{guidelines_text}"
            
        except Exception as e:
            self.logger.error(f"Error integrating character: {e}")
            return prompt

    # Keep these methods for backward compatibility
    async def format_for_model(
        self,
        prompt: str,
        model_type: str,
        character: Optional[Character] = None
    ) -> Union[str, Dict[str, Any]]:
        """Format the prompt for specific model types."""
        return self._format_for_model_sync(prompt, model_type)

    async def integrate_character(self, prompt: str, character: Character) -> str:
        """Integrate character information into the prompt."""
        return self._integrate_character_sync(prompt, character)

    async def format_prompts_display(self) -> str:
        """Format current prompts for display with proper markdown."""
        try:
            if not self._initialized:
                await self.initialize()
            
            sections = ["*System Prompts Management*\n\n*Current Prompts:*\n"]
            
            # Format main agent prompt
            sections.append("*Main Agent:*")
            main_prompt = self._prompts.get('chat_agent')
            if main_prompt:
                sections.append("```\n" + main_prompt + "\n```")
            else:
                sections.append("_Not set_")
            
            # Format simulator agent prompt
            sections.append("\n*Simulator Agent:*")
            sim_prompt = self._prompts.get('simulator_agent')
            if sim_prompt:
                sections.append("```\n" + sim_prompt + "\n```")
            else:
                sections.append("_Not set_")
            
            # Combine all sections with proper escaping
            formatted_text = "\n".join(sections)
            return escape_markdown(formatted_text)
        
        except Exception as e:
            self.logger.error(f"Error formatting prompts display: {e}")
            return "❌ Error formatting prompts display"

    async def display_prompts(self, message: "types.Message") -> None:
        """Display current prompts using message utils for proper formatting."""
        from aiogram import types
        from aiogram.enums import ParseMode
        try:
            formatted_text = await self.format_prompts_display()
            
            # Use send_long_message to handle long prompts
            await send_long_message(
                message=message,
                text=formatted_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                link_preview_options=types.LinkPreviewOptions(is_disabled=True)
            )
        
        except Exception as e:
            self.logger.error(f"Error displaying prompts: {e}")
            await message.answer(
                "❌ Error displaying prompts",
                link_preview_options=types.LinkPreviewOptions(is_disabled=True)
            )