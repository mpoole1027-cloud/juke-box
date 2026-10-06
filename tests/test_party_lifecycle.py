"""New-party reset, idle standby, and device pinning."""
import time
from conftest import TRACK_A, TRACK_B


def test_played_tracks_block_requests_until_reset(db, guest):
    db.mark_track_played(TRACK_A)
    assert db.track_played_tonight(TRACK_A) is True

    stats = db.start_new_party()

    assert stats['played_cleared'] == 1
    assert db.track_played_tonight(TRACK_A) is False, \
        "a new party must make previously played songs requestable again"


def test_new_party_clears_bans_and_skip_counts(db, guest):
    db.increment_skip_count(guest)
    db.ban_user(guest)
    stats = db.start_new_party()

    assert stats['unbanned'] == 1
    row = [u for u in db.get_all_users() if u['user_id'] == guest][0]
    assert row['is_banned'] == 0
    assert row['songs_skipped_count'] == 0


def test_new_party_keeps_guests_by_default(db, guest):
    db.start_new_party()
    assert any(u['user_id'] == guest for u in db.get_all_users())


def test_new_party_can_wipe_guests(db, guest):
    db.add_to_queue(TRACK_A, 'A', 'Artist', None, 1000, guest)
    db.start_new_party(clear_users=True)
    assert db.get_all_users() == []


def test_new_party_empties_the_queue(db, guest):
    db.add_to_queue(TRACK_A, 'A', 'Artist', None, 1000, guest)
    db.add_to_queue(TRACK_B, 'B', 'Artist', None, 1000, guest)
    stats = db.start_new_party()
    assert stats['queue_cleared'] == 2
    assert db.get_pending_queue() == []


def test_pinned_device_wins_over_active(qm, db):
    qm.fake.devices[1]['is_active'] = True          # iPhone is active
    db.set_setting('preferred_device_id', 'speaker')
    assert qm.get_party_device_id() == 'speaker'


def test_falls_back_when_pinned_device_is_offline(qm, db):
    db.set_setting('preferred_device_id', 'chromecast-that-went-away')
    qm.fake.devices[1]['is_active'] = True
    assert qm.get_party_device_id() == 'phone'


def test_no_pin_uses_active_device(qm, db):
    qm.fake.devices[1]['is_active'] = True
    assert qm.get_party_device_id() == 'phone'


MAC = {'id': 'mac', 'name': 'MacBook Pro', 'type': 'Computer', 'is_active': False}


def test_lone_computer_is_auto_pinned(qm, db):
    qm.fake.devices.append(dict(MAC))
    qm.fake.devices[1]['is_active'] = True          # iPhone is active
    assert qm.get_party_device_id() == 'mac'
    assert db.get_setting('preferred_device_id') == 'mac'


def test_no_auto_pin_without_exactly_one_computer(qm, db):
    qm.fake.devices[1]['is_active'] = True
    assert qm.get_party_device_id() == 'phone'      # no computer online
    qm.fake.devices += [dict(MAC), dict(MAC, id='mac2', name='iMac')]
    assert qm.get_party_device_id() == 'phone'      # two: ambiguous
    assert not db.get_setting('preferred_device_id')


def test_auto_pin_never_overrides_host_choice(qm, db):
    qm.fake.devices.append(dict(MAC))
    db.set_setting('preferred_device_id', 'speaker')
    assert qm.get_party_device_id() == 'speaker'
    assert db.get_setting('preferred_device_id') == 'speaker'


def test_idle_standby_disabled_by_zero(qm, db):
    db.set_setting('idle_shutdown_hours', 0)
    assert qm.idle_seconds() == 0


def test_resume_clears_standby(qm):
    qm._standing_down = True
    assert qm.resume_party() is True
    assert qm.is_standing_down() is False
    assert qm.resume_party() is False       # already running


def test_activity_defers_standby(qm, db):
    db.set_setting('idle_shutdown_hours', 1)
    qm._last_activity = time.monotonic() - 7200      # 2h ago -> would stand down
    qm.note_activity()
    assert time.monotonic() - qm._last_activity < 1
