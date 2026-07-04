import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import logging
import platform
from typing import List, Dict, Any, Optional

# Native types
from modules.llm.messages import AIMessage
from modules.llm.adapters import BaseChatModel

from agents.task.agent.views import (
	ActionResult,
	AgentBrain,
	AgentHistory,
	AgentHistoryList,
	AgentOutput,
)
from agents.task.agent.views import BrowserState, BrowserStateHistory, TabInfo
from tools.controller.registry.service import Registry
from tools.browser.actions import ClickElementAction, ExtractPageContentAction
from tools.controller.views import DoneAction
from tools.dom.views import DOMElementNode
from agents.task.agent.service import Agent
from tools.controller.registry.views import ActionRegistry
from agents.task.utils import log_llm_request


@pytest.fixture
def sample_browser_state():
	return BrowserState(
		url='https://example.com',
		title='Example Page',
		tabs=[TabInfo(url='https://example.com', title='Example Page', page_id=1)],
		screenshot='screenshot1.png',
		element_tree=DOMElementNode(
			tag_name='root',
			is_visible=True,
			parent=None,
			xpath='',
			attributes={},
			children=[],
		),
		selector_map={},
	)


@pytest.fixture
def action_registry():
	registry = Registry()

	# Register the actions we need for testing
	@registry.action(description='Click an element', param_model=ClickElementAction)
	def click_element(params: ClickElementAction, browser=None):
		pass

	@registry.action(
		description='Extract page content',
		param_model=ExtractPageContentAction,
	)
	def extract_page_content(params: ExtractPageContentAction, browser=None):
		pass

	@registry.action(description='Mark task as done', param_model=DoneAction)
	def done(params: DoneAction):
		pass

	# Create the dynamic ActionModel with all registered actions
	return registry.create_action_model()


@pytest.fixture
def sample_history(action_registry):
	# Create actions with nested params structure
	click_action = action_registry(click_element={'index': 1})

	extract_action = action_registry(extract_page_content={'value': 'text'})

	done_action = action_registry(done={'text': 'Task completed'})

	histories = [
		AgentHistory(
			model_output=AgentOutput(
				current_state=AgentBrain(
					page_summary='I need to find the founders of browser-use',
					evaluation_previous_goal='None',
					memory='Started task',
					next_goal='Click button',
				),
				action=[click_action],
			),
			result=[ActionResult(is_done=False)],
			state=BrowserStateHistory(
				url='https://example.com',
				title='Page 1',
				tabs=[TabInfo(url='https://example.com', title='Page 1', page_id=1)],
				screenshot='screenshot1.png',
				interacted_element=[{'xpath': '//button[1]'}],
			),
		),
		AgentHistory(
			model_output=AgentOutput(
				current_state=AgentBrain(
					page_summary="This is a sample page summary.",
					evaluation_previous_goal='Clicked button',
					memory='Button clicked',
					next_goal='Extract content',
				),
				action=[extract_action],
			),
			result=[
				ActionResult(
					is_done=False,
					extracted_content='Extracted text',
					error='Failed to extract completely',
				)
			],
			state=BrowserStateHistory(
				url='https://example.com/page2',
				title='Page 2',
				tabs=[TabInfo(url='https://example.com/page2', title='Page 2', page_id=2)],
				screenshot='screenshot2.png',
				interacted_element=[{'xpath': '//div[1]'}],
			),
		),
		AgentHistory(
			model_output=AgentOutput(
				current_state=AgentBrain(
					page_summary='I found out that the founders are John Doe and Jane Smith. I need to draft them a message.',
					evaluation_previous_goal='Extracted content',
					memory='Content extracted',
					next_goal='Finish task',
				),
				action=[done_action],
			),
			result=[ActionResult(is_done=True, extracted_content='Task completed', error=None)],
			state=BrowserStateHistory(
				url='https://example.com/page2',
				title='Page 2',
				tabs=[TabInfo(url='https://example.com/page2', title='Page 2', page_id=2)],
				screenshot='screenshot3.png',
				interacted_element=[{'xpath': '//div[1]'}],
			),
		),
	]
	return AgentHistoryList(history=histories)


def test_last_model_output(sample_history: AgentHistoryList):
	last_output = sample_history.last_action()
	print(last_output)
	assert last_output == {'done': {'text': 'Task completed'}}


def test_get_errors(sample_history: AgentHistoryList):
	errors = sample_history.errors()
	assert len(errors) == 1
	assert errors[0] == 'Failed to extract completely'


def test_final_result(sample_history: AgentHistoryList):
	assert sample_history.final_result() == 'Task completed'


def test_is_done(sample_history: AgentHistoryList):
	assert sample_history.is_done() == True


def test_urls(sample_history: AgentHistoryList):
	urls = sample_history.urls()
	assert 'https://example.com' in urls
	assert 'https://example.com/page2' in urls


def test_all_screenshots(sample_history: AgentHistoryList):
	screenshots = sample_history.screenshots()
	assert len(screenshots) == 3
	assert screenshots == ['screenshot1.png', 'screenshot2.png', 'screenshot3.png']


def test_all_model_outputs(sample_history: AgentHistoryList):
	outputs = sample_history.model_actions()
	print(f"DEBUG: {outputs[0]}")
	assert len(outputs) == 3
	# get first key value pair
	assert dict([next(iter(outputs[0].items()))]) == {'click_element': {'index': 1}}
	assert dict([next(iter(outputs[1].items()))])  == {'extract_page_content': {'value': 'text'}}
	assert dict([next(iter(outputs[2].items()))])  == {'done': {'text': 'Task completed'}}


def test_all_model_outputs_filtered(sample_history: AgentHistoryList):
	filtered = sample_history.model_actions_filtered(include=['click_element'])
	assert len(filtered) == 1
	assert filtered[0]['click_element']['index'] == 1


def test_empty_history():
	history = AgentHistoryList(history=[])
	assert history.last_action() is None
	assert history.errors() == []
	assert history.final_result() is None
	assert history.is_done() is False
	assert history.urls() == []
	assert history.screenshots() == []


# Add a test to verify action creation
def test_action_creation(action_registry):
	# Test that the registry has the registered actions
	registry = action_registry
	assert 'click_element' in registry.actions
	assert 'done' in registry.actions
	assert 'extract_page_content' in registry.actions
	
	# Test that we can create action instances directly
	action = ClickElementAction(element=1)
	assert isinstance(action, ClickElementAction)
	assert action.element == 1


def test_agent_basic_initialization():
    """Test basic agent initialization"""
    # Create a mock LLM for testing
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="Test response"))
    
    # Create a basic agent
    agent = Agent.from_params(
        task="Test task",
        llm=mock_llm,
    )
    
    # Verify agent is initialized correctly
    assert agent.task == "Test task"
    assert agent.llm == mock_llm


class TestLLMLogging:
    @pytest.mark.asyncio
    async def test_log_llm_request_decorator(self, caplog):
        """Test the log_llm_request decorator properly logs LLM requests"""
        caplog.set_level(logging.INFO)
        
        # Create a mock LLM
        mock_llm = MagicMock()
        mock_llm.model_name = "test-model"
        mock_llm.__class__.__name__ = "MockLLM"
        mock_llm.temperature = 0.7
        mock_llm.max_tokens = 1000
        
        # Create a function to decorate
        @log_llm_request(component='test', purpose='unit_test')
        async def test_llm_function(llm):
            # Mock a successful response
            return {"raw": MagicMock(usage=MagicMock(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150
            ))}
        
        # Execute the function
        result = await test_llm_function(mock_llm)
        
        # Check logs for expected messages
        expected_log_fragments = [
            "🔄 LLM REQUEST [test/unit_test] - Model: test-model (MockLLM)",
            "Token usage",
            "Completed in"
        ]
        
        for fragment in expected_log_fragments:
            assert any(fragment in record.message for record in caplog.records)
            
        # Test with error handling
        caplog.clear()
        
        @log_llm_request(component='test', purpose='error_test')
        async def test_error_function(llm):
            raise ValueError("Test error")
        
        # Execute and expect error
        with pytest.raises(ValueError):
            await test_error_function(mock_llm)
            
        # Check logs for error message
        error_fragments = [
            "🔄 LLM REQUEST [test/error_test]",
            "Failed after",
            "Test error"
        ]
        
        for fragment in error_fragments:
            assert any(fragment in record.message for record in caplog.records)


class TestLLMLoggingWithTelemetry:
    @pytest.mark.asyncio
    async def test_log_llm_request_with_telemetry(self, caplog, monkeypatch):
        """Test the log_llm_request decorator with telemetry integration"""
        caplog.set_level(logging.INFO)
        
        # Mock the telemetry capture function
        mock_capture = MagicMock()
        monkeypatch.setattr("agents.task.capture_llm_request", mock_capture)
        
        # Create a mock LLM
        mock_llm = MagicMock()
        mock_llm.model_name = "test-model"
        mock_llm.__class__.__name__ = "MockLLM"
        mock_llm.temperature = 0.7
        mock_llm.max_tokens = 1000
        
        # Create a mock agent with session ID
        mock_agent = MagicMock()
        mock_agent.agent_id = "test-session"
        
        # Create a function to decorate
        @log_llm_request(component='test', purpose='unit_test')
        async def test_llm_function(agent, llm):
            # Mock a successful response
            return {"raw": MagicMock(usage=MagicMock(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150
            ))}
        
        # Execute the function
        result = await test_llm_function(mock_agent, mock_llm)
        
        # Check logs for expected messages
        expected_log_fragments = [
            "🔄 LLM REQUEST [test/unit_test] - Model: test-model",
            "Token usage",
            "Completed in"
        ]
        
        for fragment in expected_log_fragments:
            assert any(fragment in record.message for record in caplog.records)
            
        # Check that telemetry was called with correct parameters
        mock_capture.assert_called_once()
        args, kwargs = mock_capture.call_args
        
        # Check the arguments
        assert kwargs["component"] == "test"
        assert kwargs["purpose"] == "unit_test"
        assert kwargs["model_name"] == "test-model"
        assert kwargs["success"] is True
        assert kwargs["token_count"] == 150
        assert kwargs["session_id"] == "test-session"


class TestSessionManager:
    @pytest.mark.asyncio
    async def test_session_manager_paths(self, tmpdir):
        """Test the SessionManager's path handling and directory creation capabilities."""
        import os
        import uuid
        from pathlib import Path
        from agents.task.agent.session import SessionManager
        
        # Create a test session manager with a temporary base directory
        test_base_dir = os.path.join(tmpdir, "sessions")
        session_manager = SessionManager(base_dir=test_base_dir)
        
        # Create a session ID
        session_id = str(uuid.uuid4())
        
        # Test initialize_session_directories
        session_dir = os.path.join(test_base_dir, session_id)
        dirs = session_manager.initialize_session_directories(session_dir)
        
        # Verify all standard directories were created
        assert 'screenshots' in dirs
        assert 'feed' in dirs
        assert 'logs' in dirs
        assert 'data' in dirs
        assert 'workspace' in dirs
        assert 'telemetry' in dirs
        assert os.path.exists(dirs['screenshots'])
        assert os.path.exists(dirs['feed'])
        assert os.path.exists(dirs['logs'])
        assert os.path.exists(dirs['data'])
        assert os.path.exists(dirs['workspace'])
        assert os.path.exists(dirs['telemetry'])
        
        # Test path normalization with duplicated segments
        duplicate_path = os.path.join(test_base_dir, session_id, session_id, "screenshots")
        from agents.task.path import pm
        normalized_path = pm().normalize_path(duplicate_path, session_id)
        
        # Check that duplicate session_id segments are removed
        assert normalized_path.count(session_id) == 1
        
        # Test get_subdirectory
        screenshots_dir = session_manager.get_subdirectory(session_id, "screenshots")
        assert os.path.exists(str(screenshots_dir))
        
        # Test create_file_path using PathManager directly
        file_path = str(pm().create_file_path(session_id, "screenshots", "test.png"))
        assert session_id in file_path
        assert "screenshots" in file_path
        assert file_path.endswith("test.png")
        
        # Verify the session is registered in the manager
        assert session_id in session_manager.sessions

    def test_clean_session_id(self):
        """Test clean_session_id function with various formats"""
        from agents.task.path import pm
        
        # Test cases and expected results
        test_cases = [
            # Simple UUID
            ("12345678-1234-1234-1234-123456789abc", "12345678-1234-1234-1234-123456789abc"),
            # UUID with agent prefix
            ("agent_12345678-1234-1234-1234-123456789abc", "12345678-1234-1234-1234-123456789abc"),
            # Path with UUID
            ("/data/task/_anonymous_/12345678-1234-1234-1234-123456789abc/file.txt", "12345678-1234-1234-1234-123456789abc"),
            # Path with nested session directories
            ("/data/task/_anonymous_/12345678-1234-1234-1234-123456789abc/data/task/12345678-1234-1234-1234-123456789abc/screenshots",
             "12345678-1234-1234-1234-123456789abc"),
            # Windows-style path
            (r"C:\data\task\_anonymous_\12345678-1234-1234-1234-123456789abc\screenshots", "12345678-1234-1234-1234-123456789abc"),
            # Invalid inputs
            (None, None),
            ("", ""),
            ("not-a-uuid", "notauuid"),  # Non-alphanumeric chars removed
        ]
        
        for input_id, expected in test_cases:
            result = pm().clean_session_id(input_id)
            assert result == expected, f"Failed for {input_id}, got {result} instead of {expected}"
    
    def test_normalize_path(self):
        """Test normalize_path function with various path formats"""
        import os
        from agents.task.path import pm
        
        session_id = "12345678-1234-1234-1234-123456789abc"
        
        # Test cases and expected results (using forward slashes for consistency)
        test_cases = [
            # Simple path
            ("data/task/_anonymous_/"+session_id+"/screenshots", "data/task/_anonymous_/"+session_id+"/screenshots"),
            # Duplicate session ID in path
            ("data/task/_anonymous_/"+session_id+"/"+session_id+"/screenshots", "data/task/_anonymous_/"+session_id+"/screenshots"),
            # Nested data/task paths
            ("data/task/data/task/_anonymous_/"+session_id+"/screenshots", "data/task/_anonymous_/"+session_id+"/screenshots"),
            # Empty path
            ("", ""),
            # Paths with different session IDs
            ("data/task/_anonymous_/session1/data/task/_anonymous_/"+session_id+"/screenshots", "data/task/_anonymous_/"+session_id+"/screenshots"),
            # Path with duplicate segments
            ("data/task/_anonymous_/"+session_id+"/data/task/_anonymous_/"+session_id+"/data/task/_anonymous_/"+session_id+"/screenshots",
             "data/task/_anonymous_/"+session_id+"/screenshots"),
        ]
        
        for input_path, expected in test_cases:
            # Convert expected to OS-specific path separators for comparison
            expected_os_path = expected.replace('/', os.sep)
            result = pm().normalize_path(input_path, session_id)
            assert result == expected_os_path, f"Failed for {input_path}, got {result} instead of {expected_os_path}"
    
    def test_agent_registration(self, tmpdir):
        """Test agent registration with SessionManager"""
        import os
        import uuid
        from agents.task.agent.session import SessionManager
        
        # Create test session manager
        test_base_dir = os.path.join(tmpdir, "sessions")
        session_manager = SessionManager(base_dir=test_base_dir)
        
        # Create a session
        session_id = str(uuid.uuid4())
        created_session_id = session_manager.create_session(session_id)
        
        # Verify session was created
        assert created_session_id in session_manager.sessions
        assert session_manager.sessions[created_session_id]["status"] == "active"
        
        # Register an agent
        agent_id = "test_agent_"+created_session_id
        session_manager.register_agent(
            session_id=created_session_id,
            agent_id=agent_id,
            agent_name="test_agent",
            agent_type="executor"
        )
        
        # Verify agent was registered
        agents = session_manager.get_session_agents(created_session_id)
        assert agent_id in agents
        
        # Get agent info
        agent_info = session_manager.get_agent_info(created_session_id, agent_id)
        assert agent_info is not None
        assert agent_info["agent_name"] == "test_agent"
        assert agent_info["agent_type"] == "executor"
        
        # Verify session path is correct
        session_dir = session_manager.get_session_dir(created_session_id)
        assert os.path.exists(str(session_dir))
        
        # Test session status update
        session_manager.update_session_status(created_session_id, "running")
        session_info = session_manager.get_session_info(created_session_id)
        assert session_info["status"] == "running"


# run this with:
# pytest browser_use/agent/tests.py
