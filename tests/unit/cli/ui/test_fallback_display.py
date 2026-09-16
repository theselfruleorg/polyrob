from cli.ui.events import normalize
from cli.ui.state import SessionState
from cli.ui.app import separator_label


def event(**overrides):
    return normalize({'type': 'provider_fallback_success', 'data': {
        'original_provider': 'zai-coding', 'original_model': 'glm-5',
        'fallback_provider': 'gemini', 'fallback_model': 'gemini-test',
        **overrides,
    }})


def test_successful_fallback_updates_actual_display_pair():
    state = SessionState()
    state.provider, state.model = 'zai-coding', 'glm-5'
    state.update(event())
    assert state.provider == 'gemini'
    assert state.model == 'gemini-test'
    assert 'zai-coding' not in separator_label(state)
    assert 'gemini-test' in separator_label(state)


def test_child_fallback_does_not_relabel_parent():
    state = SessionState()
    state.main_agent_id = 'parent'
    state.provider, state.model = 'zai-coding', 'glm-5'
    state.update(event(agent_id='child'))
    assert (state.provider, state.model) == ('zai-coding', 'glm-5')


def test_missing_model_never_pairs_old_model_with_new_provider():
    state = SessionState()
    state.provider, state.model = 'zai-coding', 'glm-5'
    state.update(event(fallback_model=''))
    assert (state.provider, state.model) == ('gemini', '')
