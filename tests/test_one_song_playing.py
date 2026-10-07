"""Only one queued song is ever marked playing."""
from conftest import TRACK_A, TRACK_B


def playing_rows(db):
    conn = db.get_connection()
    try:
        return [r['spotify_track_id'] for r in
                conn.execute("SELECT spotify_track_id FROM queue WHERE status = 'playing'")]
    finally:
        conn.close()


def test_two_guests_queuing_at_once_start_only_one_song(qm, db, guest, monkeypatch):
    # Spotify lags: right after a play command it still reports nothing playing.
    monkeypatch.setattr(qm.fake, 'get_current_playback', lambda: None)
    db.add_to_queue(TRACK_A, 'Song A', 'Artist', '', 200_000, guest)
    qm.play_if_idle()
    db.add_to_queue(TRACK_B, 'Song B', 'Artist', '', 200_000, guest)
    qm.play_if_idle()

    assert [c for c in qm.fake.calls if c.startswith('play_track')] == [f'play_track:{TRACK_A}@speaker']
    assert playing_rows(db) == [TRACK_A]
    assert [q['spotify_track_id'] for q in db.get_pending_queue()] == [TRACK_B]


def test_queuing_to_an_idle_player_starts_the_song(qm, db, guest):
    db.add_to_queue(TRACK_A, 'Song A', 'Artist', '', 200_000, guest)
    assert qm.play_if_idle()['spotify_track_id'] == TRACK_A
    assert qm.fake.playing == TRACK_A
    assert playing_rows(db) == [TRACK_A]


def test_starting_a_song_returns_a_stranded_one_to_the_queue(db, guest):
    a = db.add_to_queue(TRACK_A, 'Song A', 'Artist', '', 200_000, guest)
    b = db.add_to_queue(TRACK_B, 'Song B', 'Artist', '', 200_000, guest)
    db.update_queue_status(a, 'playing')
    db.update_queue_status(b, 'playing')
    assert playing_rows(db) == [TRACK_B]
    assert [q['id'] for q in db.get_pending_queue()] == [a]
