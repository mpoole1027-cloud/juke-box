import os
import hashlib
import hmac
import secrets
import uuid
import json
import random
import io
import logging
import time
from datetime import timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for, send_file
)
from dotenv import load_dotenv
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

load_dotenv()

import database as db
import photos
import spotify_client as sc
import queue_manager as qm

def _configure_logging():
    """Log to stderr, or with LOG_FILE set (as under launchd) to a rotating file
    so a long-running party machine never fills its disk. stderr then only
    catches crashes before logging starts."""
    handlers = []
    log_file = os.environ.get('LOG_FILE')
    if log_file:
        from logging.handlers import RotatingFileHandler
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handlers.append(RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5))
    else:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')


_configure_logging()
logger = logging.getLogger(__name__)

# JUKEBOX_DEV=1 relaxes the startup checks below for local hacking. Never set
# it on a machine guests can reach.
DEV_MODE = os.environ.get('JUKEBOX_DEV') == '1'
_DEV_SECRET = 'dev-secret-change-me'
_PLACEHOLDER_SECRETS = (_DEV_SECRET, 'choose_a_long_random_string')

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or _DEV_SECRET
# Long enough that a guest who closes the tab keeps their identity all night.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=3)
# Request bodies are small JSON except camera uploads; a phone JPEG is a few MB.
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024

# BEHIND_PROXY=1 when guests arrive through a tunnel or reverse proxy (e.g.
# cloudflared). It trusts one hop of X-Forwarded-* so client IPs (rate limits)
# and https URLs come out right, marks the session cookie Secure, and binds the
# server to localhost so the proxy is the only way in.
BEHIND_PROXY = os.environ.get('BEHIND_PROXY') == '1'
if BEHIND_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=BEHIND_PROXY,
)


@app.template_global()
def asset(filename):
    """URL of a static file, stamped with its mtime. Cloudflare tells browsers
    to cache static files for hours whatever we send, so without the stamp a
    phone keeps old JS after a deploy and new buttons silently do nothing."""
    try:
        version = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
    except OSError:
        version = 0
    return url_for('static', filename=filename, v=version)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
# In-memory counters are fine: the app runs as exactly one process (see the
# bottom of this file). Guests are counted per browser; logins and joins per IP,
# since those are what a password- or code-guesser would hammer.
def _rate_key():
    return session.get('guest_id') or get_remote_address()


limiter = Limiter(key_func=_rate_key, app=app, storage_uri='memory://',
                  default_limits=[], headers_enabled=True)


@app.errorhandler(429)
def _rate_limited(e):
    return jsonify({'error': 'Slow down a little and try again in a minute.',
                    'code': 'rate_limited'}), 429


@app.errorhandler(413)
def _too_large(e):
    return jsonify({'error': 'That upload is too large.', 'code': 'too_large'}), 413


# ---------------------------------------------------------------------------
# Nickname generation
# ---------------------------------------------------------------------------
ADJECTIVES = [
    'Funky', 'Disco', 'Groovy', 'Retro', 'Cosmic',
    'Electric', 'Neon', 'Tubular', 'Radical', 'Jazzy',
    'Stellar', 'Turbo', 'Hyper', 'Laser', 'Phantom',
]
ANIMALS = [
    'Wombat', 'Llama', 'Hedgehog', 'Ferret', 'Capybara',
    'Gecko', 'Pangolin', 'Axolotl', 'Narwhal', 'Platypus',
    'Quokka', 'Tapir', 'Marmot', 'Binturong', 'Fossa',
]


def generate_nickname():
    return f"{random.choice(ADJECTIVES)} {random.choice(ANIMALS)}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_or_create_user(user_id):
    """Fetch or create a user record. Returns user dict or None if user_id is empty."""
    if not user_id or len(user_id) > 64:
        return None
    user = db.get_user(user_id)
    if not user:
        nickname = generate_nickname()
        user = db.create_user(user_id, nickname)
    return user


def require_host(f):
    """Decorator: requires a host session (set by /api/host/login)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('is_host'):
            return f(*args, **kwargs)
        return jsonify({'error': 'Unauthorized'}), 401
    return decorated


def get_user_id_from_request(create=True):
    """The guest's ID, kept in the signed session cookie.

    The server picks it, so a guest can't claim someone else's ID or mint a
    fresh one to dodge a ban or stack votes the way a client-chosen header
    allowed. Read-only callers pass create=False so pollers that don't keep
    cookies (party-lights) don't mint a new guest on every request.
    """
    uid = session.get('guest_id')
    if not uid and create:
        uid = uuid.uuid4().hex
        session['guest_id'] = uid
        session.permanent = True
    return uid or ''


def has_party_access():
    """True for the host, and for guests who arrived with tonight's party code."""
    if session.get('is_host'):
        return True
    code = db.get_setting('party_code', '')
    return bool(code) and hmac.compare_digest(session.get('party_code', ''), code)


def require_party(f):
    """Decorator for guest actions: a leaked URL alone isn't enough to join."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if has_party_access():
            return f(*args, **kwargs)
        return jsonify({'error': 'Scan the party QR code to join.',
                        'code': 'party_code_required'}), 403
    return decorated


def _accept_party_code(raw):
    """Store the code in the session if it's tonight's. Returns True on success."""
    code = (raw or '').strip().upper()
    current = db.get_setting('party_code', '')
    if code and current and hmac.compare_digest(code, current):
        session['party_code'] = current
        session.permanent = True
        return True
    return False


def party_links():
    """Invite and TV links for the host panel. The code rides in the query string."""
    party_url = db.get_setting('party_url', os.environ.get('PARTY_URL', 'http://localhost:5001'))
    base = party_url.rstrip('/')
    code = db.get_setting('party_code', '')
    return {
        'party_url': party_url,
        'party_code': code,
        'invite_url': f'{base}/?p={code}',
        'tv_url': f'{base}/tv?p={code}',
    }


def _resolve_current_track():
    """Resolve the now-playing track from demo mode or Spotify.

    Returns (current_track, playing_queue_item, current_queue_id).
    current_track is the base track dict (or None). When a matching 'playing'
    queue row is found, current_track is enriched with 'requested_by_nickname'
    and 'dedication', and playing_queue_item / current_queue_id are populated.
    """
    demo_mode = db.get_setting('demo_mode', '0') == '1'
    demo_track_raw = db.get_setting('demo_current_track', '')
    current_track = None

    if demo_mode and demo_track_raw:
        try:
            current_track = json.loads(demo_track_raw)
        except Exception:
            current_track = None
    else:
        playback = qm.get_cached_playback()
        if playback and playback.get('item'):
            item = playback['item']
            current_track = {
                'track_id': item['id'],
                'track_name': item['name'],
                'artist': ', '.join(a['name'] for a in item['artists']),
                'album_art': item['album']['images'][0]['url'] if item['album']['images'] else '',
                'duration_ms': item['duration_ms'],
                'progress_ms': playback.get('progress_ms', 0),
                'is_playing': playback.get('is_playing', False),
            }

    playing_queue_item = None
    current_queue_id = None
    if current_track:
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT q.*, u.nickname FROM queue q JOIN users u ON q.requested_by = u.user_id "
                "WHERE q.spotify_track_id = ? AND q.status = 'playing' LIMIT 1",
                (current_track['track_id'],)
            ).fetchone()
            if row:
                playing_queue_item = dict(row)
                current_queue_id = playing_queue_item['id']
        finally:
            conn.close()
        if playing_queue_item:
            current_track['requested_by_nickname'] = playing_queue_item.get('nickname')
            current_track['dedication'] = playing_queue_item.get('dedication')

    return current_track, playing_queue_item, current_queue_id


def _camera_state(user_id):
    """What the guest's disposable camera shows: on/off and film left tonight."""
    total = int(db.get_setting('camera_shots_per_guest', '24'))
    used = db.count_user_photos(user_id, db.get_setting('party_code', '')) if user_id else 0
    return {
        'enabled': db.get_setting('camera_enabled', '1') == '1',
        'shots_total': total,
        'shots_left': max(0, total - used),
    }


COSTUME_MAX_LEN = 40


def _costume_phase():
    phase = db.get_setting('costume_phase', 'off')
    return phase if phase in db.COSTUME_PHASES else 'off'


def _ranked(board):
    """The board with a 'rank' on each entry; tied vote counts share a rank."""
    ranked, rank, prev = [], 0, None
    for i, e in enumerate(board):
        if e['votes'] != prev:
            rank, prev = i + 1, e['votes']
        ranked.append({**e, 'rank': rank})
    return ranked


def _costume_photo_url(entry):
    """Where the entry's photo is served. The ?v= changes with the photo, so a
    replaced photo is never shown from a stale cache."""
    if not entry.get('photo'):
        return None
    version = hashlib.sha1(entry['photo'].encode()).hexdigest()[:10]
    return url_for('api_costume_photo', entry_id=entry['id'], v=version)


def _costume_results(board):
    """Final standings for guests and the TV. Only ever sent once voting closes."""
    return [{**{k: e[k] for k in ('id', 'nickname', 'costume', 'votes', 'rank')},
             'photo_url': _costume_photo_url(e)}
            for e in _ranked(board)]


def _costume_state(user_id):
    """The contest as a guest sees it. Vote counts stay secret until it closes."""
    phase = _costume_phase()
    if phase == 'off':
        return {'phase': 'off'}
    party_code = db.get_setting('party_code', '')
    board = db.costume_board(party_code)
    mine = next((e for e in board if user_id and e['user_id'] == user_id), None)
    state = {
        'phase': phase,
        'my_entry': {'id': mine['id'], 'costume': mine['costume'],
                     'photo_url': _costume_photo_url(mine)} if mine else None,
        'my_vote': db.get_costume_vote(party_code, user_id) if user_id else None,
    }
    if phase == 'open':
        # Alphabetical, so where an entry sits says nothing about its votes.
        state['entries'] = sorted(
            ({'id': e['id'], 'nickname': e['nickname'], 'costume': e['costume'],
              'photo_url': _costume_photo_url(e), 'is_mine': e is mine} for e in board),
            key=lambda e: (e['costume'].lower(), e['id']))
    else:
        state['results'] = _costume_results(board)
    return state


def _costume_tv():
    """What the TV shows: entry and vote counts while open, the podium once closed."""
    phase = _costume_phase()
    if phase == 'off':
        return {'phase': 'off'}
    board = db.costume_board(db.get_setting('party_code', ''))
    tv = {'phase': phase, 'entries': len(board), 'votes': sum(e['votes'] for e in board)}
    if phase == 'closed':
        tv['results'] = _costume_results(board)[:5]
        # Seconds since voting closed, measured here so the TV's clock doesn't matter.
        closed_at = float(db.get_setting('costume_closed_at', '0') or 0)
        tv['closed_secs_ago'] = max(0, int(time.time() - closed_at)) if closed_at else None
    return tv


def _guest_issue():
    issue = qm.get_playback_issue()
    return {'code': issue['code'], 'message': issue['guest_message']} if issue else None


UI_THEMES = ('modern', 'classic')


def _ui_theme():
    theme = db.get_setting('ui_theme', 'modern')
    return theme if theme in UI_THEMES else 'modern'


@app.before_request
def _note_party_activity():
    if request.method == 'POST' and request.path.startswith('/api/'):
        qm.note_activity()


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    if 'p' in request.args:
        ok = _accept_party_code(request.args['p'])
        # Drop the code from the address bar so screenshots don't share it.
        return redirect(url_for('index', **({} if ok else {'bad_code': 1})))
    get_or_create_user(get_user_id_from_request())
    return render_template('index.html', ui_theme=_ui_theme(),
                           bad_code='bad_code' in request.args)


@app.route('/host')
def host():
    return render_template('host.html', ui_theme=_ui_theme())


@app.route('/tv')
def tv():
    if 'p' in request.args:
        _accept_party_code(request.args['p'])
        return redirect(url_for('tv'))
    return render_template('tv.html', ui_theme=_ui_theme())


@app.route('/api/join', methods=['POST'])
@limiter.limit('10 per minute', key_func=get_remote_address)
def api_join():
    """Manual entry of the party code, for guests who can't scan the QR."""
    data = request.get_json() or {}
    if _accept_party_code(data.get('code')):
        get_or_create_user(get_user_id_from_request())
        return jsonify({'success': True})
    return jsonify({'error': "That code doesn't match tonight's party."}), 403


@app.route('/qr')
@require_host
def qr_code():
    # Host-only: the QR carries the party code.
    import qrcode
    img = qrcode.make(party_links()['invite_url'])
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


# ---------------------------------------------------------------------------
# Spotify auth routes
# ---------------------------------------------------------------------------
# Both routes are host-only: whoever completes this flow becomes the account the
# whole party plays through, so a guest must never be able to start or finish it.
@app.route('/auth/spotify')
def auth_spotify():
    if not session.get('is_host'):
        return redirect(url_for('host'))
    state = secrets.token_urlsafe(24)
    session['spotify_oauth_state'] = state
    return redirect(sc.get_auth_url(state=state))


@app.route('/auth/callback')
def auth_callback():
    if not session.get('is_host'):
        return "Log in to the host panel before connecting Spotify.", 401
    expected = session.pop('spotify_oauth_state', None)
    returned = request.args.get('state', '')
    if not expected or not hmac.compare_digest(expected, returned):
        return ("This Spotify sign-in didn't start from this host panel, or it expired. "
                "Go back to /host and click Connect Spotify again."), 400
    code = request.args.get('code')
    error = request.args.get('error')
    if error:
        return f"Spotify auth error: {error}", 400
    if not code:
        return "No code received", 400
    token_info = sc.handle_callback(code)
    if token_info:
        return redirect(url_for('host'))
    return "Failed to get token from Spotify", 500


# ---------------------------------------------------------------------------
# Host login / logout
# ---------------------------------------------------------------------------
@app.route('/api/host/login', methods=['POST'])
@limiter.limit('5 per minute;30 per hour', key_func=get_remote_address)
def host_login():
    data = request.get_json() or {}
    password = data.get('password', '')
    if db.check_host_password(password):
        session['is_host'] = True
        return jsonify({'success': True})
    return jsonify({'error': 'Invalid password'}), 401


@app.route('/api/host/logout', methods=['POST'])
def host_logout():
    session.pop('is_host', None)
    return jsonify({'success': True})


@app.route('/api/host/auth_check')
def host_auth_check():
    return jsonify({'authenticated': bool(session.get('is_host'))})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
@app.route('/api/status')
def api_status():
    if not has_party_access():
        # Outsiders (and cookieless local pollers like party-lights) see only
        # what's playing: no queue, nicknames, dedications or settings.
        return jsonify({
            'party_code_required': True,
            'current_track': api_now().get_json()['current_track'],
            'ui_theme': _ui_theme(),
        })

    user_id = get_user_id_from_request(create=False)
    user = get_or_create_user(user_id) if user_id else None

    demo_mode = db.get_setting('demo_mode', '0') == '1'

    # Resolve the current now-playing track (demo mode or Spotify) plus the
    # matching 'playing' queue row, if any.
    current_track, playing_queue_item, current_queue_id = _resolve_current_track()

    downvote_count = 0
    user_has_downvoted = False
    if current_queue_id:
        downvote_count = db.get_downvote_count(current_queue_id)
        if user_id:
            user_has_downvoted = db.user_has_downvoted(current_queue_id, user_id)

    pending_queue = db.get_pending_queue()
    my_upvotes = db.get_user_upvoted_ids(user_id) if user_id else set()
    # Rough wait for each queued song: what's left of the current track plus
    # everything ahead of it.
    etas, wait_ms = [], 0
    if current_track and current_track.get('duration_ms'):
        wait_ms = max(0, current_track['duration_ms'] - (current_track.get('progress_ms') or 0))
    for q in pending_queue:
        etas.append(wait_ms)
        wait_ms += q['duration_ms'] or 0
    # Reaction counts for the CURRENT song only (fall back to global if none).
    reactions = db.get_reaction_counts(current_queue_id) if current_queue_id else db.get_reaction_counts()
    banned_users = db.get_banned_users()

    settings = {
        'downvote_threshold': int(db.get_setting('downvote_threshold', '7')),
        'max_queue_per_user': int(db.get_setting('max_queue_per_user', '2')),
        'skip_ban_threshold': int(db.get_setting('skip_ban_threshold', '2')),
    }

    return jsonify({
        'current_track': current_track,
        'current_queue_id': current_queue_id,
        'downvote_count': downvote_count,
        'user_has_downvoted': user_has_downvoted,
        'demo_mode': demo_mode,
        'queue': [
            {
                'id': q['id'],
                'track_id': q['spotify_track_id'],
                'track_name': q['track_name'],
                'artist': q['artist'],
                'album_art': q['album_art'],
                'duration_ms': q['duration_ms'],
                'status': q['status'],
                'is_mine': bool(user_id) and q['requested_by'] == user_id,
                'nickname': q['nickname'],
                'dedication': q.get('dedication'),
                'upvote_count': q['upvote_count'],
                'user_has_upvoted': q['id'] in my_upvotes,
                'eta_ms': eta,
            }
            for q, eta in zip(pending_queue, etas)
        ],
        'reactions': reactions,
        'banned_users': [{'nickname': u['nickname']} for u in banned_users],
        'user': {
            'nickname': user['nickname'],
            'is_banned': bool(user['is_banned']),
        } if user else None,
        'settings': settings,
        'spotify_connected': sc.is_authenticated(),
        'playback_issue': _guest_issue(),
        'ui_theme': _ui_theme(),
        'camera': _camera_state(user_id),
        'costume': _costume_state(user_id),
    })


@app.route('/api/now')
def api_now():
    """What's playing, for local integrations like party-lights. No party code
    needed and nothing beyond the track; same shape as /api/status's
    current_track. Served from the playback snapshot, so polling it is cheap."""
    track, _, _ = _resolve_current_track()
    return jsonify({'current_track': (
        {k: track.get(k) for k in ('track_id', 'track_name', 'artist', 'is_playing')}
        if track else None)})


@app.route('/healthz')
def healthz():
    """Liveness for the preflight script and uptime checks. 503 when the app
    can't do its job (database unreadable or the queue worker died)."""
    try:
        db.get_setting('party_code')
        db_ok = True
    except Exception:
        db_ok = False
    worker_ok = qm.worker_alive()
    issue = qm.get_playback_issue()
    ok = db_ok and worker_ok
    return jsonify({
        'ok': ok,
        'database': db_ok,
        'queue_worker': worker_ok,
        'spotify_connected': sc.is_authenticated(),
        'standing_down': qm.is_standing_down(),
        'playback_issue': issue['code'] if issue else None,
    }), 200 if ok else 503


@app.route('/api/search')
@limiter.limit('30 per minute')
@require_party
def api_search():

    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'tracks': []})

    if not sc.is_authenticated():
        return jsonify({'error': 'Spotify not connected', 'tracks': []}), 503

    tracks = sc.search_tracks(q, limit=10)
    if not tracks and sc.rate_limited_for():
        return jsonify({'error': 'Spotify is busy. Try searching again in a minute.',
                        'tracks': []}), 503
    return jsonify({'tracks': tracks})


@app.route('/api/queue', methods=['POST'])
@limiter.limit('10 per minute')
@require_party
def api_queue():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({'error': 'Missing user ID'}), 400

    user = get_or_create_user(user_id)
    if not user:
        return jsonify({'error': 'Invalid user ID'}), 400

    if user['is_banned']:
        return jsonify({'error': 'You are banned from queuing songs.'}), 403

    if not sc.is_authenticated():
        return jsonify({'error': 'Spotify not connected'}), 503

    data = request.get_json() or {}
    track_id = data.get('track_id', '').strip()
    track_name = data.get('track_name', '').strip()
    artist = data.get('artist', '').strip()
    album_art = data.get('album_art', '')
    duration_ms = data.get('duration_ms', 0)
    ded = (data.get('dedication') or '').strip()[:80] or None

    if not track_id or not track_name or not artist:
        return jsonify({'error': 'Missing track info'}), 400

    # Check if already queued tonight
    if db.track_played_tonight(track_id):
        return jsonify({'error': 'This song has already been played or queued tonight!'}), 409

    # Check per-user queue limit
    max_per_user = int(db.get_setting('max_queue_per_user', '2'))
    user_count = db.count_user_pending(user_id)
    if user_count >= max_per_user:
        return jsonify({'error': f'You already have {max_per_user} songs in the queue!'}), 429

    queue_id = db.add_to_queue(track_id, track_name, artist, album_art, duration_ms, user_id, dedication=ded)

    if sc.is_authenticated():
        # The worker's snapshot, not a fresh Spotify call per queued song.
        playback = qm.get_cached_playback()
        if not playback or not playback.get('is_playing'):
            device_id = qm.get_party_device_id()
            ok, err = sc.play_track(f'spotify:track:{track_id}', device_id=device_id)
            qm.note_play_result(ok, err, device_id)
            if ok:
                qm.invalidate_playback_cache()
                db.update_queue_status(queue_id, 'playing')

    return jsonify({'success': True, 'queue_id': queue_id})


@app.route('/api/downvote', methods=['POST'])
@limiter.limit('20 per minute')
@require_party
def api_downvote():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({'error': 'Missing user ID'}), 400

    user = get_or_create_user(user_id)
    if not user:
        return jsonify({'error': 'Invalid user ID'}), 400

    data = request.get_json() or {}
    queue_item_id = data.get('queue_item_id')
    if not queue_item_id:
        return jsonify({'error': 'Missing queue_item_id'}), 400

    item = db.get_queue_item(queue_item_id)
    if not item:
        return jsonify({'error': 'Queue item not found'}), 404

    added = db.add_downvote(queue_item_id, user_id)
    if not added:
        return jsonify({'error': 'You already downvoted this song'}), 409

    count = db.get_downvote_count(queue_item_id)
    threshold = int(db.get_setting('downvote_threshold', '7'))

    # Trigger skip if threshold reached
    skip_ban_threshold = int(db.get_setting('skip_ban_threshold', '2'))
    if count >= threshold:
        db.update_queue_status(queue_item_id, 'skipped')
        db.mark_track_played(item['spotify_track_id'])
        requester = item['requested_by']
        skip_count = db.increment_skip_count(requester)
        if skip_count >= skip_ban_threshold:
            db.ban_user(requester)

        # Single source of truth: advances to the next song, or falls back to
        # the host's playlist when the queue is empty.
        qm.advance_to_next_pending()

    return jsonify({'success': True, 'downvote_count': count, 'threshold': threshold})


@app.route('/api/react', methods=['POST'])
@limiter.limit('40 per minute')
@require_party
def api_react():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({'error': 'Missing user ID'}), 400

    get_or_create_user(user_id)

    data = request.get_json() or {}
    reaction = data.get('reaction', '')
    if reaction not in ('fire', 'heart'):
        return jsonify({'error': 'Invalid reaction. Use fire or heart.'}), 400

    playing_item = db.get_playing_item()
    current_queue_id = playing_item['id'] if playing_item else None
    db.add_reaction(user_id, reaction, current_queue_id)
    counts = db.get_reaction_counts(current_queue_id) if current_queue_id else db.get_reaction_counts()
    return jsonify({'success': True, 'reactions': counts})


@app.route('/api/user/nickname', methods=['POST'])
@limiter.limit('10 per minute')
@require_party
def api_set_nickname():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({'error': 'Missing user ID'}), 400

    user = get_or_create_user(user_id)
    if not user:
        return jsonify({'error': 'Invalid user ID'}), 400

    data = request.get_json() or {}
    nickname = (data.get('nickname') or '').strip()
    if not nickname:
        return jsonify({'error': 'Nickname cannot be empty'}), 400
    nickname = nickname[:24]

    db.set_user_nickname(user_id, nickname)
    return jsonify({'success': True, 'nickname': nickname})


@app.route('/api/upvote', methods=['POST'])
@limiter.limit('30 per minute')
@require_party
def api_upvote():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({'error': 'Missing user ID'}), 400

    user = get_or_create_user(user_id)
    if not user:
        return jsonify({'error': 'Invalid user ID'}), 400

    data = request.get_json() or {}
    queue_item_id = data.get('queue_item_id')
    if not queue_item_id:
        return jsonify({'error': 'Missing queue_item_id'}), 400

    item = db.get_queue_item(queue_item_id)
    if not item:
        return jsonify({'error': 'Queue item not found'}), 404

    added = db.add_upvote(queue_item_id, user_id)
    if not added:
        return jsonify({'error': 'You already upvoted this song'}), 409

    count = db.get_upvote_count(queue_item_id)
    return jsonify({'success': True, 'upvote_count': count})


@app.route('/api/queue/<int:queue_id>', methods=['DELETE'])
@limiter.limit('20 per minute')
@require_party
def api_remove_queue_item(queue_id):
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({'error': 'Missing user ID'}), 400

    item = db.get_queue_item(queue_id)
    if not item:
        return jsonify({'error': 'Queue item not found'}), 404
    if item['requested_by'] != user_id:
        return jsonify({'error': 'You can only remove your own songs'}), 403

    removed = db.remove_pending_by_user(queue_id, user_id)
    if not removed:
        return jsonify({'error': 'Song is no longer pending'}), 404
    return jsonify({'success': True})


_SHOT_ID_CHARS = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-')


@app.route('/api/photos', methods=['POST'])
@limiter.limit('20 per minute')
@require_party
def api_upload_photo():
    """A disposable camera shot. The guest never sees it again: it waits for
    the host's review, and only approved shots ever leave this machine."""
    user_id = get_user_id_from_request()
    user = get_or_create_user(user_id)
    if not user:
        return jsonify({'error': 'Invalid user ID'}), 400
    if user['is_banned']:
        return jsonify({'error': "Silenced guests can't use the camera.", 'code': 'banned'}), 403

    camera = _camera_state(user_id)
    if not camera['enabled']:
        return jsonify({'error': 'The camera is switched off.', 'code': 'camera_off'}), 403

    shot_id = (request.form.get('shot_id') or '').strip()
    if not 8 <= len(shot_id) <= 64 or not set(shot_id) <= _SHOT_ID_CHARS:
        return jsonify({'error': 'Missing shot ID'}), 400

    # A retry of a shot we already have: say yes again without reprocessing,
    # so the phone can drop it from its outbox.
    if db.has_photo_shot(user_id, shot_id):
        return jsonify({'success': True, 'duplicate': True, 'shots_left': camera['shots_left']})
    if camera['shots_left'] <= 0:
        return jsonify({'error': "You're out of film!", 'code': 'out_of_film', 'shots_left': 0}), 409

    upload = request.files.get('photo')
    if not upload:
        return jsonify({'error': 'No photo attached'}), 400
    try:
        jpeg, width, height = photos.process(upload.read())
    except photos.PhotoError as e:
        return jsonify({'error': str(e), 'code': 'bad_photo'}), 400

    party_code = db.get_setting('party_code', '')
    filename = photos.save(jpeg, party_code)
    result, _ = db.add_photo(user_id, shot_id, party_code, filename, width, height,
                             limit=camera['shots_total'])
    if result != 'ok':
        photos.delete(filename)
    if result == 'full':
        return jsonify({'error': "You're out of film!", 'code': 'out_of_film', 'shots_left': 0}), 409

    left = _camera_state(user_id)['shots_left']
    return jsonify({'success': True, 'duplicate': result == 'duplicate', 'shots_left': left})


# ---------------------------------------------------------------------------
# Costume contest (guests)
# ---------------------------------------------------------------------------
def _costume_guest():
    """The calling guest, or an error response if they can't take part right now."""
    user = get_or_create_user(get_user_id_from_request())
    if not user:
        return None, (jsonify({'error': 'Invalid user ID'}), 400)
    if user['is_banned']:
        return None, (jsonify({'error': "Silenced guests can't join the costume contest.",
                               'code': 'banned'}), 403)
    if _costume_phase() != 'open':
        return None, (jsonify({'error': 'Costume voting is closed.',
                               'code': 'contest_closed'}), 409)
    return user, None


@app.route('/api/costume/entry', methods=['POST'])
@limiter.limit('10 per minute')
@require_party
def api_costume_enter():
    """Enter the contest, or update your entry. Multipart form: costume (what
    you came as) and photo, which is required to enter and optional after."""
    user, err = _costume_guest()
    if err:
        return err
    costume = ' '.join((request.form.get('costume') or '').split())
    if not costume:
        return jsonify({'error': 'Tell us what you came as!'}), 400
    if len(costume) > COSTUME_MAX_LEN:
        return jsonify({'error': f'Keep it under {COSTUME_MAX_LEN} characters.'}), 400

    party_code = db.get_setting('party_code', '')
    existing = next((e for e in db.costume_board(party_code)
                     if e['user_id'] == user['user_id']), None)
    upload = request.files.get('photo')
    filename = None
    if upload:
        try:
            jpeg = photos.square(upload.read())
        except photos.PhotoError as e:
            return jsonify({'error': str(e), 'code': 'bad_photo'}), 400
        filename = photos.save(jpeg, party_code, subdir='costumes')
    elif not (existing and existing['photo']):
        return jsonify({'error': 'Add a photo of your costume so people know who to vote for.',
                        'code': 'photo_required'}), 400

    entry_id, replaced = db.save_costume_entry(party_code, user['user_id'], costume, filename)
    if replaced:
        photos.delete(replaced)
    entry = db.get_costume_entry(entry_id)
    return jsonify({'success': True, 'entry': {
        'id': entry_id, 'costume': costume, 'photo_url': _costume_photo_url(entry)}})


@app.route('/api/costume/entry', methods=['DELETE'])
@limiter.limit('10 per minute')
@require_party
def api_costume_withdraw():
    user, err = _costume_guest()
    if err:
        return err
    mine = next((e for e in db.costume_board(db.get_setting('party_code', ''))
                 if e['user_id'] == user['user_id']), None)
    if not mine:
        return jsonify({'error': "You haven't entered."}), 404
    _remove_costume_entry(mine['id'])
    return jsonify({'success': True})


def _remove_costume_entry(entry_id):
    removed = db.delete_costume_entry(entry_id)
    if removed and removed.get('photo'):
        photos.delete(removed['photo'])
    return removed


@app.route('/api/costume/photo/<int:entry_id>')
@require_party
def api_costume_photo(entry_id):
    """An entry's photo. Entrants put it up for everyone at the party, so any
    guest with tonight's code (and the TV) can see it, but only tonight's."""
    entry = db.get_costume_entry(entry_id)
    if (not entry or not entry.get('photo')
            or entry['party_code'] != db.get_setting('party_code', '')):
        return jsonify({'error': 'Photo not found'}), 404
    try:
        path = photos.path_for(entry['photo'])
    except photos.PhotoError:
        return jsonify({'error': 'Photo not found'}), 404
    if not os.path.exists(path):
        return jsonify({'error': 'Photo file is missing'}), 404
    res = send_file(path, mimetype='image/jpeg')
    # The URL changes when the photo does, so the browser may keep it a while.
    res.headers['Cache-Control'] = 'private, max-age=3600'
    return res


@app.route('/api/costume/vote', methods=['POST'])
@limiter.limit('20 per minute')
@require_party
def api_costume_vote():
    """Vote for {"entry_id": 3}. Voting again moves your vote."""
    user, err = _costume_guest()
    if err:
        return err
    try:
        entry_id = int((request.get_json() or {}).get('entry_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Pick a costume to vote for.'}), 400
    result = db.cast_costume_vote(db.get_setting('party_code', ''), user['user_id'], entry_id)
    if result == 'own_entry':
        return jsonify({'error': "Nice try! You can't vote for yourself.", 'code': 'own_entry'}), 403
    if result == 'no_entry':
        return jsonify({'error': 'That costume is no longer in the running.'}), 404
    return jsonify({'success': True, 'my_vote': entry_id})


@app.route('/api/tv')
@require_party
def api_tv():
    """TV dashboard payload. Needs the party code (open /tv?p=CODE) or a host session."""
    current_track, playing_queue_item, current_queue_id = _resolve_current_track()

    reactions = db.get_reaction_counts(current_queue_id) if current_queue_id else db.get_reaction_counts()

    pending_queue = db.get_pending_queue()
    queue = [
        {
            'id': q['id'],
            'track_name': q['track_name'],
            'artist': q['artist'],
            'album_art': q['album_art'],
            'nickname': q['nickname'],
            'dedication': q.get('dedication'),
            'upvote_count': q['upvote_count'],
        }
        for q in pending_queue
    ]

    return jsonify({
        'current_track': current_track,
        'reactions': reactions,
        'queue': queue,
        'stats': db.get_party_stats(),
        'leaderboards': db.get_leaderboards(),
        'recent_reactions': db.get_recent_reactions(20),
        'ui_theme': _ui_theme(),
        'costume': _costume_tv(),
    })


# ---------------------------------------------------------------------------
# Host-only API
# ---------------------------------------------------------------------------
@app.route('/api/host/skip', methods=['POST'])
@require_host
def api_host_skip():
    playing_item = db.get_playing_item()
    if playing_item:
        db.update_queue_status(playing_item['id'], 'skipped')
        db.mark_track_played(playing_item['spotify_track_id'])

    qm.advance_to_next_pending()
    return jsonify({'success': True})


@app.route('/api/host/ban', methods=['POST'])
@require_host
def api_host_ban():
    data = request.get_json() or {}
    user_id = data.get('user_id', '').strip()
    if not user_id:
        return jsonify({'error': 'Missing user_id'}), 400
    db.ban_user(user_id)
    return jsonify({'success': True})


@app.route('/api/host/unban', methods=['POST'])
@require_host
def api_host_unban():
    data = request.get_json() or {}
    user_id = data.get('user_id', '').strip()
    if not user_id:
        return jsonify({'error': 'Missing user_id'}), 400
    db.unban_user(user_id)
    return jsonify({'success': True})


@app.route('/api/host/settings', methods=['POST'])
@require_host
def api_host_settings():
    data = request.get_json() or {}

    if 'ui_theme' in data:
        if data['ui_theme'] not in UI_THEMES:
            return jsonify({'error': 'Theme must be "modern" or "classic".'}), 400
        db.set_setting('ui_theme', data['ui_theme'])

    if 'downvote_threshold' in data:
        val = int(data['downvote_threshold'])
        if 1 <= val <= 20:
            db.set_setting('downvote_threshold', val)

    if 'max_queue_per_user' in data:
        val = int(data['max_queue_per_user'])
        if 1 <= val <= 10:
            db.set_setting('max_queue_per_user', val)

    if 'skip_ban_threshold' in data:
        val = int(data['skip_ban_threshold'])
        if 1 <= val <= 10:
            db.set_setting('skip_ban_threshold', val)

    if 'host_password' in data:
        pw = data['host_password'].strip()
        if pw:
            if len(pw) < 8:
                return jsonify({'error': 'Host password must be at least 8 characters.'}), 400
            if pw in db.PLACEHOLDER_HOST_PASSWORDS:
                return jsonify({'error': 'Pick a password other than the default.'}), 400
            db.set_host_password(pw)
            session['is_host'] = True  # keep session valid after pw change

    if 'party_url' in data:
        url = data['party_url'].strip()
        if url:
            db.set_setting('party_url', url)

    if 'preferred_device_id' in data:
        db.set_setting('preferred_device_id', (data['preferred_device_id'] or '').strip())

    if 'idle_shutdown_hours' in data:
        try:
            val = int(data['idle_shutdown_hours'])
        except (TypeError, ValueError):
            return jsonify({'error': 'Idle shutdown must be a whole number of hours.'}), 400
        if not 0 <= val <= 72:
            return jsonify({'error': 'Idle shutdown must be between 0 and 72 hours (0 disables).'}), 400
        db.set_setting('idle_shutdown_hours', val)

    if 'camera_enabled' in data:
        db.set_setting('camera_enabled', '1' if data['camera_enabled'] else '0')

    if 'camera_shots_per_guest' in data:
        try:
            val = int(data['camera_shots_per_guest'])
        except (TypeError, ValueError):
            return jsonify({'error': 'Shots per guest must be a whole number.'}), 400
        if not 1 <= val <= 100:
            return jsonify({'error': 'Shots per guest must be between 1 and 100.'}), 400
        db.set_setting('camera_shots_per_guest', val)

    if 'fallback_playlist_url' in data:
        raw = (data['fallback_playlist_url'] or '').strip()
        if not raw:
            # Empty clears the fallback — the queue simply runs dry in silence.
            db.set_setting('fallback_playlist_id', '')
            db.set_setting('fallback_playlist_name', '')
        else:
            playlist_id = sc.parse_playlist_id(raw)
            if not playlist_id:
                return jsonify({'error': "That doesn't look like a Spotify playlist link."}), 400
            if not sc.is_authenticated():
                return jsonify({'error': 'Connect Spotify before setting a fallback playlist.'}), 400
            meta = sc.get_playlist_meta(playlist_id)
            if not meta:
                return jsonify({'error': "Couldn't read that playlist. Make sure it's yours "
                                         "or public (collaborative playlists aren't supported)."}), 400
            if not meta['track_count']:
                return jsonify({'error': f"'{meta['name']}' has no tracks in it."}), 400
            db.set_setting('fallback_playlist_id', meta['id'])
            db.set_setting('fallback_playlist_name', meta['name'])

    return jsonify({'success': True})


@app.route('/api/host/clear_queue', methods=['POST'])
@require_host
def api_host_clear_queue():
    db.clear_pending_queue()
    playlist_id = db.get_setting('jukebox_playlist_id')
    if playlist_id and sc.is_authenticated():
        sc.clear_jukebox_playlist(playlist_id)
    return jsonify({'success': True})


@app.route('/api/host/users')
@require_host
def api_host_users():
    users = db.get_all_users()
    result = []
    for u in users:
        # Count songs queued by this user
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM queue WHERE requested_by = ?",
                (u['user_id'],)
            ).fetchone()
            songs_queued = row['cnt'] if row else 0
        finally:
            conn.close()
        result.append({
            'user_id': u['user_id'],
            'nickname': u['nickname'],
            'songs_queued': songs_queued,
            'songs_skipped_count': u['songs_skipped_count'],
            'is_banned': bool(u['is_banned']),
            'first_seen': u['first_seen'],
        })
    return jsonify({'users': result})


@app.route('/api/host/spotify_status')
@require_host
def api_host_spotify_status():
    connected = sc.is_authenticated()
    auth_url = url_for('auth_spotify') if not connected else None
    fallback_id = db.get_setting('fallback_playlist_id', '')
    return jsonify({
        'connected': connected,
        'auth_url': auth_url,
        **party_links(),
        'fallback_playlist_id': fallback_id,
        'fallback_playlist_name': db.get_setting('fallback_playlist_name', ''),
        'fallback_playlist_url': (f'https://open.spotify.com/playlist/{fallback_id}'
                                  if fallback_id else ''),
        'standing_down': qm.is_standing_down(),
        'playback_issue': qm.get_playback_issue(),
        'idle_shutdown_hours': int(db.get_setting('idle_shutdown_hours', '6')),
        'preferred_device_id': db.get_setting('preferred_device_id', ''),
    })


@app.route('/api/host/new_party', methods=['POST'])
@require_host
def api_host_new_party():
    """Reset per-party state. Without this, played_tracks accumulates forever
    and previously-played songs stay permanently un-requestable."""
    data = request.get_json() or {}
    stats = db.start_new_party(clear_users=bool(data.get('clear_users')))
    db.rotate_party_code()
    db.set_setting('costume_phase', 'off')
    session['party_code'] = db.get_setting('party_code')
    qm.resume_party()
    return jsonify({'success': True, **stats, **party_links()})


@app.route('/api/host/devices')
@require_host
def api_host_devices():
    return jsonify({
        'devices': sc.list_devices(),
        'preferred_device_id': db.get_setting('preferred_device_id', ''),
    })


@app.route('/api/host/resume_party', methods=['POST'])
@require_host
def api_host_resume_party():
    return jsonify({'success': True, 'was_standing_down': qm.resume_party()})


@app.route('/api/host/reorder', methods=['POST'])
@require_host
def api_host_reorder():
    data = request.get_json() or {}
    queue_id = data.get('queue_id')
    direction = data.get('direction', '').strip()
    if not queue_id or direction not in ('up', 'down'):
        return jsonify({'error': 'Invalid request'}), 400
    swapped = db.reorder_queue_item(int(queue_id), direction)
    if swapped:
        return jsonify({'success': True})
    return jsonify({'error': 'Cannot move further'}), 400


@app.route('/api/host/queue', methods=['POST'])
@require_host
def api_host_queue():
    """Host queues a song directly (works in demo mode without Spotify)."""
    data = request.get_json() or {}
    track_name = data.get('track_name', '').strip()
    artist = data.get('artist', '').strip()
    album_art = data.get('album_art', '')
    duration_ms = int(data.get('duration_ms', 210000))
    track_id = data.get('track_id', '').strip() or f'demo_{uuid.uuid4().hex[:8]}'

    if not track_name or not artist:
        return jsonify({'error': 'Missing track name or artist'}), 400

    db.get_or_create_host_user()
    queue_id = db.add_to_queue(track_id, track_name, artist, album_art, duration_ms, 'host')

    if sc.is_authenticated():
        # The worker's snapshot, not a fresh Spotify call per queued song.
        playback = qm.get_cached_playback()
        if not playback or not playback.get('is_playing'):
            device_id = qm.get_party_device_id()
            ok, err = sc.play_track(f'spotify:track:{track_id}', device_id=device_id)
            qm.note_play_result(ok, err, device_id)
            if ok:
                qm.invalidate_playback_cache()
                db.update_queue_status(queue_id, 'playing')

    return jsonify({'success': True, 'queue_id': queue_id})


@app.route('/host/photos')
def host_photos():
    """Photo review page. The page itself is public like /host; its API isn't."""
    return render_template('photos.html', ui_theme=_ui_theme())


@app.route('/api/host/camera')
@require_host
def api_host_camera():
    """Camera settings plus photo counts for every party, for the host panel."""
    return jsonify({
        'enabled': db.get_setting('camera_enabled', '1') == '1',
        'shots_per_guest': int(db.get_setting('camera_shots_per_guest', '24')),
        'current_party': db.get_setting('party_code', ''),
        'parties': db.photo_parties(),
    })


@app.route('/api/host/photos')
@require_host
def api_host_photos():
    party = request.args.get('party') or db.get_setting('party_code', '')
    status = request.args.get('status') or None
    if status and status not in db.PHOTO_STATUSES:
        return jsonify({'error': 'Unknown status'}), 400
    return jsonify({
        'party': party,
        'photos': [
            {k: p[k] for k in ('id', 'nickname', 'user_id', 'status', 'created_at', 'width', 'height')}
            for p in db.list_photos(party_code=party, status=status)
        ],
    })


@app.route('/api/host/photos/<int:photo_id>/image')
@require_host
def api_host_photo_image(photo_id):
    photo = db.get_photo(photo_id)
    if not photo:
        return jsonify({'error': 'Photo not found'}), 404
    try:
        path = photos.path_for(photo['filename'])
    except photos.PhotoError:
        return jsonify({'error': 'Photo not found'}), 404
    if not os.path.exists(path):
        return jsonify({'error': 'Photo file is missing'}), 404
    res = send_file(path, mimetype='image/jpeg')
    # Host-only content: never let a browser or proxy cache keep a copy.
    res.headers['Cache-Control'] = 'private, no-store'
    return res


@app.route('/api/host/photos/review', methods=['POST'])
@require_host
def api_host_photo_review():
    """Approve, reject or un-review photos: {"ids": [...], "status": "approved"}."""
    data = request.get_json() or {}
    status = data.get('status')
    ids = data.get('ids')
    if status not in db.PHOTO_STATUSES:
        return jsonify({'error': 'Status must be pending, approved or rejected.'}), 400
    if not isinstance(ids, list) or not ids:
        return jsonify({'error': 'No photos selected.'}), 400
    try:
        changed = db.set_photo_status(ids, status)
    except (TypeError, ValueError):
        return jsonify({'error': 'Photo IDs must be numbers.'}), 400
    return jsonify({'success': True, 'changed': changed})


# ---------------------------------------------------------------------------
# Costume contest (host)
# ---------------------------------------------------------------------------
@app.route('/api/host/costume')
@require_host
def api_host_costume():
    """The live tally, which only the host sees while voting is open."""
    party_code = db.get_setting('party_code', '')
    board = _ranked(db.costume_board(party_code))
    return jsonify({
        'phase': _costume_phase(),
        'entries': [{**{k: e[k] for k in ('id', 'nickname', 'costume', 'votes', 'rank')},
                     'photo_url': _costume_photo_url(e)} for e in board],
        'votes': sum(e['votes'] for e in board),
    })


@app.route('/api/host/costume/phase', methods=['POST'])
@require_host
def api_host_costume_phase():
    """{"phase": "open"} starts voting, "closed" ends it and reveals the
    winner on the TV, "off" hides the contest again."""
    phase = (request.get_json() or {}).get('phase')
    if phase not in db.COSTUME_PHASES:
        return jsonify({'error': 'Phase must be off, open or closed.'}), 400
    if phase == 'closed' and _costume_phase() != 'closed':
        db.set_setting('costume_closed_at', time.time())
    db.set_setting('costume_phase', phase)
    return jsonify({'success': True, 'phase': phase})


@app.route('/api/host/costume/entry/<int:entry_id>', methods=['DELETE'])
@require_host
def api_host_costume_remove(entry_id):
    entry = db.get_costume_entry(entry_id)
    if not entry or entry['party_code'] != db.get_setting('party_code', ''):
        return jsonify({'error': 'Entry not found'}), 404
    _remove_costume_entry(entry_id)
    return jsonify({'success': True})


@app.route('/api/host/demo', methods=['POST'])
@require_host
def api_host_demo():
    """Toggle demo mode and/or set the fake now-playing track."""
    data = request.get_json() or {}

    enabled = data.get('enabled')
    if enabled is not None:
        db.set_setting('demo_mode', '1' if enabled else '0')

    track = data.get('track')
    if track:
        db.set_setting('demo_current_track', json.dumps({
            'track_id': 'demo',
            'track_name': track.get('track_name', 'Demo Track'),
            'artist': track.get('artist', 'Demo Artist'),
            'album_art': track.get('album_art', ''),
            'duration_ms': 210000,
            'progress_ms': 60000,
            'is_playing': True,
        }))

    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def config_problems():
    """Settings that are fine on a laptop but unsafe once guests can reach the app."""
    problems = []
    secret = os.environ.get('FLASK_SECRET_KEY', '')
    if not secret or secret in _PLACEHOLDER_SECRETS or len(secret) < 32:
        problems.append(
            "FLASK_SECRET_KEY is missing or too short; anyone could forge a host "
            "session. Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"")
    if db.host_password_is_placeholder():
        problems.append(
            "The host password is still the default. Set HOST_PASSWORD in .env "
            "(or change it in the host panel).")
    return problems


def create_app():
    db.init_db()
    problems = config_problems()
    for p in problems:
        logger.warning(p)
    if problems and not DEV_MODE:
        raise SystemExit("Refusing to start with unsafe settings (set JUKEBOX_DEV=1 "
                         "to override on a private machine).")
    qm.start_background_thread()
    return app


if __name__ == '__main__':
    from waitress import serve

    # Not 5000: macOS's AirPlay Receiver listens there and answers 403.
    port = int(os.environ.get('PORT', 5001))
    bind_host = os.environ.get('HOST') or ('127.0.0.1' if BEHIND_PROXY else '0.0.0.0')
    create_app()
    # Exactly ONE process. queue_manager keeps playback state in memory and its
    # worker thread must run once; a second process would fight it over
    # Spotify. Scale with threads (guests mostly poll), never with workers.
    logger.info("Serving on http://%s:%s (behind proxy: %s)", bind_host, port, BEHIND_PROXY)
    serve(app, host=bind_host, port=port, threads=16, ident='jukebox')
