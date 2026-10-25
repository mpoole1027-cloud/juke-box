"""Develop at midnight: photos stay in the darkroom, then go up on /wall."""
import time
from datetime import datetime

from test_camera import app_module, guest_client, host_client, shoot  # noqa: F401


def photo_ids(db):
    return [p['id'] for p in db.list_photos(party_code=db.get_setting('party_code'))]


def test_default_is_the_first_midnight_after_the_party_started(app_module, db):
    db.set_setting('party_started_at', '2026-10-31 02:30:00')       # 7:30 pm in Phoenix
    start = datetime.strptime('2026-10-31 02:30:00', '%Y-%m-%d %H:%M:%S')
    develop = datetime.fromtimestamp(app_module._develop_at()).astimezone()
    assert (develop.hour, develop.minute) == (0, 0)
    local_start = datetime.fromtimestamp(app_module._sqlite_to_epoch('2026-10-31 02:30:00'))
    assert 0 < (develop.replace(tzinfo=None) - local_start).total_seconds() <= 86400
    assert start  # format sanity


def test_photos_stay_hidden_until_they_develop(app_module, db):
    g = guest_client(app_module)
    shoot(g, 'shot-000a')
    pid = photo_ids(db)[0]
    db.set_photo_status([pid], 'approved')
    db.set_setting('develop_at', str(time.time() + 3600))
    wall = g.get('/api/wall').get_json()
    assert wall['developed'] is False and wall['photos'] == [] and wall['shots'] == 1
    assert g.get(f'/api/wall/photo/{pid}').status_code == 404


def test_developing_shows_approved_photos_only(app_module, db):
    g, h = guest_client(app_module), host_client(app_module)
    for i in range(3):
        shoot(g, f'shot-{i:04d}')
    approved, pending, rejected = photo_ids(db)
    db.set_photo_status([approved], 'approved')
    db.set_photo_status([rejected], 'rejected')
    assert h.post('/api/host/settings', json={'develop_now': True}).status_code == 200

    wall = g.get('/api/wall').get_json()
    assert wall['developed'] and [p['id'] for p in wall['photos']] == [approved]
    res = g.get(f'/api/wall/photo/{approved}')
    assert res.status_code == 200 and res.mimetype == 'image/jpeg'
    assert 'private' in res.headers['Cache-Control']
    assert g.get(f'/api/wall/photo/{pending}').status_code == 404

    h.post('/api/host/settings', json={'wall_include_pending': True})
    ids = [p['id'] for p in g.get('/api/wall').get_json()['photos']]
    assert ids == [approved, pending]
    assert g.get(f'/api/wall/photo/{rejected}').status_code == 404


def test_the_wall_needs_tonights_code(app_module, db):
    outsider = app_module.app.test_client()
    assert outsider.get('/api/wall').status_code == 403
    assert outsider.get('/api/wall/photo/1').status_code == 403


def test_last_partys_photos_never_show(app_module, db):
    g, h = guest_client(app_module), host_client(app_module)
    shoot(g, 'shot-0old')
    old = photo_ids(db)[0]
    db.set_photo_status([old], 'approved')
    h.post('/api/host/new_party', json={})
    assert db.get_setting('develop_at') == ''
    h.post('/api/host/settings', json={'develop_now': True})
    g2 = guest_client(app_module)
    assert g2.get('/api/wall').get_json()['photos'] == []
    assert g2.get(f'/api/wall/photo/{old}').status_code == 404


def test_host_sets_and_resets_the_develop_time(app_module, db):
    h = host_client(app_module)
    when = time.time() + 7200
    assert h.post('/api/host/settings', json={'develop_at': when}).status_code == 200
    cam = h.get('/api/host/camera').get_json()
    assert abs(cam['develop_at'] - when) < 1 and cam['develop_set'] and not cam['developed']
    assert cam['wall_url'].endswith('/wall?p=' + db.get_setting('party_code'))
    assert h.post('/api/host/settings', json={'develop_at': 'soon'}).status_code == 400
    assert h.post('/api/host/settings', json={'develop_at': time.time() + 30 * 86400}).status_code == 400
    h.post('/api/host/settings', json={'develop_at': None})
    assert not h.get('/api/host/camera').get_json()['develop_set']


def test_tv_reports_when_photos_develop(app_module, db):
    h = host_client(app_module)
    db.set_setting('develop_at', str(time.time() + 60))
    assert h.get('/api/tv').get_json()['developed'] is False
    h.post('/api/host/settings', json={'develop_now': True})
    assert h.get('/api/tv').get_json()['developed'] is True
    assert h.get('/wall').status_code == 200
