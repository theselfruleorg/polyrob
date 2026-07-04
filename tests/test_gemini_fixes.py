"""Unit tests for Gemini integration fixes.

Tests all 6 issues identified in GEMINI_UPGRADE_INSTRUCTIONS.md:
- Issue #1: Non-unique tool call IDs
- Issue #2: Arguments format (dict vs JSON string)
- Issue #3: Function call validation (covered by #2)
- Issue #4: Usage tracking never returns None
- Issue #5: System message native support
- Issue #6: Tool schema validation

NOTE: These tests use mocking to avoid circular import issues.
"""

import pytest
import sys
import os
from contextlib import contextmanager
from unittest.mock import Mock, patch, MagicMock, AsyncMock

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE (test-drift fix): the old `sys.modules[...] = Mock()` block stubbed out
# `core.config`, `core.exceptions`, `modules.llm.llm_client` and
# `modules.llm.token_counter` "to avoid circular imports". That is now actively
# harmful: making `LLMClient` a Mock means `GeminiClient(LLMClient)` subclasses a
# Mock, so `GeminiClient._generate_with_tools` resolves to a Mock attribute (not
# the real `async def`) — awaiting it raised "object Mock can't be used in 'await'
# expression". The real modules import cleanly (no circular import), so we import
# them for real and only mock the Gemini SDK (`genai`) inside the fixture.


# NOTE (test-drift fix): production no longer calls `asyncio.to_thread(...)` to
# reach the Gemini SDK. `_generate_with_tools` now `await`s the async SDK method
# `model.generate_content_async(...)` (wrapped in `asyncio.wait_for`). The old
# `patch('...asyncio.to_thread', return_value=mock_response)` therefore patched a
# dead call path AND returned a non-awaitable Mock. This helper patches the real
# async seam — `genai.GenerativeModel` → instance.generate_content_async — to
# return `mock_response`, preserving each test's intent.
@contextmanager
def patch_gemini_generate(mock_response, model_class_mock=None):
    """Patch the Gemini async generation seam to yield `mock_response`."""
    model_instance = Mock()
    model_instance.generate_content_async = AsyncMock(return_value=mock_response)
    if model_class_mock is None:
        with patch('modules.llm.gemini_client.genai.GenerativeModel') as model_class:
            model_class.return_value = model_instance
            yield model_class
    else:
        model_class_mock.return_value = model_instance
        yield model_class_mock


@pytest.fixture
def mock_config():
    """Create mock config for testing."""
    config = Mock()  # core.config is mocked above; a plain mock config suffices
    config.get_llm_config.return_value = {
        'gemini': {
            'model': 'gemini-2.0-flash',
            'api_key': 'test_key_12345'
        }
    }
    return config


# NOTE: this fixture builds a plain object (no `await`), so it is a sync
# fixture. Under pytest-asyncio strict mode a plain `@pytest.fixture` on an
# `async def` would yield an un-awaited coroutine instead of the client
# (causing `'coroutine' object has no attribute ...`). Keep it `def`.
@pytest.fixture
def gemini_client(mock_config):
    """Create and initialize Gemini client with mocked API."""
    from modules.llm.gemini_client import GeminiClient
    with patch('modules.llm.gemini_client.genai'):
        client = GeminiClient(mock_config)
        # Skip actual API initialization
        client._initialized = True
        return client


@pytest.mark.asyncio
async def test_issue_1_unique_tool_call_ids(gemini_client):
    """Test Issue #1: Tool call IDs are unique across multiple responses.

    Before fix: Used f"function_call_{i}" causing duplicates
    After fix: Uses f"gemini_call_{uuid4()}" for uniqueness
    """
    # Mock Gemini API response with tool calls
    mock_response = Mock()
    mock_response.candidates = [Mock()]
    mock_response.candidates[0].content = Mock()

    # Create mock tool calls
    mock_function_call_1 = Mock()
    mock_function_call_1.name = "read_file"
    mock_function_call_1.args = {"file_path": "test.txt"}

    mock_function_call_2 = Mock()
    mock_function_call_2.name = "write_file"
    mock_function_call_2.args = {"file_path": "output.txt", "content": "test"}

    mock_part_1 = Mock()
    mock_part_1.function_call = mock_function_call_1

    mock_part_2 = Mock()
    mock_part_2.function_call = mock_function_call_2

    mock_response.candidates[0].content.parts = [mock_part_1, mock_part_2]

    # Mock usage metadata
    mock_response.usage_metadata = Mock()
    mock_response.usage_metadata.prompt_token_count = 100
    mock_response.usage_metadata.candidates_token_count = 50
    mock_response.usage_metadata.total_token_count = 150

    # Simulate two API calls
    with patch_gemini_generate(mock_response):
        # First call
        gemini_client.last_response = mock_response
        content1, tool_calls1, usage1 = await gemini_client._generate_with_tools(
            messages=[{"role": "user", "content": "Test"}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}}
                }
            }]
        )

        # Second call
        gemini_client.last_response = mock_response
        content2, tool_calls2, usage2 = await gemini_client._generate_with_tools(
            messages=[{"role": "user", "content": "Test"}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write a file",
                    "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}}
                }
            }]
        )

    # Extract all IDs
    all_ids = []
    for tc in tool_calls1:
        all_ids.append(tc['id'])
    for tc in tool_calls2:
        all_ids.append(tc['id'])

    # Verify all IDs are unique
    assert len(all_ids) == len(set(all_ids)), f"Duplicate IDs found: {all_ids}"

    # Verify IDs start with "gemini_call_"
    for tc_id in all_ids:
        assert tc_id.startswith("gemini_call_"), f"ID should start with 'gemini_call_', got: {tc_id}"


@pytest.mark.asyncio
async def test_issue_2_arguments_are_dict(gemini_client):
    """Test Issue #2: Tool call arguments are dict, not JSON string.

    Before fix: json.dumps(function_call.args) → string
    After fix: Direct dict extraction from protobuf Struct
    """
    # Mock Gemini API response
    mock_response = Mock()
    mock_response.candidates = [Mock()]
    mock_response.candidates[0].content = Mock()

    # Create mock function call with dict-like args
    mock_function_call = Mock()
    mock_function_call.name = "read_file"

    # Simulate protobuf Struct with .items() method
    mock_args = {"file_path": "test.txt", "encoding": "utf-8"}
    mock_function_call.args = Mock()
    mock_function_call.args.items.return_value = mock_args.items()

    mock_part = Mock()
    mock_part.function_call = mock_function_call

    mock_response.candidates[0].content.parts = [mock_part]

    # Mock usage metadata
    mock_response.usage_metadata = Mock()
    mock_response.usage_metadata.total_token_count = 100

    with patch_gemini_generate(mock_response):
        gemini_client.last_response = mock_response
        content, tool_calls, usage = await gemini_client._generate_with_tools(
            messages=[{"role": "user", "content": "Read file"}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object"}
                }
            }]
        )

    # Verify arguments are dict, not string
    assert len(tool_calls) > 0, "Should have at least one tool call"
    for tc in tool_calls:
        args = tc['function']['arguments']
        assert isinstance(args, dict), f"Arguments should be dict, got {type(args)}"
        assert "file_path" in args, "Arguments should contain file_path"
        assert args["file_path"] == "test.txt", "Arguments should be preserved correctly"


@pytest.mark.asyncio
async def test_issue_4_usage_data_never_none(gemini_client):
    """Test Issue #4: Usage data never returns None values.

    Before fix: Returns {'prompt_tokens': None, ...} when unavailable
    After fix: Returns integers (0 as last resort), uses estimation
    """
    # Test Case 1: No response at all
    gemini_client.last_response = None
    usage = gemini_client._extract_usage_data()

    assert isinstance(usage['prompt_tokens'], int), "prompt_tokens should be int"
    assert isinstance(usage['completion_tokens'], int), "completion_tokens should be int"
    assert isinstance(usage['total_tokens'], int), "total_tokens should be int"
    assert usage['prompt_tokens'] == 0, "Should default to 0 when no response"

    # Test Case 2: Response without usage_metadata but with text
    mock_response = Mock()
    mock_response.usage_metadata = None
    mock_response.text = "This is a test response with approximately 10 tokens in it"
    gemini_client.last_response = mock_response

    usage = gemini_client._extract_usage_data()

    assert isinstance(usage['prompt_tokens'], int), "prompt_tokens should be int"
    assert isinstance(usage['completion_tokens'], int), "completion_tokens should be int"
    assert isinstance(usage['total_tokens'], int), "total_tokens should be int"
    assert usage['total_tokens'] > 0, "Should estimate tokens from content"

    # Test Case 3: Response with usage_metadata
    mock_response = Mock()
    mock_response.usage_metadata = Mock()
    mock_response.usage_metadata.prompt_token_count = 100
    mock_response.usage_metadata.candidates_token_count = 50
    mock_response.usage_metadata.total_token_count = 150
    gemini_client.last_response = mock_response

    usage = gemini_client._extract_usage_data()

    assert usage['prompt_tokens'] == 100
    assert usage['completion_tokens'] == 50
    assert usage['total_tokens'] == 150


@pytest.mark.asyncio
async def test_issue_6_invalid_tool_schema_validation(gemini_client):
    """Test Issue #6: Invalid tool schemas are validated and skipped.

    Before fix: No validation, invalid schemas cause API errors
    After fix: _validate_tool_schema() prevents invalid schemas
    """
    # Test invalid schemas
    invalid_schemas = [
        {"type": "function", "function": {}},  # Missing name
        {"type": "function"},  # Missing function field
        {"function_declarations": []},  # Empty declarations
        {},  # Completely empty
    ]

    for invalid_schema in invalid_schemas:
        result = gemini_client._validate_tool_schema(invalid_schema)
        assert result is False, f"Should reject invalid schema: {invalid_schema}"

    # Test valid schemas
    valid_schemas = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object"}
            }
        },
        {
            "function_declarations": [
                {"name": "test_action", "description": "Test", "parameters": {}}
            ]
        },
        {
            "name": "direct_format",
            "description": "Direct format tool",
            "parameters": {}
        }
    ]

    for valid_schema in valid_schemas:
        result = gemini_client._validate_tool_schema(valid_schema)
        assert result is True, f"Should accept valid schema: {valid_schema}"


@pytest.mark.asyncio
async def test_issue_5_system_instruction_native(gemini_client):
    """Test Issue #5: System messages use native system_instruction parameter.

    Before fix: Injected as user+model message pair (waste tokens)
    After fix: Uses GenerativeModel(system_instruction=...)
    """
    system_message = "You are a helpful assistant. Follow instructions carefully."

    mock_response = Mock()
    mock_response.candidates = [Mock()]
    mock_response.candidates[0].content = Mock()
    # Text-only part (spec restricts attrs so production's `hasattr(part,
    # 'function_call')` check is False -> treated as a plain text response).
    text_part = Mock(spec=['text'])
    text_part.text = "Response text"
    mock_response.candidates[0].content.parts = [text_part]
    mock_response.usage_metadata = Mock()
    mock_response.usage_metadata.total_token_count = 100

    # H1 FIX: native `system_instruction` now applies on BOTH the tool-calling path
    # (`_generate_with_tools`) AND the no-tools `_generate` path. This test exercises
    # the tools path; test_h1_system_instruction_native_no_tools_path covers the
    # no-tools path that previously dropped the system prompt entirely.
    with patch('modules.llm.gemini_client.genai.GenerativeModel') as mock_model_class:
        mock_model_instance = Mock()
        # Production awaits model.generate_content_async(...) (was asyncio.to_thread).
        mock_model_instance.generate_content_async = AsyncMock(return_value=mock_response)
        mock_model_class.return_value = mock_model_instance

        gemini_client.last_response = mock_response
        await gemini_client._generate_with_tools(
            messages=[{"role": "user", "content": "Test"}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "noop",
                    "description": "No-op tool",
                    "parameters": {"type": "object"}
                }
            }],
            system=system_message
        )

        # Verify GenerativeModel was called with system_instruction
        mock_model_class.assert_called_once()
        call_kwargs = mock_model_class.call_args.kwargs

        assert 'system_instruction' in call_kwargs, "Should pass system_instruction parameter"
        assert call_kwargs['system_instruction'] == system_message, "System instruction should match"


@pytest.mark.asyncio
async def test_h1_system_instruction_native_no_tools_path(gemini_client):
    """H1 regression: the no-tools `_generate` path passes the system prompt natively.

    Before the H1 fix, a role='system' message in `messages` (how the agent adapter
    delivers the system prompt) was skipped and never re-added on the no-tools path,
    so EVERY Gemini call without tools silently lost its system prompt. After the
    fix it is captured and passed via GenerativeModel(system_instruction=...).
    """
    system_message = "You are ROB. Emit brain-state as JSON. Follow the contract."

    mock_response = Mock()
    text_part = Mock(spec=['text'])
    text_part.text = "ack"
    mock_response.candidates = [Mock()]
    mock_response.candidates[0].content = Mock()
    mock_response.candidates[0].content.parts = [text_part]
    mock_response.text = "ack"
    mock_response.usage_metadata = Mock()
    mock_response.usage_metadata.total_token_count = 10

    with patch_gemini_generate(mock_response) as mock_model_class:
        # System prompt arrives embedded as a role='system' message (adapter path),
        # NOT via the `system=` kwarg — this is exactly the case that used to drop it.
        await gemini_client._generate(
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": "hi"},
            ],
        )

        mock_model_class.assert_called_once()
        call_kwargs = mock_model_class.call_args.kwargs
        assert call_kwargs.get('system_instruction') == system_message, (
            "no-tools _generate must pass the embedded system prompt via system_instruction"
        )


@pytest.mark.asyncio
async def test_integration_multiple_tool_calls_unique_ids(gemini_client):
    """Integration test: Multiple tool calls in same response have unique IDs."""
    mock_response = Mock()
    mock_response.candidates = [Mock()]
    mock_response.candidates[0].content = Mock()

    # Create multiple tool calls
    tool_calls_data = [
        ("navigate", {"url": "https://example.com"}),
        ("click", {"selector": "#button"}),
        ("extract", {"selector": ".content"})
    ]

    mock_parts = []
    for name, args in tool_calls_data:
        mock_fc = Mock()
        mock_fc.name = name
        mock_fc.args = Mock()
        mock_fc.args.items.return_value = args.items()

        mock_part = Mock()
        mock_part.function_call = mock_fc
        mock_parts.append(mock_part)

    mock_response.candidates[0].content.parts = mock_parts
    mock_response.usage_metadata = Mock()
    mock_response.usage_metadata.total_token_count = 200

    with patch_gemini_generate(mock_response):
        gemini_client.last_response = mock_response
        content, tool_calls, usage = await gemini_client._generate_with_tools(
            messages=[{"role": "user", "content": "Do automation"}],
            tools=[{"type": "function", "function": {"name": "navigate", "parameters": {}}}]
        )

    # Verify all IDs are unique
    ids = [tc['id'] for tc in tool_calls]
    assert len(ids) == len(set(ids)), "All tool call IDs should be unique"
    assert len(ids) == 3, "Should have 3 tool calls"

    # Verify all arguments are dicts
    for tc in tool_calls:
        assert isinstance(tc['function']['arguments'], dict), "All arguments should be dicts"


@pytest.mark.asyncio
async def test_empty_function_call_skipped(gemini_client):
    """Test that function calls with missing names are skipped."""
    mock_response = Mock()
    mock_response.candidates = [Mock()]
    mock_response.candidates[0].content = Mock()

    # Create function call with no name. Production now falls back to alternative
    # name attributes (function_name/tool_name/method) before skipping, so a bare
    # Mock would auto-create a truthy `.function_name` and defeat the skip. Null
    # those alternatives too to represent a genuinely nameless call.
    mock_fc_invalid = Mock()
    mock_fc_invalid.name = None  # Invalid
    mock_fc_invalid.function_name = None
    mock_fc_invalid.tool_name = None
    mock_fc_invalid.method = None
    mock_fc_invalid.args = None

    # Create valid function call
    mock_fc_valid = Mock()
    mock_fc_valid.name = "valid_action"
    mock_fc_valid.args = Mock()
    mock_fc_valid.args.items.return_value = {"key": "value"}.items()

    mock_part_invalid = Mock()
    mock_part_invalid.function_call = mock_fc_invalid

    mock_part_valid = Mock()
    mock_part_valid.function_call = mock_fc_valid

    mock_response.candidates[0].content.parts = [mock_part_invalid, mock_part_valid]
    mock_response.usage_metadata = Mock()
    mock_response.usage_metadata.total_token_count = 50

    with patch_gemini_generate(mock_response):
        gemini_client.last_response = mock_response
        content, tool_calls, usage = await gemini_client._generate_with_tools(
            messages=[{"role": "user", "content": "Test"}],
            tools=[{"type": "function", "function": {"name": "test", "parameters": {}}}]
        )

    # Should only have 1 tool call (the valid one)
    assert len(tool_calls) == 1, "Should skip invalid function call"
    assert tool_calls[0]['function']['name'] == "valid_action", "Should only include valid call"
