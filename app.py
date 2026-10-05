import os
import hmac
import secrets
import uuid
import json
import random
import io
import logging
from datetime import timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for, send_file, make_response
)
from dotenv import load_dotenv
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

load_dotenv()

import database as db
import spotify_client as sc
import queue_manager as qm

logging.basicConfig(level=logging.INFO)
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
    party_url = db.get_setting('party_url', os.environ.get('PARTY_URL', 'http://localhost:5000'))
    base = party_url.rstrip('/')
    code = db.get_setting('party_code', '')
    return {
        'party_url': party_url,
        'party_code': code,
        'invite_url': f'{base}/?p={code}',
        'tv_url': f'{base}/tv?p={code}',
    }


def get_or_init_playlist():
    """Return the cached jukebox playlist_id, creating it if needed."""
    playlist_id = db.get_setting('jukebox_playlist_id')
    if not playlist_id:
        playlist_id = sc.get_or_create_jukebox_playlist()
        if playlist_id:
            db.set_setting('jukebox_playlist_id', playlist_id)
    return playlist_id


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
                'user_has_upvoted': db.user_has_upvoted(q['id'], user_id) if user_id else False,
            }
            for q in pending_queue
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
        playback = sc.get_current_playback()
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
        playback = sc.get_current_playback()
        if not playback or not playback.get('is_playing'):
            device_id = qm.get_party_device_id()
            ok, err = sc.play_track(f'spotify:track:{track_id}', device_id=device_id)
            qm.note_play_result(ok, err, device_id)
            if ok:
                qm.invalidate_playback_cache()
                db.update_queue_status(queue_id, 'playing')

    return jsonify({'success': True, 'queue_id': queue_id})


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

    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST') or ('127.0.0.1' if BEHIND_PROXY else '0.0.0.0')
    create_app()
    # Exactly ONE process. queue_manager keeps playback state in memory and its
    # worker thread must run once; a second process would fight it over
    # Spotify. Scale with threads (guests mostly poll), never with workers.
    logger.info("Serving on http://%s:%s (behind proxy: %s)", host, port, BEHIND_PROXY)
    serve(app, host=host, port=port, threads=16, ident='jukebox')
