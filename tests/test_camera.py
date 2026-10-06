"""Disposable camera: guests shoot blind, the host reviews, nothing leaks."""
import io
import os
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


def jpeg(size=(640, 480), exif_gps=False):
    img = Image.new('RGB', size, (200, 80, 40))
    buf = io.BytesIO()
    if exif_gps:
        exif = Image.Exif()
        exif[0x010F] = 'PhoneMaker'           # Make
        exif[0x8825] = {1: 'N', 2: (45.0, 30.0, 0.0)}  # GPSInfo
        img.save(buf, 'JPEG', exif=exif)
    else:
        img.save(buf, 'JPEG')
    return buf.getvalue()


def guest_client(app_module):
    c = app_module.app.test_client()
    c.get(f"/?p={app_module.db.get_setting('party_code')}")
    return c


def host_client(app_module):
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s['is_host'] = True
    return c


def shoot(client, shot_id='shot-0001', data=None):
    return client.post('/api/photos', data={
        'shot_id': shot_id,
        'photo': (io.BytesIO(data if data is not None else jpeg()), 'shot.jpg'),
    }, content_type='multipart/form-data')


def test_shot_is_stored_and_counted(app_module, db):
    c = guest_client(app_module)
    before = c.get('/api/status').get_json()['camera']
    assert before == {'enabled': True, 'shots_total': 24, 'shots_left': 24}

    res = shoot(c)
    assert res.status_code == 200 and res.get_json()['shots_left'] == 23
    assert c.get('/api/status').get_json()['camera']['shots_left'] == 23

    [photo] = db.list_photos()
    assert photo['status'] == 'pending'
    assert photo['party_code'] == db.get_setting('party_code')
    import photos
    assert os.path.exists(photos.path_for(photo['filename']))


def test_retried_upload_is_stored_once(app_module, db):
    c = guest_client(app_module)
    assert shoot(c, 'same-shot-1').status_code == 200
    again = shoot(c, 'same-shot-1')
    assert again.status_code == 200 and again.get_json()['duplicate'] is True
    assert len(db.list_photos()) == 1


def test_out_of_film(app_module, db):
    db.set_setting('camera_shots_per_guest', 2)
    c = guest_client(app_module)
    assert shoot(c, 'shot-aaaa1').status_code == 200
    assert shoot(c, 'shot-aaaa2').status_code == 200
    res = shoot(c, 'shot-aaaa3')
    assert res.status_code == 409 and res.get_json()['code'] == 'out_of_film'
    assert len(db.list_photos()) == 2


def test_film_resets_for_a_new_party(app_module, db, monkeypatch):
    db.set_setting('camera_shots_per_guest', 1)
    c = guest_client(app_module)
    assert shoot(c, 'shot-old-01').status_code == 200
    monkeypatch.setattr(app_module.qm, 'resume_party', lambda: False)
    host_client(app_module).post('/api/host/new_party', json={})
    c.get(f"/?p={db.get_setting('party_code')}")
    assert shoot(c, 'shot-new-01').status_code == 200


def test_metadata_is_stripped_and_size_capped(app_module, db):
    c = guest_client(app_module)
    assert shoot(c, data=jpeg(size=(4000, 3000), exif_gps=True)).status_code == 200
    import photos
    with Image.open(photos.path_for(db.list_photos()[0]['filename'])) as img:
        assert max(img.size) == photos.MAX_EDGE
        assert not img.getexif()


def test_garbage_is_rejected(app_module, db):
    c = guest_client(app_module)
    res = shoot(c, data=b'<?php echo "hi"; ?>')
    assert res.status_code == 400 and res.get_json()['code'] == 'bad_photo'
    assert db.list_photos() == []


def test_needs_party_code(app_module):
    c = app_module.app.test_client()
    assert shoot(c).status_code == 403


def test_camera_off_and_banned(app_module, db):
    c = guest_client(app_module)
    db.set_setting('camera_enabled', '0')
    assert shoot(c).get_json()['code'] == 'camera_off'
    db.set_setting('camera_enabled', '1')
    with c.session_transaction() as s:
        uid = s['guest_id']
    db.ban_user(uid)
    assert shoot(c).get_json()['code'] == 'banned'


def test_bad_shot_id(app_module):
    c = guest_client(app_module)
    assert shoot(c, shot_id='../../etc').status_code == 400
    assert shoot(c, shot_id='x').status_code == 400


def test_guests_cannot_see_or_review_photos(app_module, db):
    c = guest_client(app_module)
    shoot(c)
    pid = db.list_photos()[0]['id']
    assert c.get('/api/host/photos').status_code == 401
    assert c.get(f'/api/host/photos/{pid}/image').status_code == 401
    assert c.post('/api/host/photos/review',
                  json={'ids': [pid], 'status': 'approved'}).status_code == 401


def test_host_reviews_photos(app_module, db):
    c = guest_client(app_module)
    shoot(c, 'shot-rev-01')
    shoot(c, 'shot-rev-02')
    h = host_client(app_module)

    listed = h.get('/api/host/photos').get_json()['photos']
    assert [p['status'] for p in listed] == ['pending', 'pending']
    a, b = (p['id'] for p in listed)

    img = h.get(f'/api/host/photos/{a}/image')
    assert img.status_code == 200 and img.mimetype == 'image/jpeg'
    assert 'no-store' in img.headers['Cache-Control']

    assert h.post('/api/host/photos/review', json={'ids': [a], 'status': 'approved'}).status_code == 200
    assert h.post('/api/host/photos/review', json={'ids': [b], 'status': 'rejected'}).status_code == 200
    assert [p['id'] for p in h.get('/api/host/photos?status=approved').get_json()['photos']] == [a]
    assert h.post('/api/host/photos/review', json={'ids': [a], 'status': 'bogus'}).status_code == 400

    [party] = h.get('/api/host/camera').get_json()['parties']
    assert (party['approved'], party['rejected'], party['pending']) == (1, 1, 0)


def test_host_camera_settings(app_module, db):
    h = host_client(app_module)
    assert h.post('/api/host/settings', json={'camera_shots_per_guest': 0}).status_code == 400
    assert h.post('/api/host/settings', json={'camera_enabled': False,
                                               'camera_shots_per_guest': 10}).status_code == 200
    cam = h.get('/api/host/camera').get_json()
    assert cam['enabled'] is False and cam['shots_per_guest'] == 10


def test_oversized_upload_is_refused(app_module):
    c = guest_client(app_module)
    res = shoot(c, data=b'\xff' * (16 * 1024 * 1024))
    assert res.status_code == 413


def test_export_builds_gallery_from_approved_only(app_module, db, tmp_path, monkeypatch):
    c = guest_client(app_module)
    for i in range(3):
        shoot(c, f'shot-exp-0{i}')
    a, b, _ = (p['id'] for p in db.list_photos())
    db.set_photo_status([a, b], 'approved')
    db.set_photo_status([_], 'rejected')

    sys_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'deploy')
    monkeypatch.syspath_prepend(sys_path)
    import export_photos
    party = export_photos.pick_party(None)
    out = tmp_path / 'gallery'
    assert export_photos.export(party, str(out), 'Halloween <2026>') == 2

    assert sorted(os.listdir(out / 'photos')) == ['photo-001.jpg', 'photo-002.jpg']
    page = (out / 'index.html').read_text()
    assert 'Halloween &lt;2026&gt;' in page and '"photos/photo-002.jpg"' in page
    assert 'noindex' in (out / '_headers').read_text()
    import zipfile
    assert len(zipfile.ZipFile(out / 'all-photos.zip').namelist()) == 2
