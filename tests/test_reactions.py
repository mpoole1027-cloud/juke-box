"""Hearts: one per guest per song, so a guest can't spam the button to boost a track."""
import sqlite3

import pytest

from conftest import TRACK_A, TRACK_B
from test_costume import app_module, guest_client  # noqa: F401  (fixture reuse)


def play(app_module, track_id, name):
    db = app_module.db
    db.get_or_create_host_user()
    qid = db.add_to_queue(track_id, name, 'Artist', None, 200_000, 'host')
    db.update_queue_status(qid, 'playing')
    return qid


def heart(client, reaction='heart'):
    return client.post('/api/react', json={'reaction': reaction})


def test_a_guest_can_heart_a_song_only_once(app_module):
    play(app_module, TRACK_A, 'Song A')
    c = guest_client(app_module, 'Ann')

    first = heart(c)
    assert first.status_code == 200
    assert first.get_json()['reactions'] == {'heart': 1}

    for _ in range(5):
        again = heart(c)
        assert again.status_code == 409
        assert again.get_json()['reactions'] == {'heart': 1}

    assert app_module.db.get_reaction_counts() == {'heart': 1}


def test_each_guest_gets_their_own_heart(app_module):
    play(app_module, TRACK_A, 'Song A')
    heart(guest_client(app_module, 'Ann'))
    res = heart(guest_client(app_module, 'Bob'))
    assert res.get_json()['reactions'] == {'heart': 2}


def test_the_next_song_can_be_hearted_again(app_module):
    first = play(app_module, TRACK_A, 'Song A')
    c = guest_client(app_module, 'Ann')
    assert heart(c).status_code == 200

    app_module.db.update_queue_status(first, 'played')
    play(app_module, TRACK_B, 'Song B')
    assert heart(c).status_code == 200


def test_fire_is_gone(app_module):
    play(app_module, TRACK_A, 'Song A')
    assert heart(guest_client(app_module, 'Ann'), 'fire').status_code == 400


def test_nothing_playing_cannot_be_hearted(app_module):
    assert heart(guest_client(app_module, 'Ann')).status_code == 409


def test_status_reports_whether_you_hearted(app_module, monkeypatch):
    play(app_module, TRACK_A, 'Song A')
    monkeypatch.setattr(app_module, '_resolve_current_track',
                        lambda: ({'track_id': TRACK_A}, None,
                                 app_module.db.get_playing_item()['id']))
    c = guest_client(app_module, 'Ann')
    assert c.get('/api/status').get_json()['user_has_reacted'] is False
    heart(c)
    assert c.get('/api/status').get_json()['user_has_reacted'] is True


def test_db_refuses_a_second_heart_even_without_the_endpoint(db, guest):
    assert db.add_reaction(guest, 'heart', 1) is True
    assert db.add_reaction(guest, 'heart', 1) is False
    assert db.get_reaction_counts(1) == {'heart': 1}


def test_migration_collapses_old_spam_and_fire(db, guest):
    # Simulate a DB from before the limit: drop the index, then spam.
    conn = db.get_connection()
    conn.execute("DROP INDEX idx_reactions_user_song")
    for r in ('heart', 'heart', 'fire', 'heart'):
        conn.execute("INSERT INTO reactions (user_id, reaction, queue_id) VALUES (?, ?, 7)", (guest, r))
    conn.commit()
    conn.close()

    db.init_db()

    assert db.get_reaction_counts(7) == {'heart': 1}
    conn = db.get_connection()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO reactions (user_id, reaction, queue_id) VALUES (?, 'heart', 7)", (guest,))
    conn.close()
