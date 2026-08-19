"""AgentHistoryList read accessors.

Salvaged from the in-package `agents/task/agent/tests.py`, which pytest never
collected: the default `python_files` pattern is `test_*.py`/`*_test.py`, so a file
named `tests.py` is invisible to a bare `pytest` run. These assertions had therefore
never gated anything, while the file kept shipping to the public repo. They are the
only direct coverage of the AgentHistoryList accessors, so they move here rather than
being deleted with the rotted remainder of that file.
"""

import pytest

from agents.task.agent.views import (
    ActionResult,
    AgentBrain,
    AgentHistory,
    AgentHistoryList,
    AgentOutput,
    BrowserStateHistory,
    TabInfo,
)
from tools.browser.actions import ClickElementAction, ExtractPageContentAction
from tools.controller.registry.service import Registry
from tools.controller.views import DoneAction


@pytest.fixture
def action_registry():
    registry = Registry()

    @registry.action(description='Click an element', param_model=ClickElementAction)
    def click_element(params: ClickElementAction, browser=None):
        pass

    @registry.action(description='Extract page content', param_model=ExtractPageContentAction)
    def extract_page_content(params: ExtractPageContentAction, browser=None):
        pass

    @registry.action(description='Mark task as done', param_model=DoneAction)
    def done(params: DoneAction):
        pass

    return registry.create_action_model()


@pytest.fixture
def sample_history(action_registry):
    """Three steps: click -> extract (with an error) -> done."""
    click_action = action_registry(click_element={'index': 1})
    # ExtractPageContentAction carries NO fields today. The original fixture passed
    # {'value': 'text'}, which pydantic silently dropped — so the paired assertion had
    # been failing (unnoticed, since the file was never collected).
    extract_action = action_registry(extract_page_content={})
    done_action = action_registry(done={'text': 'Task completed'})

    histories = [
        AgentHistory(
            model_output=AgentOutput(
                current_state=AgentBrain(
                    page_summary='Find the founders',
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
                    page_summary='A sample page summary.',
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
                    page_summary='Founders found; draft them a message.',
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


def test_last_action(sample_history: AgentHistoryList):
    assert sample_history.last_action() == {'done': {'text': 'Task completed'}}


def test_errors_collects_only_failed_results(sample_history: AgentHistoryList):
    errors = sample_history.errors()
    assert errors == ['Failed to extract completely']


def test_final_result(sample_history: AgentHistoryList):
    assert sample_history.final_result() == 'Task completed'


def test_is_done(sample_history: AgentHistoryList):
    assert sample_history.is_done() is True


def test_urls(sample_history: AgentHistoryList):
    urls = sample_history.urls()
    assert 'https://example.com' in urls
    assert 'https://example.com/page2' in urls


def test_screenshots_in_order(sample_history: AgentHistoryList):
    assert sample_history.screenshots() == [
        'screenshot1.png',
        'screenshot2.png',
        'screenshot3.png',
    ]


def test_model_actions(sample_history: AgentHistoryList):
    """Each entry leads with its action; interacted_element rides alongside."""
    outputs = sample_history.model_actions()
    assert len(outputs) == 3
    first_pairs = [dict([next(iter(o.items()))]) for o in outputs]
    assert first_pairs == [
        {'click_element': {'index': 1}},
        {'extract_page_content': {}},
        {'done': {'text': 'Task completed'}},
    ]


def test_model_actions_filtered(sample_history: AgentHistoryList):
    filtered = sample_history.model_actions_filtered(include=['click_element'])
    assert len(filtered) == 1
    assert filtered[0]['click_element']['index'] == 1


def test_empty_history_accessors_are_all_neutral():
    """No history must yield empty/None, never an IndexError."""
    history = AgentHistoryList(history=[])
    assert history.last_action() is None
    assert history.errors() == []
    assert history.final_result() is None
    assert history.is_done() is False
    assert history.urls() == []
    assert history.screenshots() == []
