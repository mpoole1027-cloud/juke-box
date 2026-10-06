"""Spotify OAuth is host-only and rejects callbacks the app didn't start.

Whoever finishes /auth/callback becomes the account the whole party plays
through, so on a public URL these routes are the most valuable thing to guard.
"""
import pytest


@pytest.fixture
def app_module(db, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.sc, 'get_auth_url',
                        lambda state=None: f'https://accounts.spotify.com/authorize?state={state}')
    exchanged = []
    monkeypatch.setattr(app_module.sc, 'handle_callback',
                        lambda code: exchanged.append(code) or {'access_token': 'x'})
    app_module.exchanged = exchanged
    return app_module


def host_client(app_module):
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s['is_host'] = True
    return c


def test_guest_cannot_start_spotify_auth(app_module):
    res = app_module.app.test_client().get('/auth/spotify')
    assert res.status_code == 302
    assert res.headers['Location'].endswith('/host')


def test_guest_cannot_finish_spotify_auth(app_module):
    res = app_module.app.test_client().get('/auth/callback?code=abc&state=whatever')
    assert res.status_code == 401
    assert app_module.exchanged == []


def test_host_round_trip_with_matching_state(app_module):
    c = host_client(app_module)
    res = c.get('/auth/spotify')
    assert res.status_code == 302
    state = res.headers['Location'].split('state=')[1]
    res = c.get(f'/auth/callback?code=abc&state={state}')
    assert res.status_code == 302
    assert app_module.exchanged == ['abc']


def test_callback_with_wrong_state_is_rejected(app_module):
    c = host_client(app_module)
    c.get('/auth/spotify')
    res = c.get('/auth/callback?code=abc&state=forged')
    assert res.status_code == 400
    assert app_module.exchanged == []


def test_state_is_single_use(app_module):
    c = host_client(app_module)
    state = c.get('/auth/spotify').headers['Location'].split('state=')[1]
    assert c.get(f'/auth/callback?code=abc&state={state}').status_code == 302
    assert c.get(f'/auth/callback?code=def&state={state}').status_code == 400
    assert app_module.exchanged == ['abc']
