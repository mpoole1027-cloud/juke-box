#!/usr/bin/env python3
"""Party watchdog: watches the jukebox, the tunnel and party-lights, and posts
to a Discord channel when something goes wrong (and again when it recovers).

    .venv/bin/python deploy/watchdog.py            # run forever (launchd does this)
    .venv/bin/python deploy/watchdog.py --once     # one check, print what it sees
    .venv/bin/python deploy/watchdog.py --test     # post a test message

Settings come from .env, re-read every poll so edits apply without a restart:

    PARTY_ALERTS_WEBHOOK        Discord webhook URL. Unset: alerts only go to the log.
    PARTY_ALERTS_MENTION        Prefix for critical alerts (default @everyone; "" for none).
    PARTY_ALERTS_HEARTBEAT_URL  Optional healthchecks.io ping URL, hit every poll, so
                                someone hears about it if this Mac goes silent.

Mute alerts while working on the code with `touch logs/watchdog.mute`; delete
the file to unmute. Read-only: it never restarts or changes anything.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import dotenv_values  # noqa: E402

ENV_FILE = os.path.join(ROOT, '.env')
LOG_DIR = os.path.join(ROOT, 'logs')
MUTE_FILE = os.path.join(LOG_DIR, 'watchdog.mute')

POLL_SECONDS = 15
PUBLIC_EVERY = 60          # the tunnel check goes out over the internet; less often
REMIND_SECONDS = 15 * 60   # repeat a still-open critical alert this often
EVENT_COOLDOWN = 10 * 60   # at most one alert per event kind this often

AGENTS = {
    'com.partyjukebox.app': 'Jukebox',
    'com.partyjukebox.tunnel': 'Tunnel',
    'com.partyjukebox.lights': 'Party lights',
}
LOGS = ('jukebox.log', 'jukebox.stderr.log', 'party-lights.log')

# Playback issues the jukebox retries on its own get longer to clear first.
SELF_HEALING_ISSUES = ('spotify_rate_limited', 'spotify_unreachable')

CRITICAL, WARNING = 'critical', 'warning'


@dataclass
class Problem:
    key: str        # stable identity, e.g. 'service:com.partyjukebox.app'
    severity: str
    message: str
    grace: int = 30  # seconds it must persist before anyone is told


# ── Gathering ────────────────────────────────────────────────────────────────

def http_json(url, timeout=5):
    """(status, body) or raises on connection failure."""
    req = urllib.request.Request(url, headers={'User-Agent': 'jukebox-watchdog'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read() or b'null')
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b'null')
        except ValueError:
            return e.code, None


def why(e):
    """A connection error as a few plain words, not a repr."""
    reason = getattr(e, 'reason', None) or e
    return getattr(reason, 'strerror', None) or str(reason)


def agent_info(label):
    """{'installed', 'running', 'runs', 'last_exit'} for a launchd agent."""
    plist = os.path.expanduser(f'~/Library/LaunchAgents/{label}.plist')
    out = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'],
                         capture_output=True, text=True, timeout=10).stdout
    info = {'installed': os.path.exists(plist), 'loaded': bool(out),
            'running': False, 'runs': None, 'last_exit': None}
    for line in out.splitlines():
        s = line.strip()
        if s == 'state = running' and line.startswith('\tstate'):
            info['running'] = True
        elif s.startswith('runs = '):
            info['runs'] = int(s.split('=', 1)[1])
        elif s.startswith('last exit code = '):
            info['last_exit'] = s.split('=', 1)[1].strip()
    return info


def gather(env, want_public):
    """One snapshot of everything the checks look at. Never raises."""
    port = int(env.get('PORT') or 5001)
    lights_url = env.get('PARTY_LIGHTS_URL') or 'http://127.0.0.1:5055'
    snap = {'agents': {}, 'health': None, 'health_error': None,
            'lights': None, 'lights_error': None, 'public': None,
            'on_battery': False, 'disk_free_gb': None, 'logs': {}}

    for label in AGENTS:
        try:
            snap['agents'][label] = agent_info(label)
        except Exception:
            pass

    try:
        snap['health'] = http_json(f'http://127.0.0.1:{port}/healthz')
    except Exception as e:
        snap['health_error'] = why(e)

    try:
        status, state = http_json(f'{lights_url}/api/state', timeout=3)
        snap['lights'] = state if status == 200 else None
        if status != 200:
            snap['lights_error'] = f'answered {status}'
    except Exception as e:
        snap['lights_error'] = why(e)

    if want_public:
        url = env.get('PARTY_URL', '')
        try:
            import database as db
            url = db.get_setting('party_url') or url
        except Exception:
            pass
        if url.startswith('https://'):
            try:
                status, _ = http_json(url.rstrip('/') + '/healthz', timeout=10)
                snap['public'] = (url, status == 200, f'answered {status}')
            except Exception as e:
                snap['public'] = (url, False, why(e))

    try:
        out = subprocess.run(['pmset', '-g', 'batt'], capture_output=True,
                             text=True, timeout=10).stdout
        snap['on_battery'] = "'Battery Power'" in out
    except Exception:
        pass
    try:
        snap['disk_free_gb'] = shutil.disk_usage(ROOT).free / 1e9
    except Exception:
        pass
    return snap


# ── Checks ───────────────────────────────────────────────────────────────────

def check(snap):
    """Ongoing problems in this snapshot. Pure: the same snapshot, the same list."""
    problems = []
    agents = snap['agents']
    lights_installed = agents.get('com.partyjukebox.lights', {}).get('installed')

    for label, info in agents.items():
        if info['installed'] and not info['running']:
            why = (f"exit code {info['last_exit']}" if info['loaded']
                   else 'not loaded; re-run deploy/install.sh')
            problems.append(Problem(
                f'service:{label}', CRITICAL,
                f"{AGENTS[label]} service is down ({why}). Logs: juke-box/logs/"))

    health = snap['health']
    if health is None:
        problems.append(Problem('jukebox:down', CRITICAL,
                                f"Jukebox isn't answering ({snap['health_error']})."))
    else:
        status, body = health
        body = body or {}
        if status != 200:
            parts = [k for k in ('database', 'queue_worker') if body.get(k) is False]
            problems.append(Problem('jukebox:unhealthy', CRITICAL,
                                    f"Jukebox is unhealthy ({', '.join(parts) or status})."))
        if body and not body.get('spotify_connected', True):
            problems.append(Problem('jukebox:spotify', CRITICAL,
                                    'Spotify is disconnected. Reconnect it in the host panel.'))
        issue = body.get('playback_issue')
        if issue and issue != 'spotify_not_connected':
            heals = issue in SELF_HEALING_ISSUES
            problems.append(Problem(
                'jukebox:playback', WARNING if heals else CRITICAL,
                f'Music is stuck: {issue.replace("_", " ")}. See the host panel.',
                grace=120 if heals else 45))
        if body.get('standing_down'):
            problems.append(Problem('jukebox:standing_down', WARNING,
                                    'Jukebox stood down after sitting idle. Press Resume '
                                    'in the host panel.', grace=0))

    if snap['public'] is not None:
        url, up, err = snap['public']
        if not up:
            problems.append(Problem('public', CRITICAL,
                                    f"Guests can't reach {url} ({err}). Check the tunnel.",
                                    grace=60))

    lights = snap['lights']
    if lights is None:
        if lights_installed:
            problems.append(Problem('lights:down', CRITICAL,
                                    f"Party lights aren't answering ({snap['lights_error']})."))
    else:
        dmx = lights.get('dmx') or {}
        target = dmx.get('target_fps') or 40
        if dmx.get('frozen'):
            problems.append(Problem('lights:frozen', CRITICAL, 'Light output is frozen.'))
        elif dmx.get('fps') is not None and dmx['fps'] < 0.85 * target:
            problems.append(Problem('lights:fps', WARNING,
                                    f"Lights are stuttering: {dmx['fps']:.0f} of "
                                    f"{target:.0f} fps.", grace=60))
        if 'null' in (dmx.get('driver') or ''):
            problems.append(Problem('lights:no_dmx', WARNING,
                                    'Lights are running without the DMX interface, so '
                                    'nothing reaches the fixtures. Plug it in, then '
                                    'restart the lights service.', grace=60))
        jb = lights.get('jukebox') or {}
        if jb and not jb.get('connected', True):
            problems.append(Problem('lights:jukebox_link', WARNING,
                                    "Lights can't reach the jukebox, so they won't follow "
                                    "song changes.", grace=60))
        track = jb.get('track') or {}
        music = lights.get('music') or {}
        if track.get('is_playing') and music.get('silent'):
            problems.append(Problem('lights:deaf', CRITICAL,
                                    "A song is playing but the lights hear silence. Set the "
                                    "Mac's output to the Multi-Output Device.", grace=45))

    if snap['on_battery']:
        problems.append(Problem('power', WARNING,
                                'The Mac is on battery. Plug it in before it sleeps.',
                                grace=60))
    if snap['disk_free_gb'] is not None and snap['disk_free_gb'] < 2:
        problems.append(Problem('disk', WARNING,
                                f"Only {snap['disk_free_gb']:.1f} GB free on the Mac.",
                                grace=0))
    return problems


def counters(snap):
    """Monotonic counters whose increase is itself worth an alert."""
    c = {}
    for label, info in snap['agents'].items():
        if info.get('runs') is not None:
            c[f'restarts:{label}'] = info['runs']
    lights = snap['lights'] or {}
    engine = lights.get('engine') or {}
    if engine.get('errors') is not None:
        c['lights_errors'] = engine['errors']
    audio = lights.get('audio') or {}
    if audio.get('dropouts') is not None:
        c['audio_dropouts'] = audio['dropouts']
    return c


def counter_events(prev, cur, snap):
    """Events from counters that went up since the last poll."""
    events = []
    for key, value in cur.items():
        before = prev.get(key)
        if before is None or value <= before:
            continue
        n = value - before
        if key.startswith('restarts:'):
            label = key.split(':', 1)[1]
            events.append((key, f'{AGENTS[label]} service restarted'
                                f'{"" if n == 1 else f" {n} times"}.'))
        elif key == 'lights_errors':
            last = ((snap['lights'] or {}).get('engine') or {}).get('last_error') or ''
            events.append((key, f'Lights engine hit {n} error(s)'
                                f'{": " + last[:300] if last else "."}'))
        elif key == 'audio_dropouts' and n >= 5:
            events.append((key, f'Lights dropped {n} audio blocks; the beat sync may wobble.'))
    return events


class LogTail:
    """New Traceback lines in a log since the last read. Starts at the end of
    the file, so old crashes don't alert on startup."""

    def __init__(self, path):
        self.path = path
        self.pos = self._size()

    def _size(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def new_tracebacks(self):
        size = self._size()
        if size < self.pos:  # rotated
            self.pos = 0
        if size == self.pos:
            return []
        try:
            with open(self.path, errors='replace') as f:
                f.seek(self.pos)
                text = f.read(2_000_000)
                self.pos = f.tell()
        except OSError:
            return []
        found = []
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith('Traceback'):
                # The exception itself is the first unindented line after it.
                tail = next((ln for ln in lines[i + 1:] if ln and not ln[0].isspace()), '')
                found.append(tail.strip()[:300])
        return found


# ── Deciding what to say ─────────────────────────────────────────────────────

class Tracker:
    """Turns a stream of problem lists into alerts: new problems once they have
    lasted their grace period, reminders for open critical ones, and a
    recovery note once a problem that was announced goes away."""

    def __init__(self):
        self.first_seen = {}   # key -> time it appeared
        self.announced = {}    # key -> (Problem, time last announced)
        self.event_last = {}   # event key -> time last announced

    def update(self, problems, now):
        current = {p.key: p for p in problems}
        new, reminders, resolved = [], [], []

        for key in list(self.first_seen):
            if key not in current:
                del self.first_seen[key]
        for key, p in current.items():
            self.first_seen.setdefault(key, now)

        for key, (p, _) in list(self.announced.items()):
            if key not in current:
                resolved.append(p)
                del self.announced[key]

        for key, p in current.items():
            if key in self.announced:
                _, last = self.announced[key]
                if p.severity == CRITICAL and now - last >= REMIND_SECONDS:
                    reminders.append(p)
                    self.announced[key] = (p, now)
                else:
                    self.announced[key] = (p, last)
            elif now - self.first_seen[key] >= p.grace:
                new.append(p)
                self.announced[key] = (p, now)
        return new, reminders, resolved

    def events(self, events, now):
        """Rate-limit one-off events per kind."""
        out = []
        for key, message in events:
            if now - self.event_last.get(key, -EVENT_COOLDOWN) >= EVENT_COOLDOWN:
                self.event_last[key] = now
                out.append(message)
        return out


def compose(new, reminders, resolved, events, mention):
    """One Discord message for everything that changed this poll, or None."""
    lines = []
    if any(p.severity == CRITICAL for p in new + reminders) and mention:
        lines.append(mention)
    for p in new:
        lines.append(f"{'🚨' if p.severity == CRITICAL else '⚠️'} {p.message}")
    for p in reminders:
        lines.append(f'🔁 Still broken: {p.message}')
    for message in events:
        lines.append(f'⚠️ {message}')
    for p in resolved:
        lines.append(f'✅ Back to normal: {p.message}')
    content = [ln for ln in lines if ln != mention]
    return '\n'.join(lines)[:1900] if content else None


# ── Sending ──────────────────────────────────────────────────────────────────

def post_discord(webhook, content):
    """True once Discord accepts the message."""
    body = json.dumps({'content': content, 'username': 'Party Watchdog',
                       'allowed_mentions': {'parse': ['everyone', 'roles']}}).encode()
    req = urllib.request.Request(webhook, data=body, method='POST', headers={
        'Content-Type': 'application/json', 'User-Agent': 'jukebox-watchdog (party, 1.0)'})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read()).get('retry_after', 2))
                except Exception:
                    wait = 2
                time.sleep(min(wait, 30))
                continue
            log(f'Discord rejected the alert: HTTP {e.code}')
            return False
        except Exception as e:
            log(f"Couldn't reach Discord: {e}")
            return False
    return False


def ping(url):
    try:
        urllib.request.urlopen(urllib.request.Request(
            url, headers={'User-Agent': 'jukebox-watchdog'}), timeout=10).close()
    except Exception as e:
        log(f'Heartbeat ping failed: {e}')


def log(msg):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), msg, flush=True)


def load_env():
    """.env, with the process environment taking precedence."""
    try:
        env = {k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None}
    except Exception:
        env = {}
    env.update(os.environ)
    return env


# ── Main loop ────────────────────────────────────────────────────────────────

def run(once=False):
    tracker = Tracker()
    tails = {name: LogTail(os.path.join(LOG_DIR, name)) for name in LOGS}
    prev_counters = None
    unsent = []      # messages Discord didn't take yet, retried next poll
    last_public = 0
    started = False

    while True:
        now = time.time()
        env = load_env()
        want_public = once or now - last_public >= PUBLIC_EVERY
        if want_public:
            last_public = now
        snap = gather(env, want_public)

        problems = check(snap)
        cur_counters = counters(snap)
        events = counter_events(prev_counters or cur_counters, cur_counters, snap)
        prev_counters = cur_counters
        for name, tail in tails.items():
            for exc in tail.new_tracebacks():
                events.append((f'traceback:{name}', f'Crash in logs/{name}: {exc}'))

        if once:
            for p in problems:
                print(f'{p.severity:8} {p.key:28} {p.message}')
            print('No problems.' if not problems else f'{len(problems)} problem(s).')
            return

        new, reminders, resolved = tracker.update(problems, now)
        message = compose(new, reminders, resolved, tracker.events(events, now),
                          env.get('PARTY_ALERTS_MENTION', '@everyone'))
        if not started:
            started = True
            names = ', '.join(AGENTS[label] for label, info in snap['agents'].items()
                              if info['installed'])
            hello = f'👀 Watchdog started, watching: {names or "nothing installed"}.'
            message = hello + ('\n' + message if message else '')

        webhook = env.get('PARTY_ALERTS_WEBHOOK', '').strip()
        if message:
            log('ALERT ' + message.replace('\n', ' | '))
            if webhook and not os.path.exists(MUTE_FILE):
                unsent.append(message)
        while unsent and webhook and post_discord(webhook, unsent[0]):
            unsent.pop(0)
        del unsent[:-20]  # don't hoard if Discord is down for hours

        if env.get('PARTY_ALERTS_HEARTBEAT_URL'):
            ping(env['PARTY_ALERTS_HEARTBEAT_URL'])

        time.sleep(max(1, POLL_SECONDS - (time.time() - now)))


def main():
    if '--test' in sys.argv:
        webhook = load_env().get('PARTY_ALERTS_WEBHOOK', '').strip()
        if not webhook:
            sys.exit('PARTY_ALERTS_WEBHOOK is not set in .env')
        ok = post_discord(webhook, '🧪 Test from the party watchdog. If you can read '
                                   'this, alerts will reach this channel.')
        sys.exit(0 if ok else 1)
    run(once='--once' in sys.argv)


if __name__ == '__main__':
    main()
