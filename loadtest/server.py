"""A throwaway copy of the jukebox for load testing. Never touches the real
database or Spotify account.

Runs the real Flask app under waitress exactly as app.py does (one process,
16 threads), but with a scratch SQLite file and a fake Spotify that sleeps for
roughly what the real API takes and counts every call. loadtest/run.py starts
this for you; run it by hand only to poke at it:

    .venv/bin/python loadtest/server.py --port 5098
"""
import argparse
import os
import secrets
import sys
import tempfile
import threading
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Set before app imports: load_dotenv() doesn't override what's already here,
# so .env's real settings can't leak into the test copy.
os.environ['JUKEBOX_DB'] = os.path.join(tempfile.mkdtemp(prefix='jukebox-load-'), 'load.db')
os.environ['BEHIND_PROXY'] = '0'
os.environ['JUKEBOX_DEV'] = '1'
os.environ['FLASK_SECRET_KEY'] = secrets.token_hex(32)

PARTY_CODE = 'LOADTEST'

# Typical Spotify Web API latencies from a home connection, in seconds.
LATENCY = {
    'search': 0.25,
    'playback_state': 0.12,
    'play': 0.20,
    'devices': 0.10,
    'playlist': 0.15,
}


class FakeSpotify:
    """Stands in for spotify_client: same functions, fake data, real-ish delays."""

    PLAYBACK_OK = 'ok'
    PLAYBACK_IDLE = 'idle'
    PLAYBACK_ERROR = 'error'

    def __init__(self):
        self.calls = Counter()
        self.call_times = []        # (monotonic time, name) of every API call
        self._lock = threading.Lock()
        self.playing = None
        self.started = 0.0
        self.duration = 200_000

    def _api(self, name, kind):
        with self._lock:
            self.calls[name] += 1
            self.call_times.append((time.monotonic(), name))
        time.sleep(LATENCY[kind])

    # --- auth: a local token-file read in the real client, no API call ---
    def is_authenticated(self):
        return True

    # --- search ---
    def search_tracks(self, query, limit=10):
        # The real function, so its cache sits in front of the fake API call.
        import spotify_client
        return spotify_client.search_tracks(query, limit)

    def rate_limited_for(self):
        return 0

    def _search_spotify(self, query, limit=10):
        self._api('search', 'search')
        base = abs(hash(query.lower())) % 10**12
        return [{
            'track_id': f'{base + i:022d}',
            'track_name': f'{query.title()} {i}',
            'artist': 'Load Test',
            'album': 'Load Test',
            'album_art': '',
            'duration_ms': 200_000,
            'uri': f'spotify:track:{base + i:022d}',
        } for i in range(limit)]

    # --- playback ---
    def get_current_playback(self):
        self._api('current_playback', 'playback_state')
        return self._playback()

    def get_playback_state(self):
        self._api('playback_state', 'playback_state')
        pb = self._playback()
        if not pb:
            return {'status': self.PLAYBACK_IDLE, 'playback': None}
        return {'status': self.PLAYBACK_OK, 'playback': pb}

    def _playback(self):
        if self.playing is None:
            return None
        return {
            'item': {'id': self.playing, 'name': self.playing, 'duration_ms': self.duration,
                     'artists': [{'name': 'Load Test'}], 'album': {'images': []}},
            'is_playing': True,
            'progress_ms': int((time.monotonic() - self.started) * 1000) % self.duration,
            'context': None,
        }

    def play_track(self, uri, device_id=None):
        self._api('play_track', 'play')
        self.playing, self.started = uri.split(':')[-1], time.monotonic()
        return True, None

    def pause_playback(self, device_id=None):
        self._api('pause', 'play')
        return True, None

    def start_playlist_playback(self, pid, device_id=None, shuffle=False,
                                random_offset=False, track_count=None):
        self._api('start_playlist', 'play')
        return True, None

    def clear_jukebox_playlist(self, *a, **kw):
        return True

    # --- metadata / devices ---
    def get_playlist_meta(self, pid):
        self._api('playlist_meta', 'playlist')
        return {'id': pid, 'name': 'Fallback', 'track_count': 50}

    def is_playing_our_playlist(self, pid):
        return False

    def parse_playlist_id(self, raw):
        return raw

    def list_devices(self):
        self._api('devices', 'devices')
        return [{'id': 'mac', 'name': 'Party Mac', 'type': 'Computer', 'is_active': True}]

    def get_active_device_id(self, preferred_device_id=None):
        self._api('devices', 'devices')
        return 'mac'

    def stats(self):
        """Call counts, plus the busiest rolling 30 s window (Spotify's
        rate limit is counted over a rolling 30 s window)."""
        with self._lock:
            times = [t for t, _ in self.call_times]
            counts = dict(self.calls)
        peak, j = 0, 0
        for i, t in enumerate(times):
            while times[j] < t - 30:
                j += 1
            peak = max(peak, i - j + 1)
        return {'calls': counts, 'total': len(times), 'peak_30s': peak}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=5098)
    ap.add_argument('--threads', type=int, default=16)
    ap.add_argument('--no-search-cache', action='store_true')
    args = ap.parse_args()

    import logging
    import app as jukebox
    import database as db
    import queue_manager as qm
    from flask import jsonify

    import spotify_client
    fake = FakeSpotify()
    spotify_client._search_spotify = fake._search_spotify
    if args.no_search_cache:
        spotify_client.SEARCH_CACHE_SECONDS = 0
    jukebox.sc = fake
    qm.sc = fake
    logging.getLogger().setLevel(logging.WARNING)

    jukebox.create_app()
    db.set_setting('party_code', PARTY_CODE)

    @jukebox.app.route('/loadtest/stats')
    def loadtest_stats():
        return jsonify(fake.stats())

    if os.environ.get('LOADTEST_PROFILE'):
        # Profile every request (cProfile only sees its own thread, so one
        # profiler per request) and dump the merged stats on shutdown.
        import atexit
        import cProfile
        import pstats
        import signal
        merged, lock, inner = [None], threading.Lock(), jukebox.app.wsgi_app

        def profiled(environ, start_response):
            pr = cProfile.Profile()
            try:
                return pr.runcall(inner, environ, start_response)
            finally:
                with lock:
                    if merged[0] is None:
                        merged[0] = pstats.Stats(pr)
                    else:
                        merged[0].add(pr)
        jukebox.app.wsgi_app = profiled

        def dump():
            if merged[0] is not None:
                merged[0].dump_stats(os.environ['LOADTEST_PROFILE'])
        atexit.register(dump)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    from waitress import serve
    print(f'load-test jukebox on http://127.0.0.1:{args.port} '
          f'(db {os.environ["JUKEBOX_DB"]})', flush=True)
    serve(jukebox.app, host='127.0.0.1', port=args.port, threads=args.threads,
          ident='jukebox', _quiet=True)


if __name__ == '__main__':
    main()
