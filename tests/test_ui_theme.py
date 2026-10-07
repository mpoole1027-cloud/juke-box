"""The host-controlled Classic / Modern look toggle."""
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
    assert re.search(r'classic\.css\?v=\d+" disabled>', html)

    set_theme(client, 'classic')
    html = client.get(path).get_data(as_text=True)
    assert 'data-theme="classic"' in html
    assert re.search(r'classic\.css\?v=\d+">', html)


@pytest.mark.parametrize('path', ['/', '/host', '/tv'])
def test_static_urls_change_when_the_file_does(client, path):
    """Cloudflare caches static files in browsers for hours; a stamped URL is
    the only way a phone picks up new JS after a deploy."""
    html = client.get(path).get_data(as_text=True)
    assert re.search(r'/static/css/jukebox\.css\?v=\d+"', html)
    assert not re.search(r'"/static/[^"?]+"', html)
