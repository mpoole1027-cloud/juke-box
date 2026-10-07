"""The host-controlled look picker (Modern, Classic and the Halloween looks)."""
import re

import pytest


@pytest.fixture
def client(db, monkeypatch):
    import app as app_module
    # No Spotify in tests: the status endpoints only need these two.
    monkeypatch.setattr(app_module.sc, 'is_authenticated', lambda: False)
    monkeypatch.setattr(app_module.sc, 'get_current_playback', lambda: None)
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s['is_host'] = True
    return c


def set_theme(client, theme):
    return client.post('/api/host/settings', json={'ui_theme': theme})


def test_defaults_to_modern(client):
    assert client.get('/api/status').get_json()['ui_theme'] == 'modern'
    assert client.get('/api/tv').get_json()['ui_theme'] == 'modern'


def test_host_can_switch_to_classic_and_back(client):
    assert set_theme(client, 'classic').status_code == 200
    assert client.get('/api/status').get_json()['ui_theme'] == 'classic'
    assert client.get('/api/tv').get_json()['ui_theme'] == 'classic'

    assert set_theme(client, 'modern').status_code == 200
    assert client.get('/api/status').get_json()['ui_theme'] == 'modern'


def test_rejects_unknown_theme(client, db):
    assert set_theme(client, 'neon').status_code == 400
    assert db.get_setting('ui_theme', 'modern') == 'modern'


def test_guests_cannot_change_theme(db, monkeypatch):
    import app as app_module
    res = app_module.app.test_client().post('/api/host/settings', json={'ui_theme': 'classic'})
    assert res.status_code == 401
    assert db.get_setting('ui_theme', 'modern') == 'modern'


@pytest.mark.parametrize('path', ['/', '/host', '/tv'])
def test_pages_render_with_the_saved_theme(client, path):
    html = client.get(path).get_data(as_text=True)
    assert 'data-theme="modern"' in html
    assert re.search(r'classic\.css\?v=\d+" data-theme-css="classic"[^>]* disabled>', html)

    set_theme(client, 'classic')
    html = client.get(path).get_data(as_text=True)
    assert 'data-theme="classic"' in html
    assert re.search(r'classic\.css\?v=\d+" data-theme-css="classic"[^>]*"\s*>', html)


@pytest.mark.parametrize('theme', ['seance', 'rental', 'lantern'])
def test_halloween_looks_are_selectable(client, theme):
    import app as app_module
    assert set_theme(client, theme).status_code == 200
    assert client.get('/api/status').get_json()['ui_theme'] == theme
    html = client.get('/').get_data(as_text=True)
    assert f'data-theme="{theme}"' in html
    css = app_module.UI_THEMES[theme]['css']
    # Only the active look's stylesheet is enabled.
    assert re.search(re.escape(css) + r'\?v=\d+" data-theme-css="%s"[^>]*"\s*>' % theme, html)
    assert re.search(r'classic\.css\?v=\d+"[^>]* disabled>', html)


def test_theme_stylesheets_exist():
    import os
    import app as app_module
    for name, t in app_module.UI_THEMES.items():
        if t['css']:
            assert os.path.exists(os.path.join(app_module.app.static_folder, t['css'])), name


def test_rejects_non_string_theme(client, db):
    assert set_theme(client, ['seance']).status_code == 400


def test_host_picker_lists_every_look(client):
    import app as app_module
    html = client.get('/host').get_data(as_text=True)
    for name in app_module.UI_THEMES:
        assert f'data-theme-pick="{name}"' in html


@pytest.mark.parametrize('path', ['/', '/host', '/tv'])
def test_static_urls_change_when_the_file_does(client, path):
    """Cloudflare caches static files in browsers for hours; a stamped URL is
    the only way a phone picks up new JS after a deploy."""
    html = client.get(path).get_data(as_text=True)
    assert re.search(r'/static/css/jukebox\.css\?v=\d+"', html)
    assert not re.search(r'"/static/[^"?]+"', html)
