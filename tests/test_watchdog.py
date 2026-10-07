"""The party watchdog's decisions: what counts as a problem, when it is
announced, and what the Discord message says. No network or launchd."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'deploy'))

import watchdog as wd  # noqa: E402

RUNNING = {'installed': True, 'loaded': True, 'running': True, 'runs': 1, 'last_exit': '0'}


def healthy_snap(**over):
    snap = {
        'agents': {label: dict(RUNNING) for label in wd.AGENTS},
        'health': (200, {'ok': True, 'spotify_connected': True, 'standing_down': False,
                         'playback_issue': None}),
        'health_error': None,
        'lights': {'dmx': {'fps': 40.0, 'target_fps': 40.0, 'frozen': False,
                           'driver': 'enttec'},
                   'engine': {'errors': 0, 'last_error': ''},
                   'audio': {'dropouts': 0},
                   'jukebox': {'connected': True, 'track': {'is_playing': True}},
                   'music': {'silent': False}},
        'lights_error': None,
        'public': ('https://party.test', True, ''),
        'on_battery': False,
        'disk_free_gb': 100.0,
    }
    snap.update(over)
    return snap


def keys(snap):
    return {p.key for p in wd.check(snap)}


def test_healthy_stack_has_no_problems():
    assert wd.check(healthy_snap()) == []


def test_service_down_is_critical():
    snap = healthy_snap()
    snap['agents']['com.partyjukebox.tunnel'].update(running=False, last_exit='1')
    [p] = wd.check(snap)
    assert p.key == 'service:com.partyjukebox.tunnel'
    assert p.severity == wd.CRITICAL
    assert 'exit code 1' in p.message


def test_uninstalled_lights_are_not_a_problem():
    snap = healthy_snap(lights=None, lights_error='refused')
    snap['agents']['com.partyjukebox.lights'] = {**RUNNING, 'installed': False,
                                                 'running': False, 'loaded': False}
    assert keys(snap) == set()


def test_jukebox_failures():
    assert keys(healthy_snap(health=None, health_error='refused')) == {'jukebox:down'}
    snap = healthy_snap(health=(503, {'ok': False, 'database': True, 'queue_worker': False,
                                      'spotify_connected': False, 'playback_issue': None}))
    assert keys(snap) == {'jukebox:unhealthy', 'jukebox:spotify'}


def test_self_healing_playback_issue_is_a_slow_warning():
    snap = healthy_snap()
    snap['health'][1]['playback_issue'] = 'spotify_rate_limited'
    [p] = wd.check(snap)
    assert (p.severity, p.grace) == (wd.WARNING, 120)
    snap['health'][1]['playback_issue'] = 'no_device'
    [p] = wd.check(snap)
    assert p.severity == wd.CRITICAL


def test_lights_problems():
    snap = healthy_snap()
    snap['lights']['dmx'].update(fps=21.0, driver='null (simulated output, no hardware)')
    snap['lights']['music']['silent'] = True
    assert keys(snap) == {'lights:fps', 'lights:no_dmx', 'lights:deaf'}


def test_silence_between_songs_is_fine():
    snap = healthy_snap()
    snap['lights']['music']['silent'] = True
    snap['lights']['jukebox']['track']['is_playing'] = False
    assert keys(snap) == set()


def test_grace_then_announce_once_then_recover():
    t = wd.Tracker()
    down = [wd.Problem('jukebox:down', wd.CRITICAL, 'down', grace=30)]
    assert t.update(down, 0) == ([], [], [])        # just appeared
    assert t.update(down, 15) == ([], [], [])
    new, _, _ = t.update(down, 30)
    assert [p.key for p in new] == ['jukebox:down']
    assert t.update(down, 45) == ([], [], [])        # not repeated
    _, _, resolved = t.update([], 60)
    assert [p.key for p in resolved] == ['jukebox:down']
    assert t.update([], 75) == ([], [], [])


def test_blip_shorter_than_grace_is_never_mentioned():
    t = wd.Tracker()
    down = [wd.Problem('service:x', wd.CRITICAL, 'down', grace=30)]
    t.update(down, 0)
    t.update(down, 15)
    assert t.update([], 30) == ([], [], [])          # no "fixed" for a silent blip
    assert t.update(down, 45) == ([], [], [])        # grace restarts


def test_critical_reminder_but_not_warning():
    t = wd.Tracker()
    crit = wd.Problem('a', wd.CRITICAL, 'a', grace=0)
    warn = wd.Problem('b', wd.WARNING, 'b', grace=0)
    t.update([crit, warn], 0)
    _, reminders, _ = t.update([crit, warn], wd.REMIND_SECONDS)
    assert [p.key for p in reminders] == ['a']


def test_events_are_rate_limited_per_kind():
    t = wd.Tracker()
    assert t.events([('lights_errors', 'x')], 0) == ['x']
    assert t.events([('lights_errors', 'y')], 60) == []
    assert t.events([('traceback:jukebox.log', 'z')], 60) == ['z']
    assert t.events([('lights_errors', 'w')], wd.EVENT_COOLDOWN) == ['w']


def test_counter_events():
    snap = healthy_snap()
    snap['lights']['engine'].update(errors=3, last_error='KeyError: palette')
    events = wd.counter_events({'lights_errors': 1, 'restarts:com.partyjukebox.app': 1,
                                'audio_dropouts': 0},
                               {'lights_errors': 3, 'restarts:com.partyjukebox.app': 2,
                                'audio_dropouts': 2}, snap)
    assert dict(events) == {
        'lights_errors': 'Lights engine hit 2 error(s): KeyError: palette',
        'restarts:com.partyjukebox.app': 'Jukebox service restarted.',
    }


def test_compose_mentions_only_for_critical():
    crit = wd.Problem('a', wd.CRITICAL, 'Jukebox down.')
    warn = wd.Problem('b', wd.WARNING, 'On battery.')
    assert wd.compose([warn], [], [], [], '@everyone') == '⚠️ On battery.'
    assert wd.compose([crit], [], [], [], '@everyone') == '@everyone\n🚨 Jukebox down.'
    assert wd.compose([], [], [crit], [], '@everyone') == '✅ Back to normal: Jukebox down.'
    assert wd.compose([], [], [], [], '@everyone') is None


def test_log_tail_reports_new_tracebacks_only(tmp_path):
    log = tmp_path / 'app.log'
    log.write_text('Traceback (most recent call last):\n  File "x"\nOldError: before\n')
    tail = wd.LogTail(str(log))
    assert tail.new_tracebacks() == []
    with open(log, 'a') as f:
        f.write('INFO ok\nTraceback (most recent call last):\n  File "y", line 1\n'
                '    boom()\nValueError: bad thing\n')
    assert tail.new_tracebacks() == ['ValueError: bad thing']
    assert tail.new_tracebacks() == []
    log.write_text('')  # rotated
    assert tail.new_tracebacks() == []


def test_post_discord_sends_json_with_mentions_allowed():
    got = []

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(('127.0.0.1', 0), Hook)
    threading.Thread(target=server.handle_request, daemon=True).start()
    assert wd.post_discord(f'http://127.0.0.1:{server.server_port}/hook', 'hi')
    server.server_close()
    assert got[0]['content'] == 'hi'
    assert 'everyone' in got[0]['allowed_mentions']['parse']
