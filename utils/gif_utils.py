"""
GIF creation utilities for task execution visualization.

This module provides utilities for creating GIFs from screenshots and text,
primarily used for visualizing task execution history.

Consolidates GIF creation logic previously scattered across:
- agents/task/agent/service.py (create_history_gif, get_conversation_screenshots)
- agents/task/utils.py (create_gif_with_retry, _create_text_only_gif)
"""

import logging
import os
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def create_history_gif(
    history_items: List,
    session_id: str,
    output_path: Optional[str] = None,
    user_id: Optional[str] = None,
    max_screenshots: int = 20
) -> Optional[str]:
    """
    Create a GIF from history items with screenshots.

    Args:
        history_items: List of history items (from agent.history.history)
        session_id: Session ID for path management
        output_path: Optional output path for the GIF
        user_id: Optional user ID for multi-user installations
        max_screenshots: Maximum number of screenshots to include

    Returns:
        Path to the created GIF or None if failed
    """
    if not history_items:
        logger.warning("No history items provided for GIF creation")
        return None

    try:
        # Extract screenshots and captions from history
        screenshot_paths, caption_texts = _extract_screenshots_from_history(
            history_items, max_screenshots
        )

        if not screenshot_paths:
            logger.warning("No screenshots found in history")
            return None

        # Determine output path using PathManager
        if not output_path:
            from agents.task.path import pm
            output_dir = pm().get_subdir(session_id, "results", user_id=user_id)
            output_path = str(output_dir / f"history_{int(time.time())}.gif")

        # Create GIF with paths (not loaded data)
        success = create_gif_with_retry(screenshot_paths, output_path, caption_texts, user_id=user_id)

        if success:
            logger.info(f"Created history GIF at {output_path}")
            return output_path
        else:
            logger.warning("Failed to create GIF, attempting fallback method")
            return create_text_only_gif(output_path, caption_texts or ["GIF creation failed"], user_id=user_id)
    except Exception as e:
        logger.error(f"Error creating history GIF: {e}", exc_info=True)
        return None


def get_conversation_screenshots(
    history_items: Optional[List],
    session_id: str,
    user_id: Optional[str] = None,
    max_screenshots: int = 20
) -> List[str]:
    """
    Retrieve screenshot paths from history or session directory.

    Args:
        history_items: Optional list of history items to extract from
        session_id: Session ID for path management
        user_id: Optional user ID for multi-user installations
        max_screenshots: Maximum number of screenshots to retrieve

    Returns:
        List of screenshot file paths
    """
    screenshot_paths = []

    # First try to get screenshots from history
    if history_items:
        screenshot_paths, _ = _extract_screenshots_from_history(history_items, max_screenshots)

    # If no screenshots in history, try to find saved screenshot files
    if not screenshot_paths and session_id:
        try:
            from agents.task.path import pm
            # Get screenshot directory using path manager
            screenshots_dir = pm().get_subdir(session_id, "screenshots", user_id=user_id)

            # Check if directory exists
            if screenshots_dir.exists():
                # Get last N PNG files in the directory, sorted by name
                screenshot_files = sorted([f for f in screenshots_dir.glob("*.png")])[-max_screenshots:]

                # Only collect paths, not data
                for file_path in screenshot_files:
                    if file_path.exists():
                        screenshot_paths.append(str(file_path))
        except Exception as e:
            logger.warning(f"Error retrieving screenshot paths: {e}")

    return screenshot_paths


def create_gif_with_retry(
    screenshots: List[str],
    output_path: str,
    caption_texts: Optional[List[str]] = None,
    max_retries: int = 3,
    delay_seconds: float = 0.5,
    user_id: Optional[str] = None
) -> bool:
    """
    Create a GIF from a list of screenshot paths with optional captions.
    Retries on failure and has a fallback mechanism for text-only GIFs.

    Args:
        screenshots: List of screenshot paths to include in the GIF
        output_path: Path where to save the resulting GIF
        caption_texts: Optional list of captions for each screenshot
        max_retries: Maximum number of retries on failure
        delay_seconds: Delay between frames in seconds
        user_id: Optional user ID for multi-user installations

    Returns:
        Boolean indicating success
    """
    if not screenshots:
        logger.warning("No screenshots provided for GIF creation")
        return create_text_only_gif(output_path, caption_texts or ["No screenshots available"], user_id=user_id)

    # First try to use imageio
    for attempt in range(max_retries):
        try:
            import imageio
            from PIL import Image, ImageDraw, ImageFont

            # Create images with captions if provided
            images = []
            for i, screenshot_path in enumerate(screenshots):
                try:
                    # Skip if screenshot doesn't exist
                    if not os.path.exists(screenshot_path):
                        logger.warning(f"Screenshot {screenshot_path} does not exist, skipping")
                        continue

                    # Open image
                    img = Image.open(screenshot_path)

                    # Add caption if provided
                    if caption_texts and i < len(caption_texts) and caption_texts[i]:
                        img = _add_caption_to_image(img, caption_texts[i])

                    # Add to list
                    images.append(img)
                except Exception as e:
                    logger.error(f"Error processing image {screenshot_path}: {e}")

            # If we have images, create the GIF
            if images:
                # Normalize output path
                output_path = _normalize_output_path(output_path)

                # Ensure parent directory exists
                os.makedirs(os.path.dirname(output_path), exist_ok=True)

                # Save as GIF
                imageio.mimsave(output_path, images, format='GIF', duration=delay_seconds)
                logger.info(f"Created GIF with {len(images)} frames at {output_path}")
                return True
            else:
                logger.warning("No valid images to create GIF")
                return create_text_only_gif(output_path, caption_texts or ["No valid screenshots"], user_id=user_id)

        except (ImportError, Exception) as e:
            logger.warning(f"GIF creation attempt {attempt+1} failed: {e}")

            if attempt == max_retries - 1:
                # Last attempt failed, create text-only GIF
                logger.info("All GIF creation attempts failed, creating text-only GIF")
                return create_text_only_gif(output_path, caption_texts or ["GIF creation failed"], user_id=user_id)

            # Wait before retrying
            time.sleep(0.5)

    # Should not reach here, but just in case
    return False


def create_text_only_gif(output_path: str, texts: List[str], user_id: Optional[str] = None) -> bool:
    """
    Create a text-only GIF when image processing fails.

    Args:
        output_path: Path to save the GIF
        texts: List of text strings to include in the GIF
        user_id: Optional user ID for path handling

    Returns:
        True if successfully created, False otherwise
    """
    try:
        from PIL import Image, ImageDraw, ImageFont

        # Normalize the path
        output_path = _normalize_output_path(output_path)

        # Create a blank image
        width, height = 800, 600
        image = Image.new('RGB', (width, height), color='white')
        draw = ImageDraw.Draw(image)

        try:
            font = ImageFont.truetype("arial.ttf", 16)
        except OSError:
            # Fallback to default font
            font = ImageFont.load_default()

        # Add text
        y_position = 50
        for text in texts:
            # Wrap text to fit width
            wrapped_text = _wrap_text(text, font, width - 100)

            # Draw each line
            for line in wrapped_text:
                draw.text((50, y_position), line, fill='black', font=font)
                y_position += 24

            # Add separator
            y_position += 20
            draw.line([(50, y_position), (width - 50, y_position)], fill='gray', width=1)
            y_position += 20

        # Ensure parent directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Save as GIF
        image.save(output_path, format='GIF')
        logger.info(f"Created text-only GIF at {output_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to create text-only GIF: {e}")
        return False


# ================================
# PRIVATE HELPER FUNCTIONS
# ================================

def _extract_screenshots_from_history(history_items: List, max_screenshots: int) -> tuple[List[str], List[str]]:
    """
    Extract screenshot paths and captions from history items.

    Args:
        history_items: List of history items
        max_screenshots: Maximum number of screenshots to extract

    Returns:
        Tuple of (screenshot_paths, caption_texts)
    """
    screenshot_paths = []
    caption_texts = []

    # Limit to last N screenshots to prevent memory issues
    limited_items = list(history_items)[-max_screenshots:] if len(history_items) > max_screenshots else history_items

    for i, item in enumerate(limited_items):
        # Only collect paths, not data - memory optimization
        if item.state:
            if hasattr(item.state, 'screenshot_path') and item.state.screenshot_path:
                screenshot_file = Path(item.state.screenshot_path)
                if screenshot_file.exists():
                    screenshot_paths.append(str(screenshot_file))
                    caption_texts.append(_extract_caption_from_item(item, i))

            elif hasattr(item.state, 'screenshot') and item.state.screenshot:
                # Save base64 screenshot to temp file first
                try:
                    import tempfile
                    import base64
                    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                        tmp.write(base64.b64decode(item.state.screenshot))
                        screenshot_paths.append(tmp.name)
                        caption_texts.append(_extract_caption_from_item(item, i))
                except Exception as e:
                    logger.debug(f"Error saving temp screenshot: {e}")

    return screenshot_paths, caption_texts


def _extract_caption_from_item(item, index: int) -> str:
    """
    Extract caption text from a history item.

    Args:
        item: History item
        index: Item index for fallback caption

    Returns:
        Caption text
    """
    if item.model_output and item.model_output.current_state:
        text = item.model_output.current_state.next_goal
        return f"Step {index+1}: {text}"
    return f"Step {index+1}"


def _add_caption_to_image(img, caption: str):
    """
    Add a caption to an image.

    Args:
        img: PIL Image object
        caption: Caption text to add

    Returns:
        Image with caption added
    """
    from PIL import ImageDraw, ImageFont

    # Create a drawing context
    draw = ImageDraw.Draw(img)

    # Set up font
    try:
        font = ImageFont.truetype("arial.ttf", 16)
    except OSError:
        # Fallback to default font
        font = ImageFont.load_default()

    # Add text caption at bottom
    caption = caption[:100]  # Limit length
    bbox = draw.textbbox((0, 0), caption, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    position = ((img.width - text_width) // 2, img.height - text_height - 10)

    # Add text with background
    text_bg = (
        (img.width - text_width) // 2 - 5,
        img.height - text_height - 15,
        (img.width + text_width) // 2 + 5,
        img.height - 5
    )
    draw.rectangle(text_bg, fill=(0, 0, 0, 128))
    draw.text(position, caption, font=font, fill=(255, 255, 255, 255))

    return img


def _wrap_text(text: str, font, max_width: int) -> List[str]:
    """
    Wrap text to fit within a given width.

    Args:
        text: Text to wrap
        font: Font to use for width calculation
        max_width: Maximum width in pixels

    Returns:
        List of wrapped text lines
    """
    words = text.split()
    lines = []
    current_line = []

    for word in words:
        test_line = ' '.join(current_line + [word])
        # Check if we need PIL's getbbox or getsize based on what's available
        if hasattr(font, "getbbox"):
            # PIL 9.2.0+
            bbox = font.getbbox(test_line)
            width = bbox[2] - bbox[0]
        else:
            # Older PIL
            width = font.getsize(test_line)[0]

        if width <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(' '.join(current_line))
                current_line = [word]
            else:
                # Word is too long, split it
                lines.append(word)
                current_line = []

    if current_line:
        lines.append(' '.join(current_line))

    return lines


def _normalize_output_path(output_path: str) -> str:
    """
    Normalize output path using PathManager if it contains a session ID.

    Args:
        output_path: Original output path

    Returns:
        Normalized path
    """
    import re

    # Extract session ID from path if possible
    session_id = None
    uuid_pattern = r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
    uuid_match = re.search(uuid_pattern, str(output_path))

    if uuid_match:
        session_id = uuid_match.group(1)
        # Normalize output path if it contains a session ID
        from agents.task.path import pm
        output_path = str(pm().normalize_path(output_path, session_id))

    return output_path
