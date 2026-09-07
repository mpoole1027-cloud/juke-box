"""End-to-end behaviour of the real background_worker().

These run the actual shipped loop, not a re-implementation of it.
"""
import time
import pytest
from conftest import FALLBACK_ID, TRACK_A, TRACK_B


class FastEvent:
    """Drives background_worker() for exactly n iterations, with no sleeping."""
    def __init__(self, n):
        self.n = n
        self.waits = 0

    def is_set(self):
        self.n -= 1
        return self.n < 0

    def wait(self, t):
        self.waits += 1
        return False

    def clear(self):
        pass

    def set(self):
        self.n = -1


def run_worker(qm, ticks=6):
    qm._stop_event = FastEvent(ticks)
    qm.background_worker()


@pytest.fixture
def party(qm, db, guest):
    """A guest song playing, near its end, with a fallback configured."""
    db.set_setting('fallback_playlist_id', FALLBACK_ID)
    qid = db.add_to_queue(TRACK_A, 'Song A', 'Artist', None, 200_000, guest)
    db.update_queue_status(qid, 'playing')
    qm.fake.play_track(f'spotify:track:{TRACK_A}')
    qm._current_spotify_track_id = TRACK_A
    qm._current_is_ours = True
    qm.fake.progress = 199_000
    qm.fake.calls.clear()
    return qm


def test_never_pauses_when_queue_empties(party):
    """The original bug: an empty queue paused Spotify account-wide."""
    run_worker(party)
    assert 'PAUSE' not in party.fake.calls


def test_empty_queue_starts_fallback(party):
    run_worker(party)
    assert any(c.startswith('start_playlist') for c in party.fake.calls)


def test_fallback_is_not_restarted_every_poll(party):
    run_worker(party, ticks=10)
    starts = [c for c in party.fake.calls if c.startswith('start_playlist')]
    assert len(starts) == 1, f"restart storm: {starts}"


def test_fallback_tracks_stay_out_of_played_tracks(party, db):
    """played_tracks backs the duplicate check; fallback filler must not
    silently blacklist the host's whole playlist from being requested."""
    run_worker(party, ticks=10)
    conn = db.get_connection()
    played = [r[0] for r in conn.execute("SELECT spotify_track_id FROM played_tracks")]
    conn.close()
    assert not [p for p in played if p.startswith('fb')]
    assert TRACK_A in played, "the guest's song should still be recorded"


def test_guest_song_does_not_cut_into_fallback(party, db, guest):
    run_worker(party)                       # fallback now playing
    db.add_to_queue(TRACK_B, 'Song B', 'Artist', None, 200_000, guest)
    party.fake.progress = 1_000             # mid-track
    party.fake.calls.clear()
    run_worker(party, ticks=2)
    assert not any(TRACK_B in c for c in party.fake.calls)


def test_guest_song_plays_at_end_of_fallback_track(party, db, guest):
    run_worker(party)
    db.add_to_queue(TRACK_B, 'Song B', 'Artist', None, 200_000, guest)
    party.fake.progress = 199_000           # near end
    party.fake.calls.clear()
    run_worker(party, ticks=2)
    assert any(TRACK_B in c for c in party.fake.calls)


def test_no_fallback_configured_still_never_pauses(qm, db, guest):
    db.set_setting('fallback_playlist_id', '')
    qid = db.add_to_queue(TRACK_A, 'A', 'Artist', None, 200_000, guest)
    db.update_queue_status(qid, 'playing')
    qm.fake.play_track(f'spotify:track:{TRACK_A}')
    qm._current_spotify_track_id, qm._current_is_ours = TRACK_A, True
    qm.fake.progress = 199_000
    qm.fake.calls.clear()
    run_worker(qm)
    assert 'PAUSE' not in qm.fake.calls


def test_manual_stop_stays_stopped(qm, db):
    """Host stopped Spotify with nothing of ours playing — don't force music on."""
    db.set_setting('fallback_playlist_id', FALLBACK_ID)
    qm.fake.playing = None
    qm._current_spotify_track_id = None
    qm.fake.calls.clear()
    run_worker(qm)
    assert qm.fake.calls == []


def test_worker_uses_pinned_device(party, db):
    db.set_setting('preferred_device_id', 'speaker')
    run_worker(party)
    assert any(c.endswith('@speaker') for c in party.fake.calls), party.fake.calls


def test_stands_down_after_idle_period(qm, db, guest):
    db.set_setting('fallback_playlist_id', FALLBACK_ID)
    db.set_setting('idle_shutdown_hours', 1)
    qid = db.add_to_queue(TRACK_A, 'A', 'Artist', None, 200_000, guest)
    db.update_queue_status(qid, 'playing')
    qm.fake.play_track(f'spotify:track:{TRACK_A}')
    qm._current_spotify_track_id, qm._current_is_ours = TRACK_A, True
    qm.fake.progress = 199_000
    qm._last_activity = time.monotonic() - 7200      # idle 2h, limit 1h
    qm.fake.calls.clear()
    run_worker(qm, ticks=4)
    assert qm.is_standing_down() is True
    assert qm.fake.calls == [], "a stood-down worker must not touch playback"


def test_does_not_stand_down_while_active(party, db):
    db.set_setting('idle_shutdown_hours', 1)
    party.note_activity()
    run_worker(party, ticks=3)
    assert party.is_standing_down() is False


def test_standby_disabled_never_stands_down(party, db):
    db.set_setting('idle_shutdown_hours', 0)
    party._last_activity = time.monotonic() - 999_999
    run_worker(party, ticks=3)
    assert party.is_standing_down() is False
