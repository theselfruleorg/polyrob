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


def test_file_offloading_enabled_and_gated_by_size():
    """Verify the current file-offloading contract.

    TEST-DRIFT FIX: this test previously asserted file offloading was DISABLED
    and that `_handle_large_action_results` was a no-op. Production intentionally
    RE-ENABLED offloading for non-browser large content (RobustParseConfig comment
    dated Nov 6, 2025): browser tools now return compact accessibility snapshots so
    they don't trip the size gate, but filesystem/MCP/large-dataset reads do get
    offloaded to files. `STORE_LARGE_CONTENT_AS_FILES` now defaults to "true" and
    `_handle_large_action_results` is a real, active offloader (not a no-op).

    The old test also never exercised production — its `MockAgent` shadowed the
    method with a `return None` stub. We now assert the real public contract:
    offloading is enabled by default and the decision is gated purely by size.
    """
    from agents.task.robust_parse_config import RobustParseConfig

    # Offloading is enabled by default (was re-enabled Nov 6, 2025).
    assert RobustParseConfig.STORE_LARGE_CONTENT_AS_FILES is True, \
        "File offloading should be ENABLED by default in the current contract"

    threshold = RobustParseConfig.MAX_EXTRACTED_CONTENT_LENGTH

    # Small content stays in memory (not offloaded).
    small_content = "x" * 100
    assert RobustParseConfig.should_store_content_as_file(small_content) is False, \
        "Small content should NOT be offloaded to a file"

    # Content above the threshold is flagged for file offloading.
    large_content = "x" * (threshold + 1)
    assert RobustParseConfig.should_store_content_as_file(large_content) is True, \
        "Content larger than the threshold should be offloaded to a file"

    # Content exactly at the threshold is NOT offloaded (strict greater-than gate).
    boundary_content = "x" * threshold
    assert RobustParseConfig.should_store_content_as_file(boundary_content) is False, \
        "Content at exactly the threshold should not be offloaded (strict > gate)"


if __name__ == "__main__":
    # Run tests
    print("=" * 60)
    print("Testing Accessibility Snapshot Implementation")
    print("=" * 60)

    print("\nTest 1: Navigation returns accessibility snapshot")
    print("-" * 60)
    asyncio.run(test_navigation_returns_accessibility_snapshot())

    print("\n" + "=" * 60)
    print("\nTest 2: File offloading is disabled")
    print("-" * 60)
    asyncio.run(test_file_offloading_disabled())

    print("\n" + "=" * 60)
    print("\n✅ All tests passed!")
    print("=" * 60)
