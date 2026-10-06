"""Page polls read the worker's snapshot instead of calling Spotify each time."""
import threading
import time

import pytest

from conftest import TRACK_A


@pytest.fixture
def counted(qm, monkeypatch):
    """The fake Spotify, counting get_current_playback calls."""
    fake = qm.fake
    fake.playback_calls = 0
    real = fake.get_current_playback

    def counting():
        fake.playback_calls += 1
        return real()
    monkeypatch.setattr(fake, 'get_current_playback', counting)
    return fake


@pytest.fixture
def client(qm, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, 'qm', qm)
    monkeypatch.setattr(app_module.sc, 'is_authenticated', lambda: True)
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s['is_host'] = True
    return c


def test_many_polls_cost_one_spotify_call(qm, counted, client):
    counted.playing, counted.is_playing = TRACK_A, True
    for _ in range(20):
        assert client.get('/api/status').status_code == 200
        assert client.get('/api/tv').status_code == 200
    assert counted.playback_calls == 1


def test_worker_snapshot_is_used_without_any_extra_call(qm, counted):
    counted.playing, counted.is_playing = TRACK_A, True
    qm._store_snapshot(counted.get_current_playback())
    calls = counted.playback_calls
    assert qm.get_cached_playback()['item']['id'] == TRACK_A
    assert counted.playback_calls == calls


def test_stale_snapshot_is_refreshed(qm, counted, monkeypatch):
    qm._store_snapshot(None)
    later = time.monotonic() + qm.POLL_SECONDS + 5
    monkeypatch.setattr(qm.time, 'monotonic', lambda: later)
    counted.playing = TRACK_A
    assert qm.get_cached_playback()['item']['id'] == TRACK_A
    assert counted.playback_calls == 1


def test_playing_a_song_invalidates_the_snapshot(qm, counted, guest, db):
    qm._store_snapshot(None)
    db.add_to_queue(TRACK_A, 'A', 'x', '', 1000, guest)
    qm.advance_to_next_pending()
    assert qm.get_cached_playback()['item']['id'] == TRACK_A


def test_concurrent_misses_share_one_fetch(qm, counted, monkeypatch):
    real = counted.get_current_playback

    def slow():
        time.sleep(0.05)
        return real()
    monkeypatch.setattr(counted, 'get_current_playback', slow)
    threads = [threading.Thread(target=qm.get_cached_playback) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counted.playback_calls == 1
