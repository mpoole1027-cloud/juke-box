#!/usr/bin/env python3
"""Pre-party check: run this once everything is started.

    .venv/bin/python deploy/preflight.py

Checks the jukebox, Spotify, the public link, the Mac's audio routing, the
light rig and the power situation, and says what to fix. Exits 1 if anything
would stop the party. Read-only: it never changes playback or settings.
"""
import glob
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, '.env'))

PORT = int(os.environ.get('PORT', 5001))
LOCAL = f'http://127.0.0.1:{PORT}'
LIGHTS_URL = os.environ.get('PARTY_LIGHTS_URL', 'http://127.0.0.1:5055')

failures = 0


def ok(msg):
    print(f'  ✓ {msg}')


def warn(msg):
    print(f'  ! {msg}')


def bad(msg):
    global failures
    failures += 1
    print(f'  ✗ {msg}')


def get_json(url, timeout=5):
    req = urllib.request.Request(url, headers={'User-Agent': 'jukebox-preflight'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read() or b'null')
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b'null')
        except ValueError:
            return e.code, None


def check_jukebox():
    print('Jukebox')
    try:
        status, health = get_json(f'{LOCAL}/healthz')
    except Exception as e:
        bad(f'Not answering on {LOCAL} ({e}). Is it running? '
            'launchctl print gui/$(id -u)/com.partyjukebox.app')
        return None
    if status == 200:
        ok(f'Running on {LOCAL}')
    elif health is None:
        hint = (' On macOS that is usually AirPlay Receiver; use PORT=5001.'
                if PORT == 5000 else ' An older jukebox without /healthz? Restart it.')
        bad(f'Something on {LOCAL} answered {status} but it isn\'t this jukebox.{hint}')
        return None
    else:
        bad(f'Unhealthy: {health}')
    if health and not health.get('spotify_connected'):
        bad('Spotify not connected. Log in at /host and click Connect Spotify.')
    elif health:
        ok('Spotify connected')
    if health and health.get('standing_down'):
        warn('Standing down after a long idle spell. Press Resume in the host panel.')
    if health and health.get('playback_issue'):
        bad(f"Playback problem: {health['playback_issue']} (details in the host panel)")
    return health


def check_spotify_devices():
    print('Spotify device')
    try:
        import database as db
        import spotify_client as sc
    except Exception as e:
        warn(f'Skipped ({e})')
        return
    devices = sc.list_devices()
    if not devices:
        bad('No Spotify Connect device online. Open Spotify on the party Mac.')
        return
    pinned = db.get_setting('preferred_device_id') or ''
    names = ', '.join(d['name'] for d in devices)
    if pinned and not any(d['id'] == pinned for d in devices):
        bad(f'The pinned device is offline. Online: {names}. Re-pick it in Settings.')
    elif pinned:
        name = next(d['name'] for d in devices if d['id'] == pinned)
        ok(f'Pinned device online: {name}')
    else:
        warn(f'No device pinned, so playback goes to whichever is active. Online: {names}')


def check_public_link():
    print('Public link')
    try:
        import database as db
        party_url = db.get_setting('party_url') or os.environ.get('PARTY_URL', '')
    except Exception:
        party_url = os.environ.get('PARTY_URL', '')
    if not party_url or 'localhost' in party_url or '127.0.0.1' in party_url:
        bad(f'Party URL is "{party_url}". Set it to your tunnel address in host Settings.')
        return
    if not party_url.startswith('https://'):
        warn(f'{party_url} is not https, so guests off your Wi-Fi can\'t use it.')
    try:
        status, health = get_json(party_url.rstrip('/') + '/healthz', timeout=10)
        if status == 200:
            ok(f'{party_url} reaches the jukebox')
        else:
            bad(f'{party_url}/healthz returned {status}')
    except Exception as e:
        bad(f'{party_url} is unreachable ({e}). Is the tunnel running? Check logs/tunnel.log')

    redirect = os.environ.get('SPOTIFY_REDIRECT_URI', '')
    host = party_url.split('//', 1)[-1].split('/', 1)[0]
    if os.environ.get('BEHIND_PROXY') == '1' or party_url.startswith('https://'):
        if host not in redirect:
            warn(f'SPOTIFY_REDIRECT_URI ({redirect}) is not on {host}. Connecting '
                 'Spotify works only from the address it names.')


def audio_devices():
    out = subprocess.run(['system_profiler', 'SPAudioDataType'],
                         capture_output=True, text=True, timeout=30).stdout
    devices, current = {}, None
    for line in out.splitlines():
        if line.startswith(' ' * 8) and not line.startswith(' ' * 9) and line.rstrip().endswith(':'):
            current = line.strip()[:-1]
            devices[current] = {}
        elif current and ':' in line:
            k, v = line.strip().split(':', 1)
            devices[current][k.strip()] = v.strip()
    return devices


def check_audio():
    print('Mac audio')
    try:
        devices = audio_devices()
    except Exception as e:
        warn(f'Skipped ({e})')
        return
    if not any(name.startswith('BlackHole') for name in devices):
        bad('BlackHole is not installed, so party-lights can\'t hear the music.')
    else:
        ok('BlackHole installed')
    default_out = next((n for n, d in devices.items()
                        if d.get('Default Output Device') == 'Yes'), None)
    if default_out and 'Multi-Output' in default_out:
        ok(f'Output is "{default_out}" (speakers + BlackHole)')
    else:
        bad(f'Output is "{default_out}". Switch it to the Multi-Output Device, or '
            'the lights hear nothing.')


def check_lights():
    print('Lights')
    try:
        status, state = get_json(f'{LIGHTS_URL}/api/state', timeout=3)
        if status == 200:
            ok(f'party-lights running on {LIGHTS_URL}')
            jb = (state or {}).get('jukebox') or {}
            if jb.get('connected'):
                ok(f"party-lights is reading song changes from {jb.get('url')}")
            elif jb:
                bad(f"party-lights can't reach the jukebox at {jb.get('url')} "
                    f"({jb.get('last_error') or 'no answer'}). Set jukebox.url in "
                    f"party-lights/config/settings.yaml to http://localhost:{PORT}")
        else:
            warn(f'party-lights answered {status}')
    except Exception:
        warn(f'party-lights is not running on {LIGHTS_URL} (fine if you\'re not using lights)')
    ports = glob.glob('/dev/cu.usbserial-*')
    if ports:
        ok(f'DMX interface plugged in ({", ".join(ports)})')
    else:
        warn('No DMX interface found (/dev/cu.usbserial-*)')


def check_power():
    print('Power')
    try:
        out = subprocess.run(['pmset', '-g', 'batt'], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception as e:
        warn(f'Skipped ({e})')
        return
    if "'AC Power'" in out:
        ok('On AC power, so the Mac stays awake')
    else:
        warn('On battery. Plug in: the Mac may sleep on battery and stop the party.')


def main():
    print('Party preflight\n')
    check_jukebox()
    check_spotify_devices()
    check_public_link()
    check_audio()
    check_lights()
    check_power()
    print()
    if failures:
        print(f'{failures} problem(s) to fix before guests arrive.')
        sys.exit(1)
    print('All set.')


if __name__ == '__main__':
    main()
