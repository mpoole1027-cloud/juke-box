import os
import uuid
import json
import random
import io
import logging
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for, send_file, make_response
)
from dotenv import load_dotenv

load_dotenv()

import database as db
import spotify_client as sc
import queue_manager as qm

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-change-me')

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
    """Decorator: requires either a valid host session or X-Host-Password header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('is_host'):
            return f(*args, **kwargs)
        header_pw = request.headers.get('X-Host-Password', '')
        stored_pw = db.get_setting('host_password', 'party2024')
        if header_pw == stored_pw:
            return f(*args, **kwargs)
        return jsonify({'error': 'Unauthorized'}), 401
    return decorated


def get_user_id_from_request():
    return request.headers.get('X-User-ID', '').strip()


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
        playback = sc.get_current_playback()
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


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/host')
def host():
    return render_template('host.html')


@app.route('/tv')
def tv():
    return render_template('tv.html')


@app.route('/qr')
def qr_code():
    import qrcode
    party_url = db.get_setting('party_url', os.environ.get('PARTY_URL', 'http://localhost:5000'))
    img = qrcode.make(party_url)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


# ---------------------------------------------------------------------------
# Spotify auth routes
# ---------------------------------------------------------------------------
@app.route('/auth/spotify')
def auth_spotify():
    auth_url = sc.get_auth_url()
    return redirect(auth_url)


@app.route('/auth/callback')
def auth_callback():
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
def host_login():
    data = request.get_json() or {}
    password = data.get('password', '')
    stored_pw = db.get_setting('host_password', 'party2024')
    if password == stored_pw:
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
    user_id = get_user_id_from_request()
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
                'track_name': q['track_name'],
                'artist': q['artist'],
                'album_art': q['album_art'],
                'duration_ms': q['duration_ms'],
                'status': q['status'],
                'requested_by': q['requested_by'],
                'nickname': q['nickname'],
                'dedication': q.get('dedication'),
                'upvote_count': q['upvote_count'],
                'user_has_upvoted': db.user_has_upvoted(q['id'], user_id) if user_id else False,
            }
            for q in pending_queue
        ],
        'reactions': reactions,
        'banned_users': banned_users,
        'user': {
            'user_id': user['user_id'],
            'nickname': user['nickname'],
            'is_banned': bool(user['is_banned']),
        } if user else None,
        'settings': settings,
        'spotify_connected': sc.is_authenticated(),
    })


@app.route('/api/search')
def api_search():
    user_id = get_user_id_from_request()
    if user_id:
        get_or_create_user(user_id)

    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'tracks': []})

    if not sc.is_authenticated():
        return jsonify({'error': 'Spotify not connected', 'tracks': []}), 503

    tracks = sc.search_tracks(q, limit=10)
    return jsonify({'tracks': tracks})


@app.route('/api/queue', methods=['POST'])
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
            device_id = sc.get_active_device_id()
            ok, _ = sc.play_track(f'spotify:track:{track_id}', device_id=device_id)
            if ok:
                db.update_queue_status(queue_id, 'playing')

    return jsonify({'success': True, 'queue_id': queue_id})


@app.route('/api/downvote', methods=['POST'])
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

        device_id = sc.get_active_device_id()
        pending = db.get_pending_queue()
        if pending:
            next_item = pending[0]
            ok, _ = sc.play_track(f"spotify:track:{next_item['spotify_track_id']}", device_id=device_id)
            if ok:
                db.update_queue_status(next_item['id'], 'playing')
        else:
            sc.pause_playback(device_id=device_id)

    return jsonify({'success': True, 'downvote_count': count, 'threshold': threshold})


@app.route('/api/react', methods=['POST'])
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
def api_tv():
    """Public TV dashboard payload (no auth)."""
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

    device_id = sc.get_active_device_id()
    pending = db.get_pending_queue()
    if pending:
        next_item = pending[0]
        ok, err = sc.play_track(f"spotify:track:{next_item['spotify_track_id']}", device_id=device_id)
        if ok:
            db.update_queue_status(next_item['id'], 'playing')
            return jsonify({'success': True})
        return jsonify({'error': err or 'Failed to play next song'}), 500
    else:
        sc.pause_playback(device_id=device_id)
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
            db.set_setting('host_password', pw)
            session['is_host'] = True  # keep session valid after pw change

    if 'party_url' in data:
        url = data['party_url'].strip()
        if url:
            db.set_setting('party_url', url)

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
    auth_url = sc.get_auth_url() if not connected else None
    party_url = db.get_setting('party_url', os.environ.get('PARTY_URL', 'http://localhost:5000'))
    return jsonify({
        'connected': connected,
        'auth_url': auth_url,
        'party_url': party_url,
    })


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
            device_id = sc.get_active_device_id()
            ok, _ = sc.play_track(f'spotify:track:{track_id}', device_id=device_id)
            if ok:
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
def create_app():
    db.init_db()
    qm.start_background_thread()
    return app


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    create_app()
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
