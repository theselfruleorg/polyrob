import json
from unittest.mock import AsyncMock

import pytest

from core.surfaces.rendering import markdown_to_html


@pytest.mark.asyncio
async def test_discord_create_and_edit_disable_all_mentions():
    from surfaces.discord.client import DiscordClient
    client = DiscordClient(token='test')
    client._request = AsyncMock(return_value={})
    await client.send_message('room', '@everyone <@123> <@&456>', reply_to='message')
    await client.edit_message('room', 'message', '@here')
    for call in client._request.call_args_list:
        assert call.kwargs['json']['allowed_mentions'] == {'parse': [], 'replied_user': False}


@pytest.mark.asyncio
async def test_slack_create_and_edit_render_control_syntax_literally():
    from surfaces.slack.client import SlackClient
    client = SlackClient(bot_token='test', app_token='test')
    client._call = AsyncMock(return_value={})
    text = '<!channel> <@U123> <https://evil.example|approved>'
    await client.send_message('C123', text)
    await client.edit_message('C123', '123.4', text)
    for call in client._call.call_args_list:
        body = call.kwargs['json']
        assert '<' not in body['text']
        assert '&lt;!channel&gt;' in body['text']
        assert body['parse'] == 'none' and body['link_names'] is False


@pytest.mark.asyncio
async def test_feishu_text_and_card_cannot_mention_everyone():
    from surfaces.feishu.client import FeishuClient
    client = object.__new__(FeishuClient)
    client.call = AsyncMock(return_value={'data': {}})
    text = '<at user_id="all">everyone</at>'
    await client.send_message('oc_room', text)
    await client.send_card('oc_room', {'elements': [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': text}}]})
    for call in client.call.call_args_list:
        assert '<at' not in call.kwargs['json']['content']
    with pytest.raises(ValueError, match='Structured mentions'):
        await client.send_card('oc_room', {'elements': [{'tag': 'at', 'user_id': 'all'}]})


@pytest.mark.parametrize('url', ['tg://user?id=123', 'javascript:alert', 'file:///etc/passwd', 'app://run'])
def test_telegram_links_cannot_create_app_actions_or_mentions(url):
    assert markdown_to_html(f'[label]({url})') == 'label'


def test_ordinary_web_and_mail_links_still_render():
    assert 'href="https://example.com"' in markdown_to_html('[web](https://example.com)')
    assert 'href="mailto:owner@example.com"' in markdown_to_html('[mail](mailto:owner@example.com)')
