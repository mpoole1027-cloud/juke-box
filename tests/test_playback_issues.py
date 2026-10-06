"""Playback problems reach the host and guests, and a stuck song is retried."""
import pytest

from conftest import TRACK_A
from test_worker_fallback import run_worker


@pytest.fixture
def no_speaker(qm, monkeypatch):
    """No Spotify Connect device online: play commands fail like the real API."""
    fake = qm.fake
    fake.devices = []
    real_play = fake.play_track

    def play(uri, device_id=None):
        if not fake.devices:
            fake.calls.append('play_failed')
            return False, 'http status: 404, code: -1 - Player command failed: No active device found, reason: NO_ACTIVE_DEVICE'
        return real_play(uri, device_id=device_id)
    monkeypatch.setattr(fake, 'play_track', play)
    return fake


def test_no_device_is_reported_and_the_song_stays_pending(qm, db, guest, no_speaker):
    db.add_to_queue(TRACK_A, 'Song A', 'Artist', '', 200_000, guest)
    qm.advance_to_next_pending()
    issue = qm.get_playback_issue()
    assert issue['code'] == 'no_device'
    assert 'Open Spotify' in issue['host_message']
    assert db.get_pending_queue()[0]['spotify_track_id'] == TRACK_A


def test_stuck_song_starts_once_a_device_appears(qm, db, guest, no_speaker):
    db.add_to_queue(TRACK_A, 'Song A', 'Artist', '', 200_000, guest)
    qm.advance_to_next_pending()
    run_worker(qm, ticks=2)
    assert db.get_pending_queue()  # still no speaker: still waiting

    no_speaker.devices = [{'id': 'speaker', 'name': 'Living Room', 'type': 'Speaker',
                           'is_active': False}]
    run_worker(qm, ticks=1)
    assert no_speaker.playing == TRACK_A
    assert db.get_pending_queue() == []
    assert qm.get_playback_issue() is None


def test_host_stopping_spotify_is_still_respected(qm, db, guest):
    """Without a failed play, an idle player with songs pending is left alone."""
    db.add_to_queue(TRACK_A, 'Song A', 'Artist', '', 200_000, guest)
    run_worker(qm, ticks=3)
    assert qm.fake.calls == []


def test_spotify_outage_and_recovery(qm):
    qm.fake.fail = True
    run_worker(qm, ticks=1)
    assert qm.get_playback_issue()['code'] == 'spotify_unreachable'
    qm.fake.fail = False
    run_worker(qm, ticks=1)
    assert qm.get_playback_issue() is None


def test_lost_token_is_its_own_issue(qm, monkeypatch):
    monkeypatch.setattr(qm.fake, 'get_playback_state', lambda: {
        'status': 'error', 'playback': None, 'reason': 'not_authenticated'})
    run_worker(qm, ticks=1)
    assert qm.get_playback_issue()['code'] == 'spotify_not_connected'


def test_guests_get_the_friendly_message(qm, db, guest, no_speaker, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, 'qm', qm)
    monkeypatch.setattr(app_module.sc, 'is_authenticated', lambda: True)
    db.add_to_queue(TRACK_A, 'Song A', 'Artist', '', 200_000, guest)
    qm.advance_to_next_pending()

    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s['party_code'] = db.get_setting('party_code')
    issue = c.get('/api/status').get_json()['playback_issue']
    assert issue == {'code': 'no_device',
                     'message': 'Music is paused while the host sorts out the speakers.'}
