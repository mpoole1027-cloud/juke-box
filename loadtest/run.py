"""Simulate a party's worth of guests against a throwaway jukebox.

    .venv/bin/python loadtest/run.py                      # 25, 50, 100 guests
    .venv/bin/python loadtest/run.py --guests 50 --seconds 90

Starts loadtest/server.py (scratch DB, fake Spotify with real-ish latency) and
drives it the way the pages do:

  guests      arrive over the first 20 s (everyone scanning the QR at once),
              poll /api/status every 3 s, and every so often search (a few
              debounced keystrokes), queue a song, upvote and react
  TV          polls /api/tv every 3 s
  lights      polls /api/now every 2 s

Then prints latency per endpoint and how many Spotify calls the load caused.
It never talks to the real jukebox or Spotify.
"""
import argparse
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from server import PARTY_CODE  # noqa: E402

WORDS = ['love', 'night', 'dance', 'fire', 'gold', 'summer', 'baby', 'heart',
         'party', 'dream', 'rain', 'city', 'wild', 'star', 'run', 'blue']


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.lat = defaultdict(list)
        self.codes = defaultdict(Counter)
        self.errors = Counter()

    def call(self, s, method, base, path, name=None, **kw):
        name = name or path.split('?')[0]
        t = time.perf_counter()
        try:
            r = s.request(method, base + path, timeout=30, **kw)
        except requests.RequestException as e:
            with self.lock:
                self.errors[f'{name}: {type(e).__name__}'] += 1
            return None
        ms = (time.perf_counter() - t) * 1000
        with self.lock:
            self.lat[name].append(ms)
            self.codes[name][r.status_code] += 1
        return r


# Guests don't connect to the Mac: they reach Cloudflare, and cloudflared
# relays their requests over its own pool of keep-alive connections (up to 100
# idle by default). One shared adapter models that; each guest still has its
# own Session, so its own cookie.
ADAPTER = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=100)


def session():
    s = requests.Session()
    s.mount('http://', ADAPTER)
    return s


def guest(rec, base, stop, start_delay, rng):
    if stop.wait(start_delay):
        return
    s = session()
    rec.call(s, 'GET', base, f'/?p={PARTY_CODE}', name='/ (scan QR)')
    queued, last_status = 0, None
    next_action = time.monotonic() + rng.uniform(5, 40)
    while not stop.is_set():
        r = rec.call(s, 'GET', base, '/api/status')
        if r is not None and r.ok:
            last_status = r.json()
        if time.monotonic() >= next_action:
            next_action = time.monotonic() + rng.uniform(30, 90)
            # Typing a search: the page debounces at 500 ms, so a word comes
            # through as two or three requests.
            word = rng.choice(WORDS)
            tracks = []
            for n in sorted(rng.sample(range(3, len(word) + 3), 2)):
                r = rec.call(s, 'GET', base, f'/api/search?q={word[:n]}')
                if r is not None and r.ok:
                    tracks = r.json().get('tracks', [])
                time.sleep(0.6)
            if tracks and queued < 2:
                t = rng.choice(tracks)
                r = rec.call(s, 'POST', base, '/api/queue', json={
                    'track_id': t['track_id'], 'track_name': t['track_name'],
                    'artist': t['artist'], 'album_art': '', 'duration_ms': t['duration_ms']})
                if r is not None and r.ok:
                    queued += 1
            if last_status and last_status.get('queue'):
                item = rng.choice(last_status['queue'])
                if not item.get('is_mine'):
                    rec.call(s, 'POST', base, '/api/upvote', json={'queue_item_id': item['id']})
            if rng.random() < 0.5:
                rec.call(s, 'POST', base, '/api/react',
                         json={'reaction': rng.choice(['fire', 'heart'])})
        stop.wait(3)


def poller(rec, base, stop, path, every, name):
    s = session()
    if path == '/api/tv':
        rec.call(s, 'GET', base, f'/tv?p={PARTY_CODE}', name='/tv (scan)')
    while not stop.is_set():
        rec.call(s, 'GET', base, path, name=name)
        stop.wait(every)


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def run_once(port, guests, seconds, threads):
    base = f'http://127.0.0.1:{port}'
    # A file, not a pipe: nothing reads a pipe during the run, and once it
    # fills, every server thread that logs blocks and the server freezes.
    log_path = os.path.join(HERE, f'server-{guests}.log')
    log = open(log_path, 'w')
    server = subprocess.Popen(
        [sys.executable, os.path.join(HERE, 'server.py'), '--port', str(port),
         '--threads', str(threads)],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                requests.get(base + '/healthz', timeout=1)
                break
            except requests.RequestException:
                if server.poll() is not None:
                    sys.exit(f'server exited; see {log_path}')
                time.sleep(0.2)
        rec, stop = Recorder(), threading.Event()
        rng = random.Random(guests)
        workers = [threading.Thread(target=guest, daemon=True,
                                    args=(rec, base, stop, rng.uniform(0, 20),
                                          random.Random(rng.random())))
                   for _ in range(guests)]
        workers.append(threading.Thread(target=poller, daemon=True,
                                        args=(rec, base, stop, '/api/tv', 3, '/api/tv')))
        workers.append(threading.Thread(target=poller, daemon=True,
                                        args=(rec, base, stop, '/api/now', 2, '/api/now (lights)')))
        t0 = time.monotonic()
        for w in workers:
            w.start()
        time.sleep(seconds)
        stop.set()
        for w in workers:
            w.join(timeout=35)
        elapsed = time.monotonic() - t0
        try:
            spotify = requests.get(base + '/loadtest/stats', timeout=10).json()
        except requests.RequestException as e:
            spotify = {'calls': {}, 'total': 0, 'peak_30s': 0}
            print(f'  server not answering after the run: {e}')
    finally:
        server.terminate()
        server.wait(timeout=10)
        log.close()

    total = sum(len(v) for v in rec.lat.values())
    print(f'\n=== {guests} guests, {seconds}s, {threads} server threads '
          f'— {total} requests ({total / elapsed:.1f}/s)')
    print(f'{"endpoint":22} {"n":>6} {"p50 ms":>8} {"p95 ms":>8} {"max ms":>8}  status codes')
    for name in sorted(rec.lat, key=lambda n: -len(rec.lat[n])):
        xs = rec.lat[name]
        codes = ' '.join(f'{c}×{n}' for c, n in sorted(rec.codes[name].items()))
        print(f'{name:22} {len(xs):6} {statistics.median(xs):8.0f} {pct(xs, .95):8.0f} '
              f'{max(xs):8.0f}  {codes}')
    for e, n in rec.errors.items():
        print(f'  ERROR {e} ×{n}')
    calls = ', '.join(f'{k} {v}' for k, v in sorted(spotify['calls'].items(), key=lambda kv: -kv[1]))
    print(f'Spotify API: {spotify["total"]} calls, busiest 30 s window {spotify["peak_30s"]} '
          f'({spotify["peak_30s"] / 30:.1f}/s) — {calls}')
    return rec, spotify


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--guests', type=int, nargs='+', default=[25, 50, 100])
    ap.add_argument('--seconds', type=int, default=90)
    ap.add_argument('--threads', type=int, default=16, help='waitress threads (app.py uses 16)')
    ap.add_argument('--port', type=int, default=5098)
    args = ap.parse_args()
    for n in args.guests:
        run_once(args.port, n, args.seconds, args.threads)


if __name__ == '__main__':
    main()
