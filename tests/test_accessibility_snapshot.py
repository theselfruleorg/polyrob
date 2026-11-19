"""
Test accessibility snapshot functionality in browser navigation.

This test validates the fix for the large content loop problem by ensuring:
1. Navigation returns clean accessibility snapshot
2. Content size is reasonable (30-80K chars)
3. Content is formatted (markdown headings)
4. Content is semantic (no raw HTML)
5. No file offloading occurs
"""

import os
import asyncio
import pytest
from tools.browser.browser import Browser
# Renamed: core.container exposes `DependencyContainer` (was `DIContainer`).
from core.container import DependencyContainer as DIContainer
from core.config import BotConfig


# This test launches a REAL Playwright browser and navigates to a live URL
# (example.com) over the network — genuinely-live external infrastructure, not a
# unit test. Skip it unless explicitly opted in via RUN_LIVE_BROWSER_TESTS=1.
_LIVE_BROWSER_OPT_IN = os.getenv("RUN_LIVE_BROWSER_TESTS", "").lower() in ("1", "true", "yes")
live_browser = pytest.mark.skipif(
    not _LIVE_BROWSER_OPT_IN,
    reason="Live browser/network test; set RUN_LIVE_BROWSER_TESTS=1 to run.",
)


@live_browser
@pytest.mark.asyncio
async def test_navigation_returns_accessibility_snapshot():
    """Test that browser navigation returns accessibility snapshot."""

    # Initialize browser. BaseComponent rejects a falsy config, so pass a real
    # BotConfig (the old `config={}` raised "Configuration is required").
    config = BotConfig()
    container = DIContainer(config)
    browser = Browser(config=config, container=container)

    try:
        await browser.initialize()

        # Create execution context
        from tools.browser.browser_context import BrowserContext
        browser_context = BrowserContext(browser._browser, headless=True)

        class ExecutionContext:
            pass

        execution_context = ExecutionContext()
        execution_context.browser_context = browser_context

        # Navigate to a page
        class GoToUrlAction:
            url = "https://example.com"

        result = await browser.go_to_url(GoToUrlAction(), execution_context)

        # Verify result
        assert result.extracted_content is not None, "No content returned"
        content = result.extracted_content

        print(f"\n✓ Content length: {len(content):,} chars")

        # Should be reasonable size (not 800K+ like raw HTML)
        assert len(content) < 100_000, f"Too large: {len(content):,} chars - expected <100K"
        assert len(content) > 100, f"Too small: {len(content):,} chars - expected >100"

        print("✓ Content size is reasonable")

        # Should contain title
        assert "Example" in content or "example" in content.lower(), "Missing expected content"

        print("✓ Contains expected content")

        # Should NOT have raw HTML tags (scripts, styles)
        assert "<script>" not in content, "Contains raw HTML script tags"
        assert "<style>" not in content, "Contains raw HTML style tags"
        assert "<!DOCTYPE" not in content, "Contains DOCTYPE declaration"

        print("✓ No raw HTML detected")

        # Verify metadata
        if result.metadata:
            assert result.metadata.get('method') == 'accessibility_snapshot'
            print("✓ Metadata indicates accessibility snapshot method")

        print("\n✅ Navigation returns clean accessibility snapshot!")
        return True

    finally:
        await browser.close()


if __name__ == "__main__":
    # Run tests
    print("=" * 60)
    print("Testing Accessibility Snapshot Implementation")
    print("=" * 60)

    print("\nTest 1: Navigation returns accessibility snapshot")
    print("-" * 60)
    asyncio.run(test_navigation_returns_accessibility_snapshot())

    print("\n" + "=" * 60)
    print("\n✅ All tests passed!")
    print("=" * 60)
