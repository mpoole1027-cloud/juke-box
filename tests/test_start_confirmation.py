"""Spotify accepting a play command is not the same as playing.

On 2026-10-25, with the Mac's lid shut, Spotify took each play command and then
played nothing. The worker read the silence as "the song ended", marked it
played and started the next: four guest songs burned in a minute, each one
un-requestable for the rest of the night.
"""
from conftest import TRACK_A, TRACK_B
from test_worker_fallback import run_worker


def deaf_spotify(qm, monkeypatch):
    """Accepts every play and plays nothing."""
    calls = []

    def accept(uri, device_id=None):
        calls.append(uri.split(':')[-1])
        return True, None
    monkeypatch.setattr(qm.fake, 'play_track', accept)
    qm.fake.playing = None
    return calls


def queue_two(db, guest):
    return [db.add_to_queue(t, n, 'Artist', None, 200_000, guest)
            for t, n in ((TRACK_A, 'A'), (TRACK_B, 'B'))]


def played(db):
    conn = db.get_connection()
    try:
        return [r[0] for r in conn.execute("SELECT spotify_track_id FROM played_tracks")]
    finally:
        conn.close()


def statuses(db, ids):
    conn = db.get_connection()
    try:
        return [conn.execute("SELECT status FROM queue WHERE id = ?", (i,)).fetchone()[0] for i in ids]
    finally:
        conn.close()


def test_silence_right_after_a_play_does_not_burn_the_queue(qm, db, guest, monkeypatch):
    calls = deaf_spotify(qm, monkeypatch)
    ids = queue_two(db, guest)
    qm.advance_to_next_pending()
    run_worker(qm, ticks=6)                     # well inside the start timeout
    assert statuses(db, ids) == ['playing', 'pending']
    assert calls == [TRACK_A]
    assert played(db) == []


def test_a_song_that_never_starts_goes_back_and_the_host_is_told(qm, db, guest, monkeypatch):
    calls = deaf_spotify(qm, monkeypatch)
    ids = queue_two(db, guest)
    qm.advance_to_next_pending()
    monkeypatch.setattr(qm, 'START_TIMEOUT_SECONDS', 0)
    run_worker(qm, ticks=1)
    assert statuses(db, ids)[1] == 'pending'
    assert qm.get_playback_issue()['code'] == 'not_starting'
    assert played(db) == []
    # It keeps retrying the same song, never the one behind it.
    run_worker(qm, ticks=4)
    assert set(calls) == {TRACK_A}


def test_once_spotify_plays_the_issue_clears_and_songs_end_normally(qm, db, guest, monkeypatch):
    working = qm.fake.play_track
    deaf_spotify(qm, monkeypatch)
    ids = queue_two(db, guest)
    qm.advance_to_next_pending()
    monkeypatch.setattr(qm, 'START_TIMEOUT_SECONDS', 0)
    run_worker(qm, ticks=1)
    assert qm.get_playback_issue()['code'] == 'not_starting'
    monkeypatch.setattr(qm.fake, 'play_track', working)   # the host woke Spotify up
    run_worker(qm, ticks=3)                      # retried for real now
    assert qm.get_playback_issue() is None
    assert statuses(db, ids)[0] == 'playing'
    qm.fake.progress = 199_900                   # the song ends
    run_worker(qm, ticks=2)
    assert statuses(db, ids) == ['played', 'playing']
