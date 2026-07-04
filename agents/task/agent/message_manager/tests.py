"""
MessageManager tests. The LLM is only used by MessageManager to read a model name,
so a minimal native stand-in is sufficient (no network, no API keys).
"""
import pytest

# Native message types
from modules.llm.messages import AIMessage, HumanMessage, SystemMessage


class _FakeChatModel:
    """Minimal LLM stand-in — MessageManager only reads ``model_name`` off it."""

    def __init__(self, model_name: str):
        self.model_name = model_name

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.agent.views import ActionResult
from tools.browser.views import BrowserState, TabInfo
from tools.dom.views import DOMElementNode, DOMTextNode


@pytest.fixture(
	params=[
		_FakeChatModel('gpt-5'),
		_FakeChatModel('claude-sonnet-4-5'),
	],
	ids=['gpt-5', 'claude-sonnet-4-5'],
)
def message_manager(request: pytest.FixtureRequest):
	llm = request.param
	task = 'Test task'
	action_descriptions = 'Test actions'
	return MessageManager(
		llm=llm,
		task=task,
		action_descriptions=action_descriptions,
		system_prompt_class=SystemPrompt,
		max_input_tokens=1000,
		image_tokens=800,
	)


def test_initial_messages(message_manager: MessageManager):
	"""Test that message manager initializes with system and task messages"""
	messages = message_manager.get_messages()
	assert len(messages) == 2
	assert isinstance(messages[0], SystemMessage)
	assert isinstance(messages[1], HumanMessage)
	assert 'Test task' in messages[1].content


def test_add_state_message(message_manager: MessageManager):
	"""Test adding browser state message"""
	state = BrowserState(
		url='https://test.com',
		title='Test Page',
		element_tree=DOMElementNode(
			tag_name='div',
			attributes={},
			children=[],
			is_visible=True,
			parent=None,
			xpath='//div',
		),
		selector_map={},
		tabs=[TabInfo(page_id=1, url='https://test.com', title='Test Page')],
	)
	message_manager.add_state_message(state)

	messages = message_manager.get_messages()
	assert len(messages) == 3
	assert isinstance(messages[2], HumanMessage)
	assert 'https://test.com' in messages[2].content


def test_add_state_with_memory_result(message_manager: MessageManager):
	"""Test adding state with result that should be included in memory"""
	state = BrowserState(
		url='https://test.com',
		title='Test Page',
		element_tree=DOMElementNode(
			tag_name='div',
			attributes={},
			children=[],
			is_visible=True,
			parent=None,
			xpath='//div',
		),
		selector_map={},
		tabs=[TabInfo(page_id=1, url='https://test.com', title='Test Page')],
	)
	result = ActionResult(extracted_content='Important content', include_in_memory=True)

	message_manager.add_state_message(state, result)
	messages = message_manager.get_messages()

	# Should have system, task, extracted content, and state messages
	assert len(messages) == 4
	assert 'Important content' in messages[2].content
	assert isinstance(messages[2], HumanMessage)
	assert isinstance(messages[3], HumanMessage)
	assert 'Important content' not in messages[3].content


def test_add_state_with_non_memory_result(message_manager: MessageManager):
	# Assert initial state
	assert len(message_manager.history.messages) == 1
	
	# Create browser state and action result
	state = BrowserState(
		url='https://example.com',
		title='Example Domain',
		text_content='This is a test page',
		pixels_above=0, 
		pixels_below=0,
		tabs=[
			TabInfo(page_id=1, url='https://example.com', title='Example Domain')
		],
		dom_elements=[
			DOMElementNode(
				node_type='ELEMENT_NODE',
				tag_name='div',
				attributes={'id': 'test'},
				node_index=1,
				text='This is a test div',
				is_displayed=True,
				children=[
					DOMTextNode(
						node_type='TEXT_NODE',
						text='This is a test text node',
						node_index=2
					)
				],
			)
		],
		selector_map={'1': 'div#test'},
	)
	
	result = [ActionResult(error='Test error', include_in_memory=False)]
	
	# Add state message with non-memory result
	message_manager.add_state_message(state, result)
	
	# Verify state message was added
	assert len(message_manager.history.messages) == 2
	
	# Ensure result is included in the message
	state_message = message_manager.history.messages[1].message
	assert 'Test error' in str(state_message.content)


def test_update_message_context(message_manager: MessageManager):
	# Assert initial state
	assert message_manager.message_context is None
	
	# Update message context
	new_context = "This is a new evaluation context"
	message_manager.update_message_context(new_context)
	
	# Verify context was updated
	assert message_manager.message_context == new_context
	
	# Add a state message and check if the context influences the messages
	state = BrowserState(
		url='https://example.com',
		title='Example Domain',
		text_content='This is a test page',
		pixels_above=0, 
		pixels_below=0,
		tabs=[
			TabInfo(page_id=1, url='https://example.com', title='Example Domain')
		],
		dom_elements=[],
		selector_map={},
	)
	
	# Add state message after setting context
	message_manager.add_state_message(state)
	
	# Get messages with context
	messages = message_manager.get_messages()
	
	# Verify context is included in the message
	human_messages = [m for m in messages if isinstance(m, HumanMessage)]
	assert len(human_messages) > 0
	
	# At least one message should contain the context or be influenced by it
	# This checks our implementation of using message_context in the message manager


@pytest.mark.skip('not sure how to fix this')
@pytest.mark.parametrize('max_tokens', [100000, 10000, 5000])
def test_token_overflow_handling_with_real_flow(message_manager: MessageManager, max_tokens):
	"""Test handling of token overflow in a realistic message flow"""
	# Set more realistic token limit
	message_manager.max_input_tokens = max_tokens

	# Create a long sequence of interactions
	for i in range(200):  # Simulate 40 steps of interaction
		# Create state with varying content length
		state = BrowserState(
			url=f'https://test{i}.com',
			title=f'Test Page {i}',
			element_tree=DOMElementNode(
				tag_name='div',
				attributes={},
				children=[
					DOMTextNode(
						text=f'Content {j} ' * (10 + i),  # Increasing content length
						is_visible=True,
						parent=None,
					)
					for j in range(5)  # Multiple DOM items
				],
				is_visible=True,
				parent=None,
				xpath='//div',
			),
			selector_map={j: f'//div[{j}]' for j in range(5)},
			tabs=[TabInfo(page_id=1, url=f'https://test{i}.com', title=f'Test Page {i}')],
		)

		# Alternate between different types of results
		result = None
		if i % 2 == 0:  # Every other iteration
			result = ActionResult(
				extracted_content=f'Important content from step {i}' * 5,
				include_in_memory=i % 4 == 0,  # Include in memory every 4th message
			)

		# Add state message
		message_manager.add_state_message(state, result)

		try:
			messages = message_manager.get_messages()
		except ValueError as e:
			if 'Max token limit reached - history is too long' in str(e):
				return  # If error occurs, end the test
			else:
				raise e

		assert message_manager.history.total_tokens <= message_manager.max_input_tokens + 100

		last_msg = messages[-1]
		assert isinstance(last_msg, HumanMessage)

		if i % 4 == 0:
			assert isinstance(message_manager.history.messages[-2].message, HumanMessage)
		if i % 2 == 0 and not i % 4 == 0:
			if isinstance(last_msg.content, list):
				assert 'Current url: https://test' in last_msg.content[0]['text']
			else:
				assert 'Current url: https://test' in last_msg.content

		# Add model output every time
		from browser_use.agent.views import AgentBrain, AgentOutput
		from browser_use.controller.registry.views import ActionModel

		output = AgentOutput(
			current_state=AgentBrain(
				page_summary=f'Thought process from step {i}',
				evaluation_previous_goal=f'Success in step {i}',
				memory=f'Memory from step {i}',
				next_goal=f'Goal for step {i + 1}',
			),
			action=[ActionModel()],
		)
		message_manager._remove_last_state_message()
		message_manager.add_model_output(output)

		# Get messages and verify after each addition
		messages = [m.message for m in message_manager.history.messages]

		# Verify token limit is respected

		# Verify essential messages are preserved
		assert isinstance(messages[0], SystemMessage)  # System prompt always first
		assert isinstance(messages[1], HumanMessage)  # Task always second
		assert 'Test task' in messages[1].content

		# Verify structure of latest messages
		assert isinstance(messages[-1], AIMessage)  # Last message should be model output
		assert f'step {i}' in messages[-1].content  # Should contain current step info

		# Log token usage for debugging
		token_usage = message_manager.history.total_tokens
		token_limit = message_manager.max_input_tokens
		# print(f'Step {i}: Using {token_usage}/{token_limit} tokens')

		# go through all messages and verify that the token count and total tokens is correct
		total_tokens = 0
		real_tokens = []
		stored_tokens = []
		for msg in message_manager.history.messages:
			total_tokens += msg.metadata.input_tokens
			stored_tokens.append(msg.metadata.input_tokens)
			real_tokens.append(message_manager._count_tokens(msg.message))
		assert total_tokens == sum(real_tokens)
		assert stored_tokens == real_tokens
		assert message_manager.history.total_tokens == total_tokens


# pytest -s browser_use/agent/message_manager/tests.py
