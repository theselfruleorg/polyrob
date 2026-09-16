"""Console write affordances must match server policy; themes stay tenant-scoped."""
from pathlib import Path
from types import SimpleNamespace
import re

from core.config_service import is_console_unwritable
from core.flags import REGISTRY
from core.prefs import write_preference
from webview.config_view import flag_metadata
from webview.theme import theme_preference


def test_flag_permissions_match_server(monkeypatch):
    monkeypatch.setenv('POLYROB_POSTURE', 'own_ops')
    monkeypatch.setenv('WEBVIEW_READ_ONLY', 'false')
    for key in REGISTRY:
        assert flag_metadata(key)['console_writable'] == (not is_console_unwritable(key)), key


def test_multitenant_and_readonly_never_offer_env_writes(monkeypatch):
    for posture, readonly in [('multitenant', 'false'), ('local', 'true')]:
        monkeypatch.setenv('POLYROB_POSTURE', posture)
        monkeypatch.setenv('WEBVIEW_READ_ONLY', readonly)
        assert not flag_metadata('MEMORY_BACKEND')['console_writable']
        assert flag_metadata('MEMORY_BACKEND')['write_reason']
    assert not flag_metadata('POLYROB_<PROVIDER>_MODEL')['console_writable']


def test_theme_roundtrip_and_public_isolation(monkeypatch, tmp_path):
    from webview import webgate
    monkeypatch.setenv('POLYROB_POSTURE', 'own_ops')
    monkeypatch.setattr(webgate, 'data_dir', lambda: str(tmp_path))
    monkeypatch.setattr('webview.pages._effective_user_id', lambda request: request.state.user_id)
    assert write_preference(tmp_path, 'alice', 'ui.theme', 'light')[0]
    assert write_preference(tmp_path, 'bob', 'ui.theme', 'dark')[0]
    for user, expected in [('alice','light'),('bob','dark')]:
        assert theme_preference(SimpleNamespace(state=SimpleNamespace(authenticated=True,user_id=user))) == expected
    assert theme_preference(SimpleNamespace(state=SimpleNamespace(authenticated=False,user_id='alice'))) == 'auto'
    assert not write_preference(tmp_path, 'alice', 'ui.theme', 'sepia')[0]


def test_colors_live_only_in_tokens():
    root = Path(__file__).resolve().parents[3] / 'webview'
    paths = list((root/'static/css').rglob('*.css')) + [root/'static/app/app.css']
    for path in paths:
        if path.name == 'variables.css':
            continue
        css = re.sub(r'/\*.*?\*/', '', path.read_text(), flags=re.S)
        for declaration in re.findall(r':\s*([^;{}]+)[;}]', css):
            assert not re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\(', declaration), (path, declaration)
