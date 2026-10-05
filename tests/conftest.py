"""Shared fixtures: a throwaway DB and a fake Spotify Connect account.

Every test gets its own sqlite file, so the real jukebox.db is never touched.
"""
import os
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FALLBACK_ID = 'PL00000000000000000000'
FALLBACK_TRACKS = [f'fb{i:020d}' for i in range(5)]
TRACK_A = 'a' * 22
TRACK_B = 'b' * 22


class FakeSpotify:
    """Stands in for spotify_client. Records every playback command issued."""

    def __init__(self):
        self.calls = []
        self.playing = None
        self.is_playing = False
        self.context = None
        self.progress = 0
        self.duration = 200_000
        self.fail = False
        self.devices = [
            {'id': 'speaker', 'name': 'Living Room', 'type': 'Speaker', 'is_active': False},
            {'id': 'phone', 'name': 'iPhone', 'type': 'Smartphone', 'is_active': False},
        ]
        self._lock = threading.Lock()

    # Mirrors the real module's constants so queue_manager can read them
    # off the fake exactly as it reads them off spotify_client.
    PLAYBACK_OK = 'ok'
    PLAYBACK_IDLE = 'idle'
    PLAYBACK_ERROR = 'error'

    # --- playback ---
    def get_playback_state(self):
        """Set .fail = True to simulate an unreachable Spotify / dead token."""
        if getattr(self, 'fail', False):
            return {'status': self.PLAYBACK_ERROR, 'playback': None,
                    'reason': 'simulated outage'}
        pb = self.get_current_playback()
        if not pb or not pb.get('item'):
            return {'status': self.PLAYBACK_IDLE, 'playback': pb}
        return {'status': self.PLAYBACK_OK, 'playback': pb}

    def get_current_playback(self):
        if self.playing is None:
            return None
        return {
            'item': {'id': self.playing, 'name': self.playing, 'duration_ms': self.duration,
                     'artists': [{'name': 'Artist'}], 'album': {'images': []}},
            'is_playing': self.is_playing,
            'progress_ms': self.progress,
            'context': {'uri': f'spotify:playlist:{self.context}'} if self.context else None,
        }

    def play_track(self, uri, device_id=None):
        tid = uri.split(':')[-1]
        with self._lock:
            self.calls.append(f'play_track:{tid}@{device_id}')
        self.playing, self.is_playing, self.context, self.progress = tid, True, None, 0
        return True, None

    def pause_playback(self, device_id=None):
        with self._lock:
            self.calls.append('PAUSE')
        self.is_playing = False
        return True, None

    def start_playlist_playback(self, pid, device_id=None, shuffle=False,
                                random_offset=False, track_count=None):
        with self._lock:
            self.calls.append(f'start_playlist:{pid}@{device_id}')
        self.playing, self.is_playing = FALLBACK_TRACKS[0], True
        self.context, self.progress = pid, 0
        return True, None

    # --- metadata ---
    def get_playlist_meta(self, pid):
        return {'id': pid, 'name': 'Test Fallback', 'track_count': len(FALLBACK_TRACKS)}

    def is_playing_our_playlist(self, pid):
        return self.context == pid

    def list_devices(self):
        return list(self.devices)

    def get_active_device_id(self, preferred_device_id=None):
        if preferred_device_id:
            for d in self.devices:
                if d['id'] == preferred_device_id:
                    return d['id']
        active = [d for d in self.devices if d['is_active']]
        if active:
            return active[0]['id']
        return self.devices[0]['id'] if self.devices else None


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Limiter counters and the playback snapshot are process-global; don't
    let one test's state leak into the next."""
    def reset():
        mod = sys.modules.get('app')
        if mod is not None:
            mod.limiter.reset()
        # The playback snapshot is process-global too.
        qm_mod = sys.modules.get('queue_manager')
        if qm_mod is not None:
            qm_mod._snapshot = None
    reset()
    yield
    reset()


@pytest.fixture
def db(monkeypatch):
    import database
    monkeypatch.setattr(database, 'DB_PATH', os.path.join(tempfile.mkdtemp(), 'test.db'))
    database.init_db()
    return database


@pytest.fixture
def qm(db, monkeypatch):
    """queue_manager wired to the fake Spotify, with module state reset."""
    import queue_manager
    fake = FakeSpotify()
    monkeypatch.setattr(queue_manager, 'sc', fake)
    monkeypatch.setattr(queue_manager, '_current_spotify_track_id', None, raising=False)
    monkeypatch.setattr(queue_manager, '_last_playing_queue_id', None, raising=False)
    monkeypatch.setattr(queue_manager, '_current_is_ours', False, raising=False)
    monkeypatch.setattr(queue_manager, '_standing_down', False, raising=False)
    monkeypatch.setattr(queue_manager, '_snapshot', None, raising=False)
    queue_manager.note_activity()
    queue_manager.fake = fake
    return queue_manager


@pytest.fixture
def guest(db):
    conn = db.get_connection()
    conn.execute("INSERT OR IGNORE INTO users (user_id, nickname) VALUES ('u1','Guest')")
    conn.commit()
    conn.close()
    return 'u1'
