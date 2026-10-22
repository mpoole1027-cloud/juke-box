"""Host volume control: read from the playback snapshot, set on the playing device."""
import pytest


@pytest.fixture
def app_module(db, monkeypatch):
    import app as app_module
    playback = {'item': None, 'device': {'id': 'speaker', 'name': 'Living Room', 'volume_percent': 40}}
    monkeypatch.setattr(app_module.qm, 'get_cached_playback', lambda: playback)
    calls = []

    def set_volume(percent, device_id=None):
        calls.append((percent, device_id))
        return set_volume.result
    set_volume.result = (True, None)
    monkeypatch.setattr(app_module.sc, 'set_volume', set_volume)
    app_module.volume_calls = calls
    app_module.fake_set_volume = set_volume
    return app_module


def host_client(app_module):
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s['is_host'] = True
    return c


def test_volume_needs_the_host(app_module):
    c = app_module.app.test_client()
    assert c.get('/api/host/volume').status_code == 401
    assert c.post('/api/host/volume', json={'volume': 50}).status_code == 401
    assert app_module.volume_calls == []


def test_volume_reads_the_snapshot(app_module):
    data = host_client(app_module).get('/api/host/volume').get_json()
    assert data == {'volume': 40, 'device': 'Living Room'}


def test_volume_sets_the_playing_device(app_module):
    res = host_client(app_module).post('/api/host/volume', json={'volume': 72.4})
    assert res.status_code == 200 and res.get_json()['volume'] == 72
    assert app_module.volume_calls == [(72, 'speaker')]


@pytest.mark.parametrize('bad', [None, 'loud', -1, 101])
def test_volume_rejects_bad_values(app_module, bad):
    res = host_client(app_module).post('/api/host/volume', json={'volume': bad})
    assert res.status_code == 400
    assert app_module.volume_calls == []


def test_speaker_that_refuses_volume_says_so(app_module):
    app_module.fake_set_volume.result = (False, 'http status: 403, code: -1 - VOLUME_CONTROL_DISALLOW')
    res = host_client(app_module).post('/api/host/volume', json={'volume': 50})
    assert res.status_code == 409 and "speaker" in res.get_json()['error']
