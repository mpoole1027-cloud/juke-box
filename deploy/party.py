#!/usr/bin/env python3
"""Party night in two commands.

    .venv/bin/python deploy/party.py on               # before guests arrive
    .venv/bin/python deploy/party.py on --new-party   # ...and start a fresh party code
    .venv/bin/python deploy/party.py off              # when it's over

on:  unmutes the watchdog and switches its alerts to live, turns per-song
     light shows on and drafts cards for newly mapped songs, then runs the
     preflight check and prints tonight's links. --new-party first starts a
     new party (new code, clean queue, photos develop at the next midnight),
     which asks for the host password.

off: saves tonight's setlist to Spotify, exports the approved photos to
     exports/, compresses the big logs into logs/archive/, puts alerts back
     in test mode, and prints the recap link.

Both are safe to run twice.
"""
import argparse
import getpass
import gzip
import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

ENV_FILE = os.path.join(ROOT, '.env')
load_dotenv(ENV_FILE)

LOG_DIR = os.path.join(ROOT, 'logs')
MUTE_FILE = os.path.join(LOG_DIR, 'watchdog.mute')
LIGHTS_DIR = os.path.join(os.path.dirname(ROOT), 'party-lights')
PORT = int(os.environ.get('PORT', 5001))
LOCAL = f'http://127.0.0.1:{PORT}'
LIGHTS_URL = os.environ.get('PARTY_LIGHTS_URL', 'http://127.0.0.1:5055')
#: Logs bigger than this are compressed into logs/archive/ by `off`.
ARCHIVE_OVER_BYTES = 20_000_000


def step(msg):
    print(f'\n▸ {msg}')


def say(msg):
    print(f'  {msg}')


def set_env(key, value, path=ENV_FILE):
    """Set KEY=value in .env, replacing the line if it's there."""
    lines = []
    if os.path.exists(path):
        with open(path) as f:
            lines = f.read().splitlines()
    out, done = [], False
    for line in lines:
        if line.split('=', 1)[0].strip() == key:
            if not done:
                out.append(f'{key}={value}')
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f'{key}={value}')
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write('\n'.join(out) + '\n')
    os.chmod(tmp, os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o600)
    os.replace(tmp, path)


def post(url, body=b'{}', opener=None, timeout=10):
    req = urllib.request.Request(url, data=body, method='POST',
                                 headers={'Content-Type': 'application/json'})
    return (opener.open if opener else urllib.request.urlopen)(req, timeout=timeout)


def new_party():
    import http.cookiejar
    import json
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    password = getpass.getpass('  Host password: ')
    try:
        post(f'{LOCAL}/api/host/login', json.dumps({'password': password}).encode(), opener)
    except urllib.error.HTTPError:
        sys.exit('  That password was refused; nothing changed.')
    res = json.loads(post(f'{LOCAL}/api/host/new_party', opener=opener).read())
    say(f"New party code {res.get('party_code')}. Queue cleared, photos develop at the next midnight.")


def links():
    import database as db
    base = (db.get_setting('party_url') or os.environ.get('PARTY_URL', LOCAL)).rstrip('/')
    code = db.get_setting('party_code', '')
    return {'Guests': f'{base}/?p={code}', 'TV': f'{base}/tv?p={code}',
            'Photo wall': f'{base}/wall?p={code}', 'Recap': f'{base}/recap?p={code}',
            'Host': f'{base}/host', 'Lights': f'{LIGHTS_URL}/live'}


def cmd_on(args):
    if args.new_party:
        step('Starting a new party')
        new_party()

    step('Alerts')
    if os.path.exists(MUTE_FILE):
        os.remove(MUTE_FILE)
        say('Watchdog unmuted.')
    set_env('PARTY_ALERTS_MODE', 'live')
    say('PARTY_ALERTS_MODE=live: critical alerts mention everyone and repeat until fixed.')

    step('Per-song light shows')
    py = os.path.join(LIGHTS_DIR, '.venv', 'bin', 'python')
    if os.path.exists(py):
        out = subprocess.run([py, 'tools/design_cards.py'], cwd=LIGHTS_DIR,
                             capture_output=True, text=True)
        say((out.stdout or out.stderr).strip().splitlines()[-1] if (out.stdout or out.stderr) else 'no output')
    try:
        post(f'{LIGHTS_URL}/api/cue/shows-on', timeout=3)
        say('Shows follow the jukebox.')
    except Exception as e:
        say(f"Couldn't reach the lights to turn shows on ({e}).")

    step('Preflight')
    code = subprocess.call([sys.executable, os.path.join(ROOT, 'deploy', 'preflight.py')])
    print('\n  If the Mac might sleep: sudo pmset -a disablesleep 1  (undo: ... 0)')

    step("Tonight's links")
    for name, url in links().items():
        say(f'{name:11s} {url}')
    return code


#: Logs launchd writes for a service: moved aside, then the service is
#: restarted so it opens a fresh file (launchd holds the old one open).
SERVICE_LOGS = {'party-lights.log': 'com.partyjukebox.lights',
                'tunnel.log': 'com.partyjukebox.tunnel',
                'watchdog.log': 'com.partyjukebox.watchdog'}


def archive_logs():
    os.makedirs(os.path.join(LOG_DIR, 'archive'), exist_ok=True)
    stamp = time.strftime('%Y-%m-%d')
    for name in sorted(os.listdir(LOG_DIR)):
        path = os.path.join(LOG_DIR, name)
        if not (os.path.isfile(path) and name in SERVICE_LOGS
                and os.path.getsize(path) > ARCHIVE_OVER_BYTES):
            continue
        moved = os.path.join(LOG_DIR, 'archive', f'{name[:-4]}-{stamp}.log')
        os.replace(path, moved)
        subprocess.run(['launchctl', 'kickstart', '-k',
                        f'gui/{os.getuid()}/{SERVICE_LOGS[name]}'], capture_output=True)
        with open(moved, 'rb') as src, gzip.open(moved + '.gz', 'wb') as out:
            shutil.copyfileobj(src, out)
        os.remove(moved)
        say(f'logs/{name} -> logs/archive/{os.path.basename(moved)}.gz (service restarted)')
    say('jukebox.log rotates on its own.')


def cmd_off(args):
    import database as db
    import setlist

    step('Setlist')
    try:
        res = setlist.save_tonight()
        say(f"{'Updated' if res['updated'] else 'Saved'} {res['songs']} songs: {res['url']}")
    except setlist.NothingPlayed as e:
        say(str(e))
    except Exception as e:
        say(f"Spotify didn't take it ({e}). Try again from the host panel.")

    step('Photos')
    party = db.get_setting('party_code', '')
    subprocess.call([sys.executable, os.path.join(ROOT, 'deploy', 'export_photos.py'),
                     '--party', party, '--title', args.title])

    step('Logs')
    archive_logs()

    step('Alerts')
    set_env('PARTY_ALERTS_MODE', 'test')
    say('Back to test mode: new critical problems only, no mentions.')

    step('Recap')
    say(links()['Recap'])
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    on = sub.add_parser('on', help='get ready for guests')
    on.add_argument('--new-party', action='store_true', help='start a new party code first')
    off = sub.add_parser('off', help='wrap up after the party')
    off.add_argument('--title', default='Party Photos', help='heading on the photo gallery')
    args = ap.parse_args()
    sys.exit(cmd_on(args) if args.cmd == 'on' else cmd_off(args))


if __name__ == '__main__':
    main()
