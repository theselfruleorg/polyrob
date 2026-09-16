"""Playwright browser utilities for checking installation and managing browsers."""

import subprocess
import sys
import logging
from pathlib import Path
from typing import Tuple, Optional

from tools.browser.child_env import build_browser_env

logger = logging.getLogger(__name__)


def check_playwright_browsers() -> bool:
    """Check if Playwright browsers are installed.
    
    Returns:
        Boolean indicating if browsers are installed
    """
    try:
        # Use playwright's own check mechanism  
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            # S2 (2026-09-14): an implicit inherit handed the Playwright CLI the
            # whole agent environment (wallet seed + every API key). It needs
            # PATH/HOME/PLAYWRIGHT_* and nothing else.
            env=build_browser_env(),
        )
        
        # If dry-run succeeds, browsers are likely installed
        if result.returncode == 0:
            logger.info("Playwright browsers appear to be installed")
            return True
        
        # Alternative check: Look for browser directories
        home = Path.home()
        cache_dir = home / ".cache" / "ms-playwright"
        
        if cache_dir.exists():
            # Check for chromium installation
            chromium_dirs = list(cache_dir.glob("chromium-*"))
            if chromium_dirs:
                logger.info(f"Found Chromium at: {cache_dir}")
                return True
        
        # Check other standard paths
        alternatives = [
            Path("/opt/rob/.cache/ms-playwright"),  # Docker container path
            Path("/usr/local/share/ms-playwright"),  # System install path
        ]
        
        for alt_path in alternatives:
            if alt_path.exists():
                # Check for any chromium installation
                chromium_dirs = list(alt_path.glob("chromium-*"))
                if chromium_dirs:
                    logger.info(f"Found Chromium at alternative path: {alt_path}")
                    return True
        
        logger.warning("Playwright browsers not found")
        return False
        
    except FileNotFoundError:
        logger.warning("Playwright CLI not found - browsers not installed")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("Playwright browser check timed out")
        return False
    except Exception as e:
        logger.error(f"Error checking for Playwright browsers: {e}")
        return False


def install_playwright_browsers(with_deps: bool = False) -> Tuple[bool, Optional[str]]:
    """Install Playwright browsers.
    
    Args:
        with_deps: Whether to install system dependencies as well
        
    Returns:
        Tuple of (success, error_message)
    """
    try:
        logger.info("Installing Playwright browsers")
        
        # Build command
        cmd = [sys.executable, "-m", "playwright", "install"]
        if with_deps:
            cmd.append("--with-deps")
        cmd.append("chromium")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,  # 2 minute timeout for installation
            env=build_browser_env(),  # S2: scrubbed, never the agent's own environment
        )
        
        if result.returncode == 0:
            logger.info("✅ Playwright browsers installed successfully")
            return True, None
        else:
            error_msg = f"Failed to install Playwright browsers: {result.stderr}"
            logger.error(error_msg)
            return False, error_msg
            
    except subprocess.TimeoutExpired:
        error_msg = "Playwright browser installation timed out"
        logger.error(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"Error installing Playwright browsers: {e}"
        logger.error(error_msg)
        return False, error_msg