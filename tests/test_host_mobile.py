"""The phone host panel's backend: the lights proxy, the stats, and the
photos password that lets trusted guests review photos and nothing else."""
import io
import tempfile

import pytest
from PIL import Image


@pytest.fixture
def app_module(db, monkeypatch):
    import app as app_module
    import photos
    monkeypatch.setattr(photos, 'PHOTOS_DIR', tempfile.mkdtemp())
    monkeypatch.setattr(app_module.sc, 'is_authenticated', lambda: True)
    monkeypatch.setattr(app_module.sc, 'get_current_playback', lambda: None)
    return app_module


def host_client(app_module):
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s['is_host'] = True
    return c


def guest_client(app_module):
    c = app_module.app.test_client()
    c.get(f"/?p={app_module.db.get_setting('party_code')}")
    return c


# ---------------------------------------------------------------------------
# Lights proxy
# ---------------------------------------------------------------------------
LIGHTS_STATE = {
    'state': {'mode': 'manual', 'blackout': False, 'master': 0.8, 'vibe': 0.5,
              'look': 'wash', 'palette': 'blood-moon'},
    'dmx': {'fps': 40.0, 'target_fps': 40.0, 'late': 0},
}


@pytest.fixture
def lights(app_module, monkeypatch):
    """Fake party-lights: records calls, answers /api/state."""
    calls = []

    def fake(method, path, body=None):
        calls.append((method, path, body))
        if fake.down:
            raise ConnectionError('refused')
        return 200, LIGHTS_STATE if path == '/api/state' else {}
    fake.down = False
    monkeypatch.setattr(app_module, '_lights_call', fake)
    fake.calls = calls
    return fake


def test_lights_need_the_host(app_module, lights):
    c = guest_client(app_module)
    assert c.get('/api/host/lights').status_code == 401
    assert c.post('/api/host/lights', json={'action': 'blackout'}).status_code == 401
    assert lights.calls == []


def test_lights_state_is_trimmed_and_names_the_preset(app_module, lights):
    data = host_client(app_module).get('/api/host/lights').get_json()
    assert data['online'] is True
    assert data['preset'] == 'blood-moon'
    assert data['fps'] == 40.0 and data['master'] == 0.8
    assert len(data['presets']) == 8


def test_lights_offline_is_not_an_error(app_module, lights):
    lights.down = True
    res = host_client(app_module).get('/api/host/lights')
    assert res.status_code == 200 and res.get_json() == {'online': False}
    res = host_client(app_module).post('/api/host/lights', json={'action': 'blackout'})
    assert res.status_code == 502


@pytest.mark.parametrize('body,call', [
    ({'action': 'blackout'}, ('POST', '/api/cue/blackout', None)),
    ({'action': 'auto'}, ('POST', '/api/cue/resume-auto', None)),
    ({'action': 'preset', 'name': 'toxic'}, ('POST', '/api/cue/preset/toxic', None)),
    ({'action': 'master', 'value': 0.25}, ('POST', '/api/master', {'master': 0.25})),
    ({'action': 'vibe', 'value': 1}, ('POST', '/api/vibe', {'vibe': 1.0})),
])
def test_lights_controls_map_to_party_lights(app_module, lights, body, call):
    res = host_client(app_module).post('/api/host/lights', json=body)
    assert res.status_code == 200
    assert lights.calls[0] == call


@pytest.mark.parametrize('body', [
    {'action': 'freeze'},
    {'action': 'preset', 'name': '../layout'},
    {'action': 'master', 'value': 2},
    {'action': 'vibe', 'value': 'loud'},
    {},
])
def test_lights_only_forward_the_allowlist(app_module, lights, body):
    res = host_client(app_module).post('/api/host/lights', json=body)
    assert res.status_code == 400
    assert lights.calls == []


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def test_stats_count_tonight(app_module, db):
    conn = db.get_connection()
    conn.executescript("""
        INSERT INTO users (user_id, nickname, name_set) VALUES
            ('a', 'Ann', 1), ('b', 'Bo', 1), ('c', 'Cy', 0);
        INSERT INTO queue (id, spotify_track_id, track_name, artist, duration_ms,
                           requested_by, status) VALUES
            (1, 't1', 'One', 'X', 1000, 'a', 'played'),
            (2, 't2', 'Two', 'X', 1000, 'a', 'skipped'),
            (3, 't3', 'Three', 'X', 60000, 'b', 'pending'),
            (4, 't4', 'Old', 'X', 1000, 'b', 'played');
        UPDATE queue SET added_at = '2000-01-01 00:00:00' WHERE id = 4;
        INSERT INTO upvotes (queue_id, user_id) VALUES (3, 'a'), (3, 'c');
        INSERT INTO downvotes (queue_id, user_id) VALUES (2, 'b');
        INSERT INTO reactions (user_id, reaction, queue_id) VALUES ('b', 'heart', 1);
    """)
    conn.commit()
    conn.close()

    s = host_client(app_module).get('/api/host/stats').get_json()
    assert s['guests'] == 2
    assert s['songs_played'] == 1          # the 2000 one is another night
    assert s['songs_skipped'] == 1
    assert s['songs_requested'] == 3
    assert s['queue_depth'] == 1 and s['queue_ms'] == 60000
    assert s['top_requesters'][0] == {'nickname': 'Ann', 'n': 2}
    assert s['most_upvoted']['track_name'] == 'Three' and s['most_upvoted']['votes'] == 2
    assert s['most_downvoted']['track_name'] == 'Two'
    assert s['most_hearted']['track_name'] == 'One' and s['hearts'] == 1
    assert s['active_now'] == 2            # a and b queued/reacted just now


def test_stats_need_the_host(app_module):
    assert guest_client(app_module).get('/api/host/stats').status_code == 401


def test_new_party_restarts_the_count(app_module, db):
    c = host_client(app_module)
    db.get_or_create_host_user()
    db.add_to_queue('t1', 'One', 'X', '', 1000, 'host')
    assert c.get('/api/host/stats').get_json()['songs_requested'] == 1
    db.set_setting('party_started_at', '2999-01-01 00:00:00')
    assert c.get('/api/host/stats').get_json()['songs_requested'] == 0


# ---------------------------------------------------------------------------
# Photos password
# ---------------------------------------------------------------------------
def jpeg():
    buf = io.BytesIO()
    Image.new('RGB', (64, 48), (200, 80, 40)).save(buf, 'JPEG')
    return buf.getvalue()


def shoot(app_module, shot_id):
    c = guest_client(app_module)
    c.post('/api/user/nickname', json={'nickname': 'Shooter'})
    res = c.post('/api/photos', data={'shot_id': shot_id, 'photo': (io.BytesIO(jpeg()), 's.jpg')},
                 content_type='multipart/form-data')
    assert res.status_code == 200, res.get_json()


def reviewer_client(app_module, password='spooky-pics'):
    c = app_module.app.test_client()
    res = c.post('/api/photos/login', json={'password': password})
    return c, res


@pytest.fixture
def photos_pw(app_module, db):
    db.set_photos_password('spooky-pics')


def test_photos_login_is_off_until_a_password_is_set(app_module):
    _, res = reviewer_client(app_module)
    assert res.status_code == 401


def test_reviewer_can_review_tonights_photos(app_module, db, photos_pw):
    shoot(app_module, 'shot-0001')
    c, res = reviewer_client(app_module)
    assert res.status_code == 200 and res.get_json()['is_host'] is False
    assert c.get('/api/photos/auth_check').get_json() == {'authenticated': True, 'is_host': False}

    listed = c.get('/api/host/photos').get_json()['photos']
    assert len(listed) == 1
    pid = listed[0]['id']
    assert c.get(f'/api/host/photos/{pid}/image').status_code == 200
    res = c.post('/api/host/photos/review', json={'ids': [pid], 'status': 'approved'})
    assert res.status_code == 200
    assert db.get_photo(pid)['status'] == 'approved'


def test_reviewer_gets_nothing_else(app_module, photos_pw):
    c, _ = reviewer_client(app_module)
    assert c.get('/api/host/auth_check').get_json() == {'authenticated': False}
    for url in ('/api/host/camera', '/api/host/users', '/api/host/stats', '/api/host/lights'):
        assert c.get(url).status_code == 401, url
    assert c.post('/api/host/skip').status_code == 401
    assert c.post('/api/host/settings', json={'photos_password': 'mine-now'}).status_code == 401


def test_reviewer_is_kept_to_tonight(app_module, db, photos_pw):
    shoot(app_module, 'shot-0001')
    old_id = db.list_photos()[0]['id']
    old_party = db.get_setting('party_code')
    db.rotate_party_code()

    c, _ = reviewer_client(app_module)
    # Asking for the old party still answers with tonight's (empty) list.
    data = c.get(f'/api/host/photos?party={old_party}').get_json()
    assert data['party'] != old_party and data['photos'] == []
    assert c.get(f'/api/host/photos/{old_id}/image').status_code == 404
    res = c.post('/api/host/photos/review', json={'ids': [old_id], 'status': 'rejected'})
    assert res.status_code == 404
    assert db.get_photo(old_id)['status'] == 'pending'

    # The host still can.
    h = host_client(app_module)
    assert h.get(f'/api/host/photos?party={old_party}').get_json()['photos'][0]['id'] == old_id


def test_changing_the_photos_password_signs_reviewers_out(app_module, photos_pw):
    c, _ = reviewer_client(app_module)
    assert c.get('/api/host/photos').status_code == 200
    res = host_client(app_module).post('/api/host/settings', json={'photos_password': 'new-spooky'})
    assert res.status_code == 200
    assert c.get('/api/host/photos').status_code == 401
    _, res = reviewer_client(app_module, 'spooky-pics')
    assert res.status_code == 401
    _, res = reviewer_client(app_module, 'new-spooky')
    assert res.status_code == 200


def test_photos_password_must_differ_from_the_host_password(app_module, db):
    db.set_host_password('host-password-1')
    res = host_client(app_module).post('/api/host/settings', json={'photos_password': 'host-password-1'})
    assert res.status_code == 400
    assert not db.check_photos_password('host-password-1')


def test_host_password_works_on_the_photos_login(app_module, db):
    db.set_host_password('host-password-1')
    c, res = reviewer_client(app_module, 'host-password-1')
    assert res.status_code == 200 and res.get_json()['is_host'] is True
    assert c.get('/api/host/camera').status_code == 200


def test_phone_page_renders(app_module):
    res = app_module.app.test_client().get('/host/m')
    assert res.status_code == 200
    assert b'host_m.js' in res.data and b'host_m.css' in res.data


def test_photos_password_env_seeds_once(db, monkeypatch):
    monkeypatch.setenv('PHOTOS_PASSWORD', 'from-env-1')
    db.init_db()
    assert db.check_photos_password('from-env-1')
    db.set_photos_password('from-panel')
    db.init_db()  # a restart must not undo the host panel's change
    assert db.check_photos_password('from-panel')
    assert not db.check_photos_password('from-env-1')
