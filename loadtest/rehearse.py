"""Full rehearsal against the LIVE stack: real Spotify, real lights, the tunnel.

    .venv/bin/python loadtest/rehearse.py --guests 30 --minutes 60
    .venv/bin/python loadtest/rehearse.py --base http://127.0.0.1:5001   # skip the tunnel

Unlike run.py this is the real thing, so start a new party first (it uses the
current party code and its guests stay in that party's history) and expect
music. Guests arrive over the first two minutes and behave like phones: poll
/api/status every 3 s, search Halloween songs now and then, queue a song every
quarter hour or so (the jukebox's per-guest limit applies), upvote, heart, and
one in four take a few disposable-camera shots. A TV polls /api/tv and a
second screen /api/wall.

Every 30 s a monitor samples the lights (fps, late frames, engine ticks, the
show playing), the jukebox's health and both services' CPU and memory, into
logs/rehearsal-<time>.jsonl. At the end it prints latency per endpoint, errors,
and the monitor's worst moments.
"""
import argparse
import io
import json
import os
import random
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)



class Recorder:
    """Latency and status codes per endpoint (run.py's, without importing it:
    run.py pulls in server.py, which points the database at a scratch copy)."""

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
        with self.lock:
            self.lat[name].append((time.perf_counter() - t) * 1000)
            self.codes[name][r.status_code] += 1
        return r


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]

SONGS = ['thriller', 'monster mash', 'ghostbusters', 'somebody\'s watching me',
         'time warp', 'superstition', 'bad moon rising', 'zombie cranberries',
         'disturbia', 'spooky scary skeletons', 'this is halloween', 'dead man\'s party',
         'werewolves of london', 'black magic woman', 'highway to hell', 'abracadabra',
         'monster kanye', 'bury a friend', 'toxic britney', 'psycho killer',
         'season of the witch', 'i put a spell on you', 'ghost town', 'the days notion']
NAMES = ['Morticia', 'Gomez', 'Wednesday', 'Lurch', 'Elvira', 'Vlad', 'Mina', 'Igor',
         'Frank', 'Bride', 'Jack', 'Sally', 'Beetle', 'Lydia', 'Carrie', 'Damien',
         'Regan', 'Freddy', 'Chucky', 'Annabelle', 'Norman', 'Hannibal', 'Pennywise',
         'Samara', 'Jason', 'Ghostface', 'Pinhead', 'Candyman', 'Babadook', 'Coraline']


def photo_bytes(rng):
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (1200, 1600), tuple(rng.randrange(256) for _ in range(3)))
    d = ImageDraw.Draw(img)
    for _ in range(12):
        x, y = rng.randrange(1200), rng.randrange(1600)
        d.ellipse((x, y, x + rng.randrange(60, 400), y + rng.randrange(60, 400)),
                  fill=tuple(rng.randrange(256) for _ in range(3)))
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=85)
    return buf.getvalue()


def guest(rec, base, code, stop, start_delay, rng, n):
    if stop.wait(start_delay):
        return
    s = requests.Session()
    s.headers['User-Agent'] = 'Mozilla/5.0 (iPhone; party rehearsal)'
    rec.call(s, 'GET', base, f'/?p={code}', name='/ (scan QR)')
    rec.call(s, 'POST', base, '/api/user/nickname',
             json={'nickname': f'{NAMES[n % len(NAMES)]} Rehearsal{n}'})
    photographer = rng.random() < 0.25
    shots_left = rng.randint(1, 4) if photographer else 0
    next_action = time.monotonic() + rng.uniform(10, 120)
    next_queue = time.monotonic() + rng.uniform(30, 600)
    next_shot = time.monotonic() + rng.uniform(60, 900)
    last = None
    while not stop.is_set():
        r = rec.call(s, 'GET', base, '/api/status')
        if r is not None and r.ok:
            last = r.json()
        now = time.monotonic()
        if now >= next_action:
            next_action = now + rng.uniform(60, 240)
            song = rng.choice(SONGS)
            tracks = []
            for q in ([song[:rng.randrange(3, len(song))]] if rng.random() < 0.5 else []) + [song]:
                r = rec.call(s, 'GET', base, f'/api/search?q={q}', name='/api/search')
                if r is not None and r.ok:
                    tracks = r.json().get('tracks', [])
                time.sleep(0.8)
            if tracks and now >= next_queue:
                t = tracks[0]
                r = rec.call(s, 'POST', base, '/api/queue', json={
                    'track_id': t['track_id'], 'track_name': t['track_name'],
                    'artist': t['artist'], 'album_art': t.get('album_art', ''),
                    'duration_ms': t['duration_ms']})
                next_queue = now + rng.uniform(600, 1200)
            if last and last.get('queue'):
                item = rng.choice(last['queue'])
                if not item.get('is_mine'):
                    rec.call(s, 'POST', base, '/api/upvote', json={'queue_item_id': item['id']})
            if rng.random() < 0.6:
                rec.call(s, 'POST', base, '/api/react', json={'reaction': 'heart'})
        if shots_left and now >= next_shot:
            next_shot = now + rng.uniform(300, 900)
            shots_left -= 1
            rec.call(s, 'POST', base, '/api/photos', name='/api/photos',
                     data={'shot_id': f'rehearsal-{n:03d}-{shots_left:04d}'},
                     files={'photo': ('shot.jpg', photo_bytes(rng), 'image/jpeg')})
        stop.wait(3)


def screen(rec, base, code, stop, page, path, every):
    s = requests.Session()
    rec.call(s, 'GET', base, f'/{page}?p={code}', name=f'/{page} (open)')
    while not stop.is_set():
        rec.call(s, 'GET', base, path)
        stop.wait(every)


def proc_stats(pattern):
    try:
        pid = subprocess.run(['pgrep', '-f', pattern], capture_output=True, text=True).stdout.split()[0]
        cpu, rss = subprocess.run(['ps', '-o', '%cpu=,rss=', '-p', pid], capture_output=True,
                                  text=True).stdout.split()
        return {'cpu': float(cpu), 'rss_mb': round(int(rss) / 1024, 1)}
    except Exception:
        return None


def monitor(stop, out_path, lights, local, samples):
    last_ticks = None
    with open(out_path, 'a') as out:
        while not stop.is_set():
            row = {'t': time.strftime('%H:%M:%S')}
            try:
                st = requests.get(f'{lights}/api/state', timeout=3).json()
                ticks, ts = st['engine']['ticks'], st['server_time']
                row.update({'fps': st['dmx']['fps'], 'late': st['dmx']['late'],
                            'ticks_s': (round((ticks - last_ticks[0]) / (ts - last_ticks[1]), 1)
                                        if last_ticks else None),
                            'show': (st['engine'].get('show') or {}).get('name'),
                            'look': st['engine'].get('look'),
                            'audio_peak': st['audio'].get('peak'),
                            'dropouts': st['audio'].get('dropouts')})
                last_ticks = (ticks, ts)
            except Exception as e:
                row['lights_error'] = str(e)
            try:
                t0 = time.perf_counter()
                h = requests.get(f'{local}/healthz', timeout=5).json()
                row['health_ms'] = round((time.perf_counter() - t0) * 1000)
                row['playback_issue'] = h.get('playback_issue')
                now = requests.get(f'{local}/api/now', timeout=5).json().get('current_track') or {}
                row['track'] = now.get('track_name')
            except Exception as e:
                row['jukebox_error'] = str(e)
            row['app'] = proc_stats('juke-box/app.py')
            row['lights_proc'] = proc_stats('run_lights.py')
            samples.append(row)
            out.write(json.dumps(row) + '\n')
            out.flush()
            stop.wait(30)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--base', default=None, help='default: the party URL (through the tunnel)')
    ap.add_argument('--guests', type=int, default=30)
    ap.add_argument('--minutes', type=float, default=60)
    ap.add_argument('--lights', default='http://127.0.0.1:5055')
    args = ap.parse_args()

    import database as db
    code = db.get_setting('party_code', '')
    base = (args.base or db.get_setting('party_url') or 'http://127.0.0.1:5001').rstrip('/')
    local = f"http://127.0.0.1:{os.environ.get('PORT', 5001)}"
    stamp = time.strftime('%Y%m%d-%H%M')
    out_path = os.path.join(ROOT, 'logs', f'rehearsal-{stamp}.jsonl')
    print(f'Rehearsal: {args.guests} guests for {args.minutes:g} min against {base} '
          f'(party {code}). Monitor: {out_path}', flush=True)

    rec, stop, samples = Recorder(), threading.Event(), []
    rng = random.Random(31)
    threads = [threading.Thread(target=guest, daemon=True, args=(
        rec, base, code, stop, rng.uniform(0, 120), random.Random(i), i)) for i in range(args.guests)]
    threads += [
        threading.Thread(target=screen, daemon=True, args=(rec, base, code, stop, 'tv', '/api/tv', 3)),
        threading.Thread(target=screen, daemon=True, args=(rec, base, code, stop, 'wall', '/api/wall', 5)),
        threading.Thread(target=monitor, daemon=True, args=(stop, out_path, args.lights, local, samples)),
    ]
    for t in threads:
        t.start()
    end = time.time() + args.minutes * 60
    try:
        while time.time() < end:
            time.sleep(min(60, max(1, end - time.time())))
            n = sum(len(v) for v in rec.lat.values())
            last = samples[-1] if samples else {}
            print(f"{time.strftime('%H:%M')}  {n} requests, {sum(rec.errors.values())} errors, "
                  f"fps {last.get('fps')}, ticks/s {last.get('ticks_s')}, show {last.get('show')}, "
                  f"track {last.get('track')}", flush=True)
    except KeyboardInterrupt:
        print('stopping early')
    stop.set()
    time.sleep(4)

    print('\nendpoint                     n     p50     p95     max   codes')
    for name in sorted(rec.lat):
        xs = rec.lat[name]
        codes = ' '.join(f'{c}:{k}' for c, k in sorted(rec.codes[name].items()))
        print(f'{name:26s} {len(xs):6d} {pct(xs, .5):7.0f} {pct(xs, .95):7.0f} {max(xs):7.0f}   {codes}')
    if rec.errors:
        print('\nerrors:', dict(rec.errors))
    if samples:
        fps = [s['fps'] for s in samples if 'fps' in s]
        ticks = [s['ticks_s'] for s in samples if s.get('ticks_s')]
        apps = [s['app'] for s in samples if s.get('app')]
        lights = [s['lights_proc'] for s in samples if s.get('lights_proc')]
        print(f"\nlights: fps min {min(fps)}, ticks/s min {min(ticks) if ticks else '-'}, "
              f"late frames {samples[0].get('late')} -> {samples[-1].get('late')}")
        if apps:
            print(f"jukebox: cpu max {max(a['cpu'] for a in apps)}%, "
                  f"rss {apps[0]['rss_mb']} -> {apps[-1]['rss_mb']} MB")
        if lights:
            print(f"lights: cpu max {max(a['cpu'] for a in lights)}%, "
                  f"rss {lights[0]['rss_mb']} -> {lights[-1]['rss_mb']} MB")
        print('shows played:', Counter(s.get('show') for s in samples if s.get('show')))
        print('issues:', Counter(s.get('playback_issue') for s in samples if s.get('playback_issue')))


if __name__ == '__main__':
    main()
