"""Costume contest: guests enter and vote on their phones, the TV reveals the winner."""
import pytest


@pytest.fixture
def app_module(db, monkeypatch):
    import app as app_module
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


def enter(client, name):
    res = client.post('/api/costume/entry', json={'costume': name})
    assert res.status_code == 200, res.get_json()
    return res.get_json()['entry']['id']


def test_contest_is_hidden_until_the_host_opens_it(app_module):
    c = guest_client(app_module)
    assert costume(c) == {'phase': 'off'}
    res = c.post('/api/costume/entry', json={'costume': 'Dracula'})
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
    assert all(set(e) == {'id', 'nickname', 'costume', 'is_mine'} for e in state['entries'])
    assert 'results' not in state

    mine = costume(ann)
    assert mine['my_entry'] == {'id': witch, 'costume': 'Wicked Witch'}
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
    assert enter(ann, 'Good Witch') == witch
    entries = host_client(app_module).get('/api/host/costume').get_json()['entries']
    assert entries == [{'id': witch, 'nickname': 'Ann', 'costume': 'Good Witch', 'votes': 1, 'rank': 1}]


def test_entry_validation(app_module):
    set_phase(app_module, 'open')
    c = guest_client(app_module)
    assert c.post('/api/costume/entry', json={'costume': '   '}).status_code == 400
    assert c.post('/api/costume/entry', json={'costume': 'x' * 41}).status_code == 400
    assert c.post('/api/costume/vote', json={'entry_id': 'abc'}).status_code == 400
    assert c.post('/api/costume/vote', json={'entry_id': 999}).status_code == 404


def test_withdrawing_takes_its_votes_with_it(app_module):
    set_phase(app_module, 'open')
    ann, bob = guest_client(app_module, 'Ann'), guest_client(app_module, 'Bob')
    witch = enter(ann, 'Witch')
    bob.post('/api/costume/vote', json={'entry_id': witch})
    assert ann.delete('/api/costume/entry').status_code == 200
    assert costume(bob)['my_vote'] is None
    assert costume(bob)['entries'] == []
    assert ann.delete('/api/costume/entry').status_code == 404


def test_host_can_remove_an_entry(app_module):
    set_phase(app_module, 'open')
    ann = guest_client(app_module, 'Ann')
    witch = enter(ann, 'Something rude')
    host = host_client(app_module)
    assert host.delete(f'/api/host/costume/entry/{witch}').status_code == 200
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
