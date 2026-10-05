"""A party code in the QR link is what lets a guest in, not the URL alone."""
import pytest


@pytest.fixture
def app_module(db, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.sc, 'is_authenticated', lambda: True)
    monkeypatch.setattr(app_module.sc, 'get_current_playback', lambda: {
        'is_playing': True, 'progress_ms': 1000,
        'item': {'id': 'n' * 22, 'name': 'Now', 'artists': [{'name': 'Band'}],
                 'album': {'images': []}, 'duration_ms': 200000}})
    monkeypatch.setattr(app_module.sc, 'search_tracks', lambda q, limit=10: [])
    return app_module


def code(app_module):
    return app_module.db.get_setting('party_code')


def test_code_is_generated_and_readable(app_module):
    c = code(app_module)
    assert len(c) == 6 and all(ch in app_module.db.PARTY_CODE_ALPHABET for ch in c)


def test_guest_without_code_is_turned_away(app_module):
    c = app_module.app.test_client()
    c.get('/')
    res = c.get('/api/search?q=abba')
    assert res.status_code == 403
    assert res.get_json()['code'] == 'party_code_required'
    assert c.get('/api/tv').status_code == 403


def test_status_without_code_shows_only_now_playing(app_module, db):
    db.get_or_create_host_user()
    db.add_to_queue('q' * 22, 'Queued', 'Someone', '', 1000, 'host', dedication='secret')
    data = app_module.app.test_client().get('/api/status').get_json()
    assert data['party_code_required'] is True
    assert data['current_track'] == {
        'track_id': 'n' * 22, 'track_name': 'Now', 'artist': 'Band', 'is_playing': True}
    assert 'queue' not in data


def test_qr_link_lets_the_guest_in_and_strips_the_code(app_module):
    c = app_module.app.test_client()
    res = c.get(f'/?p={code(app_module).lower()}')
    assert res.status_code == 302 and res.headers['Location'] == '/'
    assert c.get('/api/search?q=abba').status_code == 200
    assert 'queue' in c.get('/api/status').get_json()


def test_wrong_code_is_flagged(app_module):
    c = app_module.app.test_client()
    res = c.get('/?p=WRONG1')
    assert 'bad_code' in res.headers['Location']
    assert c.get('/api/search?q=abba').status_code == 403


def test_manual_join(app_module):
    c = app_module.app.test_client()
    assert c.post('/api/join', json={'code': 'NOPE00'}).status_code == 403
    assert c.post('/api/join', json={'code': code(app_module)}).status_code == 200
    assert c.get('/api/search?q=abba').status_code == 200


def test_new_party_rotates_the_code(app_module, monkeypatch):
    monkeypatch.setattr(app_module.qm, 'resume_party', lambda: False)
    guest = app_module.app.test_client()
    guest.get(f'/?p={code(app_module)}')
    old = code(app_module)

    host = app_module.app.test_client()
    with host.session_transaction() as s:
        s['is_host'] = True
    data = host.post('/api/host/new_party', json={}).get_json()
    assert data['party_code'] != old
    assert data['invite_url'].endswith(f"/?p={data['party_code']}")
    assert guest.get('/api/search?q=abba').status_code == 403


def test_qr_is_host_only(app_module):
    assert app_module.app.test_client().get('/qr').status_code == 401
    host = app_module.app.test_client()
    with host.session_transaction() as s:
        s['is_host'] = True
    res = host.get('/qr')
    assert res.status_code == 200 and res.mimetype == 'image/png'


def test_tv_link_with_code_unlocks_the_tv_feed(app_module):
    tv = app_module.app.test_client()
    tv.get(f'/tv?p={code(app_module)}')
    assert tv.get('/api/tv').status_code == 200
