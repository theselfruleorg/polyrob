"""History / screenshot output mixin (roadmap P9 decomposition; code-motion from service.py).

Self-contained output concern: persisting history, building the run GIF, listing
conversation screenshots, and saving a screenshot to disk. Moved verbatim off the
``Agent`` god-file; ``Agent`` composes ``HistoryIOMixin`` so call sites
(``agent.save_screenshot``, ``self._make_history_item`` -> ``self.save_screenshot``)
are unchanged.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Optional

from agents.task.constants import MemoryConfig
from agents.task.agent.views import AgentOutput, ActionResult, AgentHistory
from tools.browser.views import BrowserState, BrowserStateHistory
from agents.task.path import pm


class HistoryIOMixin:
    """History/screenshot persistence for Agent (see module docstring)."""

    async def _make_history_item(
        self,
        model_output: "AgentOutput | None",
        state: BrowserState,
        result: List[ActionResult],
    ) -> None:
        """Create and store history item"""
        # Get interacted elements
        if model_output:
            interacted_elements = AgentHistory.get_interacted_element(model_output, state.selector_map)
        else:
            interacted_elements = [None]

        # Save screenshot to disk if we have a session ID
        # FIX (Dec 2025): Validate screenshot isn't blank/empty before saving
        screenshot_path = None
        if hasattr(self, 'session_id') and self.session_id and state.screenshot:
            # Validate screenshot has actual content (not blank)
            # A valid base64 PNG screenshot should be at least a few hundred bytes
            MIN_VALID_SCREENSHOT_SIZE = 500  # bytes in base64

            if len(state.screenshot) > MIN_VALID_SCREENSHOT_SIZE:
                try:
                    # B-T5: offload the blocking PIL encode+write to a worker thread so
                    # the async step path isn't stalled (_make_history_item is now async).
                    screenshot_path = await self.save_screenshot_async(
                        self.session_id,
                        state.screenshot,
                        self.state.n_steps
                    )
                    # Clear screenshot from state object to free memory
                    state.screenshot = None  # ADDED: Free memory after saving screenshot
                except Exception as e:
                    self.logger.warning(f"Failed to save screenshot: {e}")
            else:
                self.logger.debug(f"Skipping save of blank/small screenshot ({len(state.screenshot)} bytes)")
                state.screenshot = None  # Clear anyway to free memory

        # IMPORTANT: Create a lightweight state history WITHOUT the full screenshot blob
        # Store only the path to the saved screenshot to prevent memory bloat
        state_history = BrowserStateHistory(
            url=state.url,
            title=state.title,
            screenshot=None,  # Don't store the full screenshot in memory
            interacted_element=interacted_elements,
            screenshot_path=screenshot_path,  # Store the path instead
            tabs=getattr(state, 'tabs', [])  # Simply use getattr to safely get tabs
        )

        # Truncate page content if present to save memory
        if hasattr(state, 'page_content') and state.page_content:
            # Store only first 500 chars for context
            truncated_content = state.page_content[:500] if len(state.page_content) > 500 else state.page_content
            state_history.page_content_preview = truncated_content  # ADDED: Store truncated version
            # Clear the full content from state
            state.page_content = None  # ADDED: Free memory after storing preview

        # Store history item with the lightweight state
        history_item = AgentHistory(
            model_output=model_output,
            state=state_history,
            result=result
        )

        # Add history item to our history tracker
        self.history.history.append(history_item)

        # Save history file with proper session directory handling
        if not hasattr(self, 'session_id') or not self.session_id:
            return

        try:
            # Use already-clean session ID
            clean_id = self.session_id

            # Create history path using centralized path manager
            # Now save to history directory instead of workspace
            history_path = pm().get_history_dir(
                clean_id,
                self.user_id
            ) / f"agent_history_{self.agent_name}.json"

            # Update session data
            if hasattr(self, 'session_data') and self.session_data:
                self.session_data['history_path'] = str(history_path)

                # Update session metadata
                try:
                    self.session_manager.update_session_metadata(
                        clean_id,  # Use clean_id instead of self.session_id
                        {"history_path": str(history_path)}
                    )
                except Exception as e:
                    self.logger.debug(f"Could not update session metadata: {e}")

            # Ensure directory exists and save using pathlib
            try:
                Path(history_path).parent.mkdir(parents=True, exist_ok=True)
                self.history.save_to_file(history_path)
            except Exception as e:
                self.logger.error(f"Failed to save history file {history_path}: {e}", exc_info=True)

        except Exception as e:
            self.logger.warning(f"Error saving history: {e}")

    def save_history(self, file_path: Optional[str | Path] = None) -> None:
        """Save the history to a file"""
        if not file_path:
            file_path = 'AgentHistory.json'
        self.history.save_to_file(file_path)

    def create_history_gif(self, output_path: Optional[str] = None) -> Optional[str]:
        """
        Create a GIF from all screenshots in this agent's history.

        Args:
            output_path: Optional output path for the GIF

        Returns:
            Path to the created GIF or None if failed
        """
        if not self.history or not self.history.history:
            self.logger.warning("No history available to create GIF")
            return None

        from utils.gif_utils import create_history_gif
        return create_history_gif(
            history_items=list(self.history.history),
            session_id=self.session_id,
            output_path=output_path,
            user_id=self.user_id
        )

    def get_conversation_screenshots(self) -> List[str]:
        """
        Retrieve screenshot paths from the agent's history for creating a GIF.

        Returns:
            List of screenshot file paths (not base64 data to save memory)
        """
        from utils.gif_utils import get_conversation_screenshots
        history_items = list(self.history.history) if self.history and hasattr(self.history, 'history') else None
        return get_conversation_screenshots(
            history_items=history_items,
            session_id=self.session_id,
            user_id=self.user_id
        )

    def save_screenshot(self, session_id: str, screenshot_data: str, step_number: int = 0, is_fullpage: bool = False) -> Optional[str]:
        """
        Save a screenshot to disk.

        Args:
            session_id: The session ID
            screenshot_data: Base64-encoded screenshot data
            step_number: Current step number for filename
            is_fullpage: Whether this is a full page screenshot

        Returns:
            Path to the saved screenshot file or None if failed
        """
        if not screenshot_data:
            return None

        try:
            # Import here to avoid circular imports
            from agents.task.path import pm

            # Session ID already clean from orchestrator
            clean_id = session_id

            # Use PathManager to get screenshots directory
            screenshots_dir = pm().get_subdir(clean_id, "screenshots", user_id=self.user_id)

            # Create filename based on step number - use JPEG for better compression
            prefix = "fullpage" if is_fullpage else "step"
            filename = f"{prefix}_{step_number:03d}.jpg"

            # Create full path
            screenshot_path = screenshots_dir / filename

            # Convert base64 to binary and compress
            import base64
            from PIL import Image
            import io

            img_data = base64.b64decode(screenshot_data)

            # Open image and convert to JPEG with compression
            img = Image.open(io.BytesIO(img_data))

            # Convert RGBA to RGB if needed
            if img.mode == 'RGBA':
                rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                rgb_img.paste(img, mask=img.split()[3])
                img = rgb_img

            # Save with JPEG compression
            img.save(screenshot_path, 'JPEG', quality=MemoryConfig.SCREENSHOT_JPEG_QUALITY, optimize=True)

            # Send telemetry
            try:
                from agents.task.telemetry.views import ScreenshotSavedTelemetryEvent
                self.telemetry_manager.capture_event(
                    ScreenshotSavedTelemetryEvent(
                        agent_id=self.agent_id,
                        step=step_number,
                        screenshot_path=str(screenshot_path)
                    )
                )
            except Exception as telemetry_err:
                self.logger.debug(f"Failed to send telemetry for screenshot: {telemetry_err}")

            return str(screenshot_path)
        except Exception as e:
            self.logger.error(f"Error saving screenshot: {e}", exc_info=True)
            return None

    async def save_screenshot_async(
        self, session_id: str, screenshot_data: str, step_number: int = 0,
        is_fullpage: bool = False,
    ) -> Optional[str]:
        """Async screenshot save — offloads the blocking PIL write to a worker thread.

        The base64 decode + PIL convert + JPEG encode + disk write in
        :meth:`save_screenshot` are CPU/IO-bound; running them via
        ``asyncio.to_thread`` keeps the agent step loop responsive. Same return
        contract (path string or None).
        """
        if not screenshot_data:
            return None
        return await asyncio.to_thread(
            self.save_screenshot, session_id, screenshot_data, step_number, is_fullpage
        )
