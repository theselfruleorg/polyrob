import pytest

from modules.llm.brain_scrubber import StreamingBrainScrubber


@pytest.mark.parametrize('content', [
    '{"current_state":{"memory":"PRIVATE","next_goal":"hidden"}}',
    '```json\n{"current_state":{"memory":"PRIVATE"}}\n```',
    '{"reasoning":"PRIVATE","next_goal":"hidden"}',
    '<think>PRIVATE</think>',
    '<reasoning>PRIVATE</reasoning>',
])
def test_every_possible_chunk_boundary_keeps_internal_content_hidden(content):
    for index in range(len(content) + 1):
        scrubber = StreamingBrainScrubber()
        parts = [scrubber.feed('Before '), scrubber.feed(content[:index]),
                 scrubber.feed(content[index:]), scrubber.feed(' After')]
        assert ''.join(parts) == 'Before  After', (index, parts)
    scrubber = StreamingBrainScrubber()
    assert ''.join(scrubber.feed(c) for c in content) == ''


def test_legitimate_json_and_code_are_emitted_once_complete():
    scrubber = StreamingBrainScrubber()
    assert scrubber.feed('Value: {"ok":') == 'Value: '
    assert scrubber.feed('true}') == '{"ok":true}'
    assert scrubber.feed('```python\nx = 2\n') == ''
    assert scrubber.feed('```') == '```python\nx = 2\n```'


def test_unclosed_and_oversize_blocks_stay_hidden_until_reset():
    scrubber = StreamingBrainScrubber()
    assert scrubber.feed('{"current_') == ''
    assert scrubber.feed('state": {"memory":"PRIVATE') == ''
    assert scrubber.feed('x' * (scrubber.MAX_PENDING + 1)) == ''
    assert len(scrubber._pending) <= scrubber.MAX_PENDING
    assert scrubber.feed('"}}secret tail') == ''
    assert StreamingBrainScrubber().feed('new response') == 'new response'
