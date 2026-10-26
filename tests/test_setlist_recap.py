"""Tonight's setlist (every song that played) and the recap page."""
import time

import pytest

from conftest import TRACK_A, TRACK_B


@pytest.fixture
def host(qm, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, 'qm', qm)
    monkeypatch.setattr(app_module.sc, 'is_authenticated', lambda: True)
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s['is_host'] = True
    return c, app_module


def test_the_worker_logs_each_song_once(qm, db, guest):
    db.add_to_queue(TRACK_A, 'Song A', 'Band', '', 200_000, guest)
    qm.advance_to_next_pending()
    qm.fake.playing, qm.fake.is_playing = TRACK_A, True
    qm._current_spotify_track_id = None
    playback = qm.fake.get_current_playback()
    item = db.get_playing_item()
    qm._log_play(playback['item'], item)
    qm._log_play(playback['item'], item)        # a restart mid-song: same play
    qm._log_play({'id': TRACK_B, 'name': 'Fallback', 'artists': [{'name': 'X'}]}, None)
    plays = db.get_plays(db.get_setting('party_code'))
    assert [p['spotify_track_id'] for p in plays] == [TRACK_A, TRACK_B]
    assert plays[0]['requested_by'] and plays[1]['requested_by'] is None


def test_save_setlist_creates_then_updates_one_playlist(host, db, monkeypatch):
    c, app_module = host
    code = db.get_setting('party_code')
    db.log_play(code, TRACK_A, 'A', 'Band')
    calls = []

    def save(tracks, name, description, playlist_id=None):
        calls.append((tuple(tracks), playlist_id))
        return 'p' * 22, 'https://open.spotify.com/playlist/' + 'p' * 22
    monkeypatch.setattr(app_module.setlist.sc, 'save_setlist', save)
    first = c.post('/api/host/setlist', json={}).get_json()
    db.log_play(code, TRACK_B, 'B', 'Band')
    second = c.post('/api/host/setlist', json={}).get_json()
    assert not first['updated'] and second['updated'] and second['songs'] == 2
    assert calls == [((TRACK_A,), None), ((TRACK_A, TRACK_B), 'p' * 22)]


def test_nothing_played_means_nothing_to_save(host):
    c, _ = host
    assert c.post('/api/host/setlist', json={}).status_code == 400


def test_a_spotify_failure_is_reported(host, db, monkeypatch):
    c, app_module = host
    db.log_play(db.get_setting('party_code'), TRACK_A, 'A', 'Band')

    def broken(*a, **k):
        raise RuntimeError('403 Forbidden')
    monkeypatch.setattr(app_module.setlist.sc, 'save_setlist', broken)
    res = c.post('/api/host/setlist', json={})
    assert res.status_code == 502 and '403' in res.get_json()['error']


def test_recap_has_the_setlist_and_hides_undeveloped_photos(host, db):
    c, _ = host
    db.log_play(db.get_setting('party_code'), TRACK_A, 'Song A', 'Band')
    db.set_setting('develop_at', str(time.time() + 600))
    data = c.get('/api/recap').get_json()
    assert [s['track_name'] for s in data['setlist']] == ['Song A']
    assert data['developed'] is False and data['photos'] == []
    assert data['costume'] is None
    assert c.get('/recap').status_code == 200


def test_recap_needs_the_party_code(qm):
    import app as app_module
    assert app_module.app.test_client().get('/api/recap').status_code == 403
