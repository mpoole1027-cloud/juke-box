"""The TV's numbers and leaderboards cover tonight's party only."""
import time

import pytest


@pytest.fixture
def client(qm, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, 'qm', qm)
    monkeypatch.setattr(app_module.sc, 'is_authenticated', lambda: True)
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s['is_host'] = True
    return c


def _sqlite(ts):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts))


def test_tv_counts_only_since_the_party_started(client, db):
    conn = db.get_connection()
    try:
        old = _sqlite(time.time() - 30 * 86400)
        conn.execute("INSERT INTO users (user_id, nickname, name_set, first_seen) "
                     "VALUES ('old', 'Last Year', 1, ?)", (old,))
        conn.execute("INSERT INTO queue (spotify_track_id, track_name, artist, requested_by, "
                     "status, added_at) VALUES ('t1', 'Old Song', 'X', 'old', 'played', ?)", (old,))
        conn.execute("INSERT INTO users (user_id, nickname, name_set) VALUES ('new', 'Tonight', 1)")
        conn.execute("INSERT INTO queue (spotify_track_id, track_name, artist, requested_by, "
                     "status) VALUES ('t2', 'New Song', 'Y', 'new', 'played')")
        conn.commit()
    finally:
        conn.close()
    db.set_setting('party_started_at', _sqlite(time.time() - 60))
    data = client.get('/api/tv').get_json()
    assert data['stats']['songs_played'] == 1
    assert data['stats']['guest_count'] == 1
    assert [d['nickname'] for d in data['leaderboards']['top_djs']] == ['Tonight']
    assert data['stats']['party_start'] == db.get_setting('party_started_at')
    assert data['leaderboards']['photographers'] == []
    assert data['stats']['photos'] == 0
