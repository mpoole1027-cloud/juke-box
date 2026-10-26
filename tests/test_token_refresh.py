"""The Spotify login: read without the network, refreshed once, saved whole."""
import json
import os
import threading
import time

import pytest

import spotify_client as sc

SCOPED = {'access_token': 'old', 'refresh_token': 'r', 'token_type': 'Bearer',
          'expires_in': 3600, 'scope': sc.SCOPES}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    path = tmp_path / 'token_cache.json'
    monkeypatch.setattr(sc, 'TOKEN_CACHE_PATH', str(path))
    monkeypatch.setattr(sc, '_refresh_retry_at', 0.0)
    monkeypatch.setenv('SPOTIFY_CLIENT_ID', 'id')
    monkeypatch.setenv('SPOTIFY_CLIENT_SECRET', 'secret')

    def write(**extra):
        path.write_text(json.dumps({**SCOPED, **extra}))
    return path, write


def test_is_authenticated_never_refreshes(cache, monkeypatch):
    _, write = cache
    write(expires_at=int(time.time()) - 100)        # expired
    monkeypatch.setattr(sc.SpotifyOAuth, 'refresh_access_token',
                        lambda *a, **k: pytest.fail('is_authenticated hit the network'))
    assert sc.is_authenticated()


def test_missing_scope_means_reconnect(cache):
    _, write = cache
    write(expires_at=int(time.time()) + 3600, scope='streaming')
    assert not sc.is_authenticated()


def test_token_requests_have_a_timeout():
    assert sc.get_oauth().requests_timeout == sc.TOKEN_TIMEOUT


def test_save_is_atomic_and_private(cache):
    path, write = cache
    write(expires_at=1)
    sc._cache_handler().save_token_to_cache({'access_token': 'new'})
    assert json.loads(path.read_text()) == {'access_token': 'new'}
    assert oct(os.stat(path).st_mode & 0o777) == '0o600'
    assert [p.name for p in path.parent.iterdir()] == ['token_cache.json']


def test_concurrent_expiry_refreshes_once(cache, monkeypatch):
    _, write = cache
    write(expires_at=int(time.time()) - 100)
    calls = []

    def slow_refresh(self, refresh_token):
        calls.append(refresh_token)
        time.sleep(0.2)
        info = {**SCOPED, 'access_token': 'new', 'expires_at': int(time.time()) + 3600}
        self.cache_handler.save_token_to_cache(info)
        return info
    monkeypatch.setattr(sc.SpotifyOAuth, 'refresh_access_token', slow_refresh)
    tokens = []
    threads = [threading.Thread(target=lambda: tokens.append(sc._fresh_token()['access_token']))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert sc._fresh_token()['access_token'] == 'new'


def test_failed_refresh_backs_off(cache, monkeypatch):
    _, write = cache
    write(expires_at=int(time.time()) - 100)
    calls = []

    def offline(self, refresh_token):
        calls.append(1)
        raise ConnectionError('no network')
    monkeypatch.setattr(sc.SpotifyOAuth, 'refresh_access_token', offline)
    for _ in range(5):
        sc.get_spotify()
    assert len(calls) == 1
