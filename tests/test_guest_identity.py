"""Guest identity comes from the signed session cookie, never from the client."""
import pytest


@pytest.fixture
def app_module(db, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.sc, 'is_authenticated', lambda: True)
    monkeypatch.setattr(app_module.sc, 'get_current_playback', lambda: {'is_playing': True})
    monkeypatch.setattr(app_module.sc, 'play_track', lambda *a, **k: (True, None))
    return app_module


def join(app_module, name='Test Guest'):
    """A browser that scanned tonight's QR code and entered a name."""
    c = app_module.app.test_client()
    c.get('/?p=' + app_module.db.get_setting('party_code'))
    c.get('/')
    if name:
        c.post('/api/user/nickname', json={'nickname': name})
    return c


def queue_song(client, track='t' * 22):
    return client.post('/api/queue', json={
        'track_id': track, 'track_name': 'Song', 'artist': 'Artist', 'duration_ms': 1000})


def test_each_browser_gets_its_own_guest(app_module):
    a = app_module.app.test_client()
    b = app_module.app.test_client()
    a.get('/')
    b.get('/')
    with a.session_transaction() as sa, b.session_transaction() as sb:
        assert sa['guest_id'] and sb['guest_id'] and sa['guest_id'] != sb['guest_id']


def test_header_cannot_pick_an_identity(app_module, db):
    victim = join(app_module)
    with victim.session_transaction() as s:
        victim_id = s['guest_id']
    assert queue_song(victim).status_code == 200
    item = db.get_pending_queue()[0]

    attacker = join(app_module)
    res = attacker.delete(f"/api/queue/{item['id']}", headers={'X-User-ID': victim_id})
    assert res.status_code == 403
    assert db.get_pending_queue()


def test_status_marks_only_my_songs_and_hides_ids(app_module):
    me = join(app_module)
    other = join(app_module)
    queue_song(me, 'm' * 22)
    queue_song(other, 'o' * 22)

    queue = me.get('/api/status').get_json()['queue']
    mine = {q['track_id']: q['is_mine'] for q in queue}
    assert mine == {'m' * 22: True, 'o' * 22: False}
    assert all('requested_by' not in q for q in queue)


def test_cookieless_pollers_do_not_create_guests(app_module, db):
    c = app_module.app.test_client(use_cookies=False)
    for _ in range(5):
        assert c.get('/api/status').status_code == 200
    assert db.get_all_users() == []


def test_ban_sticks_to_the_browser(app_module, db):
    c = join(app_module)
    assert queue_song(c, 'x' * 22).status_code == 200
    with c.session_transaction() as s:
        db.ban_user(s['guest_id'])
    res = queue_song(c, 'y' * 22)
    assert res.status_code == 403
    assert 'banned' in res.get_json()['error']


def test_new_guest_must_enter_a_name_before_queuing(app_module, db):
    c = join(app_module, name=None)
    assert c.get('/api/status').get_json()['name_required'] is True
    res = queue_song(c)
    assert res.status_code == 403
    assert res.get_json()['code'] == 'name_required'
    assert db.get_pending_queue() == []

    c.post('/api/user/nickname', json={'nickname': '  Jane   Doe '})
    status = c.get('/api/status').get_json()
    assert status['name_required'] is False
    assert status['user']['nickname'] == 'Jane Doe'
    assert queue_song(c).status_code == 200


def test_queue_shows_each_songs_wait(app_module, db, monkeypatch):
    monkeypatch.setattr(app_module.qm, 'get_cached_playback', lambda: {
        'is_playing': True, 'progress_ms': 60_000,
        'item': {'id': 'c' * 22, 'name': 'Now', 'artists': [{'name': 'B'}],
                 'album': {'images': []}, 'duration_ms': 180_000}})
    c = join(app_module)
    for t, dur in (('1' * 22, 200_000), ('2' * 22, 100_000)):
        c.post('/api/queue', json={'track_id': t, 'track_name': 'S', 'artist': 'A',
                                   'duration_ms': dur})
    queue = c.get('/api/status').get_json()['queue']
    assert [q['eta_ms'] for q in queue] == [120_000, 320_000]
