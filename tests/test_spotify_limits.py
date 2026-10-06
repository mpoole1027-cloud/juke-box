"""Keeping Spotify calls down: cached searches, and backing off on a 429
instead of sleeping through it in a request thread."""
import time

import pytest
import requests

import spotify_client as sc


@pytest.fixture(autouse=True)
def _reset_spotify_state(monkeypatch):
    monkeypatch.setattr(sc, '_retry_until', 0.0)
    sc._search_cache.clear()
    yield
    sc._search_cache.clear()


@pytest.fixture
def spotify_search(monkeypatch):
    calls = []

    def fake(query, limit):
        calls.append(query)
        return [{'track_id': 't' * 22, 'track_name': query}] if query != 'nothing' else []
    monkeypatch.setattr(sc, '_search_spotify', fake)
    return calls


def test_repeat_searches_hit_the_cache(spotify_search):
    first = sc.search_tracks('Daft Punk')
    assert sc.search_tracks('daft  punk ') == first
    assert sc.search_tracks('DAFT PUNK') == first
    assert spotify_search == ['Daft Punk']


def test_empty_results_are_not_cached(spotify_search):
    sc.search_tracks('nothing')
    sc.search_tracks('nothing')
    assert spotify_search == ['nothing', 'nothing']


def test_cache_entries_expire(spotify_search, monkeypatch):
    sc.search_tracks('abba')
    monkeypatch.setattr(sc, 'SEARCH_CACHE_SECONDS', 0)
    sc.search_tracks('abba')
    assert len(spotify_search) == 2


class _Adapter(requests.adapters.BaseAdapter):
    """Answers every request with a fixed status, counting requests."""

    def __init__(self, status, headers=None):
        super().__init__()
        self.status, self.headers, self.sent = status, headers or {}, 0

    def send(self, request, **kwargs):
        self.sent += 1
        r = requests.Response()
        r.status_code, r.request, r.url = self.status, request, request.url
        r.headers.update(self.headers)
        r._content = b'{}'
        return r

    def close(self):
        pass


def test_429_backs_off_for_retry_after_without_sleeping():
    session = sc._SpotifySession()
    adapter = _Adapter(429, {'Retry-After': '120'})
    session.mount('https://', adapter)

    start = time.monotonic()
    assert session.get('https://api.spotify.com/v1/search').status_code == 429
    assert time.monotonic() - start < 1
    assert 115 < sc.rate_limited_for() <= 120

    with pytest.raises(sc.RateLimited):
        session.get('https://api.spotify.com/v1/search')
    assert adapter.sent == 1                 # the second call never left


def test_429_without_retry_after_uses_default():
    session = sc._SpotifySession()
    session.mount('https://', _Adapter(429))
    session.get('https://api.spotify.com/v1/me/player')
    assert sc.rate_limited_for() > sc.DEFAULT_RETRY_AFTER - 5


def test_shared_session_does_not_retry_429():
    adapter = sc._session.get_adapter('https://api.spotify.com')
    assert 429 not in adapter.max_retries.status_forcelist


def test_playback_state_reports_rate_limit(monkeypatch):
    monkeypatch.setattr(sc, 'get_spotify', lambda: sc._client_for('token'))
    monkeypatch.setattr(sc, '_retry_until', time.monotonic() + 60)
    state = sc.get_playback_state()
    assert state['status'] == sc.PLAYBACK_ERROR
    assert state['reason'] == 'rate_limited'


def test_client_is_reused_per_token():
    assert sc._client_for('a') is sc._client_for('a')
    assert sc._client_for('b') is not sc._client_for('a')


@pytest.fixture
def guest_client(db, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.sc, 'is_authenticated', lambda: True)
    monkeypatch.setattr(app_module.sc, '_search_spotify', lambda q, limit: [])
    c = app_module.app.test_client()
    c.get(f"/?p={db.get_setting('party_code')}")
    return c


def test_search_says_spotify_is_busy_while_backing_off(guest_client, monkeypatch):
    monkeypatch.setattr(sc, '_retry_until', time.monotonic() + 60)
    res = guest_client.get('/api/search?q=abba')
    assert res.status_code == 503
    assert 'busy' in res.get_json()['error']


def test_cached_search_still_works_while_backing_off(guest_client, monkeypatch):
    sc._search_cache[('abba', 10)] = (time.monotonic(), [{'track_id': 'x' * 22}])
    monkeypatch.setattr(sc, '_retry_until', time.monotonic() + 60)
    res = guest_client.get('/api/search?q=ABBA')
    assert res.status_code == 200
    assert res.get_json()['tracks']
