"""Costume contest: guests enter and vote on their phones, the TV reveals the winner."""
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


def guest_client(app_module, nickname=None):
    c = app_module.app.test_client()
    c.get(f"/?p={app_module.db.get_setting('party_code')}")
    if nickname:
        c.post('/api/user/nickname', json={'nickname': nickname})
    return c


def host_client(app_module):
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s['is_host'] = True
    return c


def set_phase(app_module, phase):
    res = host_client(app_module).post('/api/host/costume/phase', json={'phase': phase})
    assert res.status_code == 200


def costume(client):
    return client.get('/api/status').get_json()['costume']


def jpeg(size=(800, 600), color=(120, 40, 160)):
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, 'JPEG')
    return buf.getvalue()


def post_entry(client, name, photo=True):
    data = {'costume': name}
    if photo:
        data['photo'] = (io.BytesIO(jpeg() if photo is True else photo), 'costume.jpg')
    return client.post('/api/costume/entry', data=data, content_type='multipart/form-data')


def enter(client, name, photo=True):
    res = post_entry(client, name, photo)
    assert res.status_code == 200, res.get_json()
    return res.get_json()['entry']['id']


def entry_file(app_module, entry_id):
    import photos
    return photos.path_for(app_module.db.get_costume_entry(entry_id)['photo'])


def test_contest_is_hidden_until_the_host_opens_it(app_module):
    c = guest_client(app_module)
    assert costume(c) == {'phase': 'off'}
    res = post_entry(c, 'Dracula')
    assert res.status_code == 409 and res.get_json()['code'] == 'contest_closed'
    assert host_client(app_module).get('/api/tv').get_json()['costume'] == {'phase': 'off'}


def test_guests_enter_and_vote_without_seeing_counts(app_module):
    set_phase(app_module, 'open')
    ann, bob, cat = (guest_client(app_module, n) for n in ('Ann', 'Bob', 'Cat'))
    witch = enter(ann, '  Wicked   Witch ')
    zombie = enter(bob, 'Zombie Elvis')

    assert cat.post('/api/costume/vote', json={'entry_id': witch}).status_code == 200
    state = costume(cat)
    assert state['my_vote'] == witch and state['my_entry'] is None
    # Alphabetical by costume, whitespace tidied, and no vote counts anywhere.
    assert [e['costume'] for e in state['entries']] == ['Wicked Witch', 'Zombie Elvis']
    assert all(set(e) == {'id', 'nickname', 'costume', 'photo_url', 'is_mine'}
               for e in state['entries'])
    assert 'results' not in state

    mine = costume(ann)
    assert mine['my_entry']['id'] == witch and mine['my_entry']['costume'] == 'Wicked Witch'
    assert mine['my_entry']['photo_url'].startswith(f'/api/costume/photo/{witch}?v=')
    assert [e['is_mine'] for e in mine['entries']] == [True, False]

    # Voting again moves the vote instead of adding one.
    cat.post('/api/costume/vote', json={'entry_id': zombie})
    tally = host_client(app_module).get('/api/host/costume').get_json()
    assert tally['votes'] == 1
    assert {e['costume']: e['votes'] for e in tally['entries']} == {'Zombie Elvis': 1, 'Wicked Witch': 0}


def test_no_voting_for_yourself(app_module):
    set_phase(app_module, 'open')
    ann = guest_client(app_module, 'Ann')
    witch = enter(ann, 'Witch')
    res = ann.post('/api/costume/vote', json={'entry_id': witch})
    assert res.status_code == 403 and res.get_json()['code'] == 'own_entry'
    assert costume(ann)['my_vote'] is None


def test_renaming_an_entry_keeps_its_votes(app_module):
    set_phase(app_module, 'open')
    ann, bob = guest_client(app_module, 'Ann'), guest_client(app_module, 'Bob')
    witch = enter(ann, 'Witch')
    bob.post('/api/costume/vote', json={'entry_id': witch})
    assert enter(ann, 'Good Witch', photo=False) == witch
    entries = host_client(app_module).get('/api/host/costume').get_json()['entries']
    assert [{k: e[k] for k in ('id', 'nickname', 'costume', 'votes', 'rank')} for e in entries] == [
        {'id': witch, 'nickname': 'Ann', 'costume': 'Good Witch', 'votes': 1, 'rank': 1}]
    assert entries[0]['photo_url']


def test_entry_validation(app_module):
    set_phase(app_module, 'open')
    c = guest_client(app_module)
    assert post_entry(c, '   ').status_code == 400
    assert post_entry(c, 'x' * 41).status_code == 400
    assert c.post('/api/costume/vote', json={'entry_id': 'abc'}).status_code == 400
    assert c.post('/api/costume/vote', json={'entry_id': 999}).status_code == 404


def test_withdrawing_takes_its_votes_with_it(app_module):
    set_phase(app_module, 'open')
    ann, bob = guest_client(app_module, 'Ann'), guest_client(app_module, 'Bob')
    witch = enter(ann, 'Witch')
    path = entry_file(app_module, witch)
    bob.post('/api/costume/vote', json={'entry_id': witch})
    assert ann.delete('/api/costume/entry').status_code == 200
    assert not os.path.exists(path)
    assert costume(bob)['my_vote'] is None
    assert costume(bob)['entries'] == []
    assert ann.delete('/api/costume/entry').status_code == 404


def test_host_can_remove_an_entry(app_module):
    set_phase(app_module, 'open')
    ann = guest_client(app_module, 'Ann')
    witch = enter(ann, 'Something rude')
    path = entry_file(app_module, witch)
    host = host_client(app_module)
    assert host.delete(f'/api/host/costume/entry/{witch}').status_code == 200
    assert not os.path.exists(path)
    assert costume(ann)['my_entry'] is None
    assert host.delete(f'/api/host/costume/entry/{witch}').status_code == 404


def test_banned_guests_sit_it_out(app_module):
    set_phase(app_module, 'open')
    ann, bob = guest_client(app_module, 'Ann'), guest_client(app_module, 'Bob')
    witch = enter(ann, 'Witch')
    bob_id = next(u['user_id'] for u in app_module.db.get_all_users() if u['nickname'] == 'Bob')
    host_client(app_module).post('/api/host/ban', json={'user_id': bob_id})
    res = bob.post('/api/costume/vote', json={'entry_id': witch})
    assert res.status_code == 403 and res.get_json()['code'] == 'banned'


def test_closing_reveals_ranked_results_with_ties(app_module):
    set_phase(app_module, 'open')
    ann, bob, cat, dan = (guest_client(app_module, n) for n in ('Ann', 'Bob', 'Cat', 'Dan'))
    witch, zombie = enter(ann, 'Witch'), enter(bob, 'Zombie')
    ghost = enter(cat, 'Ghost')
    for voter, choice in ((bob, witch), (cat, witch), (dan, zombie), (ann, zombie)):
        voter.post('/api/costume/vote', json={'entry_id': choice})

    set_phase(app_module, 'closed')
    results = costume(dan)['results']
    assert [(r['costume'], r['votes'], r['rank']) for r in results] == [
        ('Witch', 2, 1), ('Zombie', 2, 1), ('Ghost', 0, 3)]
    # Voting is over.
    assert dan.post('/api/costume/vote', json={'entry_id': ghost}).status_code == 409

    tv = host_client(app_module).get('/api/tv').get_json()['costume']
    assert tv['phase'] == 'closed' and tv['entries'] == 3 and tv['votes'] == 4
    assert tv['results'][0]['costume'] == 'Witch' and tv['closed_secs_ago'] < 5


def test_tv_shows_only_counts_while_open(app_module):
    set_phase(app_module, 'open')
    ann, bob = guest_client(app_module, 'Ann'), guest_client(app_module, 'Bob')
    bob.post('/api/costume/vote', json={'entry_id': enter(ann, 'Witch')})
    tv = host_client(app_module).get('/api/tv').get_json()['costume']
    assert tv == {'phase': 'open', 'entries': 1, 'votes': 1}


def test_new_party_starts_an_empty_contest(app_module):
    set_phase(app_module, 'open')
    enter(guest_client(app_module, 'Ann'), 'Witch')
    host = host_client(app_module)
    host.post('/api/host/new_party', json={})
    assert host.get('/api/host/costume').get_json() == {'phase': 'off', 'entries': [], 'votes': 0}


def test_phase_validation_and_host_only(app_module):
    host = host_client(app_module)
    assert host.post('/api/host/costume/phase', json={'phase': 'party'}).status_code == 400
    guest = guest_client(app_module)
    assert guest.post('/api/host/costume/phase', json={'phase': 'open'}).status_code == 401
    assert guest.get('/api/host/costume').status_code == 401


def test_a_photo_is_required_to_enter(app_module):
    set_phase(app_module, 'open')
    c = guest_client(app_module)
    res = post_entry(c, 'Witch', photo=False)
    assert res.status_code == 400 and res.get_json()['code'] == 'photo_required'
    res = post_entry(c, 'Witch', photo=b'not an image')
    assert res.status_code == 400 and res.get_json()['code'] == 'bad_photo'
    assert costume(c)['entries'] == []


def test_photos_are_square_and_shared_with_the_party_only(app_module):
    set_phase(app_module, 'open')
    ann, bob = guest_client(app_module, 'Ann'), guest_client(app_module, 'Bob')
    enter(ann, 'Witch', photo=jpeg((3000, 1800)))
    url = costume(bob)['entries'][0]['photo_url']

    res = bob.get(url)
    assert res.status_code == 200 and res.mimetype == 'image/jpeg'
    with Image.open(io.BytesIO(res.data)) as img:
        assert img.size == (1024, 1024)
    assert host_client(app_module).get(url).status_code == 200
    # No party code, no photo.
    assert app_module.app.test_client().get(url).status_code == 403


def test_a_new_photo_replaces_the_old_file(app_module):
    set_phase(app_module, 'open')
    ann = guest_client(app_module, 'Ann')
    witch = enter(ann, 'Witch')
    old_path, old_url = entry_file(app_module, witch), costume(ann)['my_entry']['photo_url']
    assert enter(ann, 'Witch', photo=jpeg(color=(0, 200, 0))) == witch
    assert not os.path.exists(old_path) and os.path.exists(entry_file(app_module, witch))
    assert costume(ann)['my_entry']['photo_url'] != old_url


def test_last_partys_photos_are_not_served(app_module):
    set_phase(app_module, 'open')
    ann = guest_client(app_module, 'Ann')
    enter(ann, 'Witch')
    url = costume(ann)['entries'][0]['photo_url']
    host = host_client(app_module)
    host.post('/api/host/new_party', json={})
    assert host.get(url).status_code == 404


def test_tv_and_results_carry_photos(app_module):
    set_phase(app_module, 'open')
    ann, bob = guest_client(app_module, 'Ann'), guest_client(app_module, 'Bob')
    bob.post('/api/costume/vote', json={'entry_id': enter(ann, 'Witch')})
    set_phase(app_module, 'closed')
    assert costume(bob)['results'][0]['photo_url']
    assert host_client(app_module).get('/api/tv').get_json()['costume']['results'][0]['photo_url']
