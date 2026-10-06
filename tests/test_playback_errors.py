"""A Spotify outage must never be mistaken for 'the song ended'.

get_current_playback() returns None for a dead token, a network failure and an
idle player alike. The worker read that as the track finishing, so a momentary
Wi-Fi drop marked the guest's song played -- making it un-requestable for the
rest of the night -- and skipped to the next one.
"""
import spotify_client as sc
from conftest import FALLBACK_ID, TRACK_A
from test_worker_fallback import run_worker


def _playing_guest_song(qm, db, guest):
    qid = db.add_to_queue(TRACK_A, 'Song A', 'Artist', None, 200_000, guest)
    db.update_queue_status(qid, 'playing')
    qm.fake.play_track(f'spotify:track:{TRACK_A}')
    qm._current_spotify_track_id = TRACK_A
    qm._current_is_ours = True
    qm.fake.progress = 199_000          # near end: the dangerous moment
    qm.fake.calls.clear()
    return qid


def test_outage_does_not_mark_the_song_played(qm, db, guest):
    qid = _playing_guest_song(qm, db, guest)
    qm.fake.fail = True
    run_worker(qm, ticks=5)

    conn = db.get_connection()
    played = [r[0] for r in conn.execute("SELECT spotify_track_id FROM played_tracks")]
    conn.close()
    assert TRACK_A not in played, \
        "a network blip must not burn the guest's song into played_tracks"

    # track_played_tonight() also covers songs currently pending/playing, so
    # retire the queue row to prove the outage left no permanent mark behind.
    db.update_queue_status(qid, 'skipped')
    assert db.track_played_tonight(TRACK_A) is False, \
        "the song must still be requestable after the outage"


def test_outage_does_not_skip_to_the_next_song(qm, db, guest):
    _playing_guest_song(qm, db, guest)
    db.set_setting('fallback_playlist_id', FALLBACK_ID)
    qm.fake.fail = True
    run_worker(qm, ticks=5)
    assert qm.fake.calls == [], f"nothing should have been commanded: {qm.fake.calls}"


def test_outage_leaves_tracking_state_untouched(qm, db, guest):
    _playing_guest_song(qm, db, guest)
    qm.fake.fail = True
    run_worker(qm, ticks=5)
    assert qm._current_spotify_track_id == TRACK_A
    assert qm._current_is_ours is True


def test_queue_row_stays_playing_through_an_outage(qm, db, guest):
    qid = _playing_guest_song(qm, db, guest)
    qm.fake.fail = True
    run_worker(qm, ticks=5)
    assert db.get_queue_item(qid)['status'] == 'playing'


def test_recovers_when_spotify_comes_back(qm, db, guest):
    _playing_guest_song(qm, db, guest)
    db.set_setting('fallback_playlist_id', FALLBACK_ID)

    qm.fake.fail = True
    run_worker(qm, ticks=3)
    assert qm.fake.calls == []

    qm.fake.fail = False                # network returns, song still near its end
    run_worker(qm, ticks=3)
    assert db.track_played_tonight(TRACK_A) is True, "now it really did finish"
    assert any(c.startswith('start_playlist') for c in qm.fake.calls)


def test_real_client_reports_error_not_idle_when_unauthenticated(monkeypatch):
    """An unusable client is a failure, not an idle player."""
    monkeypatch.setattr(sc, 'get_spotify', lambda: None)
    state = sc.get_playback_state()
    assert state['status'] == sc.PLAYBACK_ERROR
    assert state['reason'] == 'not_authenticated'


def test_real_client_reports_error_when_the_call_raises(monkeypatch):
    class Boom:
        def current_playback(self):
            raise RuntimeError('read timed out')
    monkeypatch.setattr(sc, 'get_spotify', lambda: Boom())
    assert sc.get_playback_state()['status'] == sc.PLAYBACK_ERROR


def test_real_client_reports_idle_when_nothing_is_playing(monkeypatch):
    class Quiet:
        def current_playback(self):
            return None
    monkeypatch.setattr(sc, 'get_spotify', lambda: Quiet())
    assert sc.get_playback_state()['status'] == sc.PLAYBACK_IDLE
