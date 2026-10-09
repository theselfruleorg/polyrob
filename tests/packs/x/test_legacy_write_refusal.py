import pytest

from polyrob_x.twitter_tool import TwitterTool


@pytest.mark.asyncio
@pytest.mark.parametrize('name,args', [
    ('post', ('text',)), ('create_tweet', ('text',)), ('like', ('123',)),
    ('unlike', ('123',)), ('retweet', ('123',)), ('unretweet', ('123',)),
    ('reply', ('123', 'text')), ('quote_tweet', ('123', 'text')),
])
async def test_legacy_write_requires_guarded_action(name, args):
    tool = object.__new__(TwitterTool)
    with pytest.raises(RuntimeError, match='owner approval'):
        await getattr(tool, name)(*args)


@pytest.mark.parametrize('context', [None, object()])
def test_browser_write_without_role_is_untrusted(context):
    from polyrob_x.x_browser.tool import _leaf_or_forged
    assert _leaf_or_forged(context)


def test_plaintext_choice_is_visible_even_with_many_extra_parameters():
    from tools.controller.grant_card import render_grant_card
    params = {f'extra{i}': 'padding' for i in range(20)}
    params.update(recipient='recipient', text='message', allow_plaintext=True)
    card = render_grant_card('twitter_dm', params, 'approval-id')
    assert 'allow_plaintext' in card
