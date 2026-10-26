"""/api/now for party-lights and /healthz for the preflight check."""
import pytest

from conftest import TRACK_A


@pytest.fixture
def client(qm, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, 'qm', qm)
    monkeypatch.setattr(app_module.sc, 'is_authenticated', lambda: True)
    return app_module.app.test_client(use_cookies=False)


def test_now_needs_no_code_and_shows_only_the_track(qm, client, db, guest):
    qm.fake.playing, qm.fake.is_playing = TRACK_A, True
    db.add_to_queue(TRACK_A, 'A', 'x', '', 1000, guest)
    db.update_queue_status(db.get_pending_queue()[0]['id'], 'playing')
    data = client.get('/api/now').get_json()
    track = data['current_track']
    assert {k: track[k] for k in ('track_id', 'track_name', 'artist', 'is_playing')} == {
        'track_id': TRACK_A, 'track_name': TRACK_A, 'artist': 'Artist', 'is_playing': True}
    assert set(track) <= {'track_id', 'track_name', 'artist', 'is_playing', 'position_s',
                          'requested_by_nickname'}


def test_now_carries_the_position_forward_by_the_snapshot_age(qm, client, monkeypatch):
    qm.fake.playing, qm.fake.is_playing = TRACK_A, True
    playback = qm.fake.get_current_playback()
    playback['progress_ms'] = 30_000
    qm._store_snapshot(playback)
    monkeypatch.setattr(qm, 'playback_age', lambda: 1.5)
    assert client.get('/api/now').get_json()['current_track']['position_s'] == 31.5
    playback['is_playing'] = False
    assert client.get('/api/now').get_json()['current_track']['position_s'] == 30.0


def test_now_when_nothing_plays(client):
    assert client.get('/api/now').get_json() == {'current_track': None}


def test_health_reports_a_dead_worker(qm, client, monkeypatch):
    monkeypatch.setattr(qm, 'worker_alive', lambda: False)
    res = client.get('/healthz')
    assert res.status_code == 503
    assert res.get_json()['queue_worker'] is False


def test_health_ok(qm, client, monkeypatch):
    monkeypatch.setattr(qm, 'worker_alive', lambda: True)
    res = client.get('/healthz')
    assert res.status_code == 200
    body = res.get_json()
    assert body['ok'] and body['database'] and body['playback_issue'] is None
