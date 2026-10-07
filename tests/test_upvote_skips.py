"""Each upvote moves a queued song up one spot, capped at MAX_UPVOTE_SKIPS."""


def fill_queue(db, n):
    """Queue n songs from the host and n voters; returns queue ids in added order."""
    db.get_or_create_host_user()
    conn = db.get_connection()
    for i in range(10):
        conn.execute("INSERT OR IGNORE INTO users (user_id, nickname) VALUES (?, ?)", (f'v{i}', f'Voter {i}'))
    conn.commit()
    conn.close()
    return [db.add_to_queue(f'{i:022d}', f'Song {i}', 'Artist', None, 1000, 'host') for i in range(n)]


def upvote(db, qid, votes):
    for i in range(votes):
        assert db.add_upvote(qid, f'v{i}')


def order(db):
    return [q['id'] for q in db.get_pending_queue()]


def test_each_upvote_moves_a_song_up_one_spot(db):
    ids = fill_queue(db, 6)
    upvote(db, ids[5], 1)
    assert order(db) == [ids[0], ids[1], ids[2], ids[3], ids[5], ids[4]]
    assert db.add_upvote(ids[5], 'v1')
    assert order(db) == [ids[0], ids[1], ids[2], ids[5], ids[3], ids[4]]


def test_a_song_skips_at_most_three_spots(db):
    ids = fill_queue(db, 6)
    upvote(db, ids[5], 3)
    assert order(db) == [ids[0], ids[1], ids[5], ids[2], ids[3], ids[4]]


def test_upvotes_past_the_cap_still_count_but_dont_move_it(db):
    ids = fill_queue(db, 6)
    upvote(db, ids[5], 7)
    queue = db.get_pending_queue()
    assert [q['id'] for q in queue] == [ids[0], ids[1], ids[5], ids[2], ids[3], ids[4]]
    song = queue[2]
    assert song['upvote_count'] == 7
    assert song['skips_maxed'] is True
    assert not any(q['skips_maxed'] for q in queue if q['id'] != ids[5])


def test_more_upvotes_wins_a_tie_for_the_same_spot(db):
    ids = fill_queue(db, 5)
    upvote(db, ids[3], 2)  # aims for spot 1
    upvote(db, ids[4], 3)  # also aims for spot 1, with more votes
    assert order(db) == [ids[0], ids[4], ids[3], ids[1], ids[2]]


def test_status_reports_skips_maxed(db, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.sc, 'is_authenticated', lambda: True)
    monkeypatch.setattr(app_module.sc, 'get_current_playback', lambda: {'is_playing': True})
    ids = fill_queue(db, 4)
    upvote(db, ids[3], 3)
    c = app_module.app.test_client()
    c.get('/?p=' + db.get_setting('party_code'))
    queue = c.get('/api/status').get_json()['queue']
    assert [q['skips_maxed'] for q in queue] == [True, False, False, False]


def test_a_song_bumped_by_one_upvote_does_not_block_another(db):
    # Two guests' songs: the 2nd gets one upvote, the 4th gets two. The 4th
    # should still climb two spots, past the song the 2nd one displaced.
    ids = fill_queue(db, 4)
    upvote(db, ids[1], 1)
    upvote(db, ids[3], 2)
    assert order(db) == [ids[1], ids[3], ids[0], ids[2]]


def test_a_song_does_not_pass_one_with_as_many_upvotes(db):
    ids = fill_queue(db, 3)
    upvote(db, ids[1], 2)
    upvote(db, ids[2], 2)
    assert order(db) == [ids[1], ids[2], ids[0]]
