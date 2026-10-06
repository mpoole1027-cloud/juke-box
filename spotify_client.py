import os
import re
import random
import threading
import time
from collections import OrderedDict

import requests
import spotipy
from urllib3.util.retry import Retry
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheFileHandler
import logging

logger = logging.getLogger(__name__)

TOKEN_CACHE_PATH = os.path.join(os.path.dirname(__file__), 'token_cache.json')

SCOPES = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "user-read-currently-playing "
    "playlist-modify-private "
    "playlist-read-private "
    "streaming"
)


def get_oauth(show_dialog=False):
    return SpotifyOAuth(
        client_id=os.environ.get('SPOTIFY_CLIENT_ID', ''),
        client_secret=os.environ.get('SPOTIFY_CLIENT_SECRET', ''),
        redirect_uri=os.environ.get('SPOTIFY_REDIRECT_URI', 'http://127.0.0.1:5001/auth/callback'),
        scope=SCOPES,
        cache_handler=CacheFileHandler(cache_path=TOKEN_CACHE_PATH),
        open_browser=False,
        show_dialog=show_dialog,
    )


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
# Spotify answers a busy app with 429 and a Retry-After that can run to
# minutes. spotipy's default session retries 429s by sleeping through that
# Retry-After inside the call, which would park a request thread per guest
# search until all 16 are stuck and every page hangs. Instead: never retry a
# 429, remember when Spotify said to come back, and fail fast until then.
DEFAULT_RETRY_AFTER = 30
_retry_until = 0.0


class RateLimited(Exception):
    """Raised instead of calling Spotify while it has asked us to back off."""


def rate_limited_for():
    """Seconds left before Spotify will take calls again (0 when it will)."""
    return max(0.0, _retry_until - time.monotonic())


def _note_retry_after(value):
    global _retry_until
    try:
        wait = max(1, int(value))
    except (TypeError, ValueError):
        wait = DEFAULT_RETRY_AFTER
    _retry_until = max(_retry_until, time.monotonic() + wait)
    logger.warning("Spotify rate limit hit: backing off for %ss.", wait)


class _SpotifySession(requests.Session):
    def request(self, method, url, *args, **kwargs):
        wait = rate_limited_for()
        if wait:
            raise RateLimited(f"Spotify asked us to wait; {wait:.0f}s left")
        response = super().request(method, url, *args, **kwargs)
        if response.status_code == 429:
            _note_retry_after(response.headers.get('Retry-After'))
        return response


def _make_session():
    # One shared session so calls reuse connections. Same retries as spotipy's
    # default, minus 429.
    session = _SpotifySession()
    retry = Retry(total=3, connect=None, read=False, status=3, backoff_factor=0.3,
                  allowed_methods=frozenset(['GET', 'POST', 'PUT', 'DELETE']),
                  status_forcelist=(500, 502, 503, 504))
    adapter = requests.adapters.HTTPAdapter(max_retries=retry, pool_maxsize=20)
    session.mount('https://', adapter)
    return session


_session = _make_session()
_client = (None, None)          # (access token, spotipy client)


def _client_for(token):
    # Reuse one client per token: spotipy closes its session when a client is
    # garbage-collected, so a fresh client per call would keep tearing down
    # the shared connection pool.
    global _client
    current = _client
    if current[0] != token:
        current = (token, spotipy.Spotify(auth=token, requests_session=_session))
        _client = current
    return current[1]


def get_spotify():
    """Return an authenticated Spotipy client, or None if not authenticated."""
    try:
        oauth = get_oauth()
        token_info = oauth.get_cached_token()
        if not token_info:
            return None
        if oauth.is_token_expired(token_info):
            token_info = oauth.refresh_access_token(token_info['refresh_token'])
        return _client_for(token_info['access_token'])
    except Exception as e:
        logger.error(f"Error getting Spotify client: {e}")
        return None


def is_authenticated():
    try:
        oauth = get_oauth()
        token_info = oauth.get_cached_token()
        return token_info is not None
    except Exception:
        return False


def get_auth_url(state=None):
    """Spotify consent URL. `state` is echoed back to /auth/callback so the app
    can reject callbacks it didn't start (OAuth CSRF)."""
    oauth = get_oauth(show_dialog=True)  # force full consent dialog so new scopes are granted
    return oauth.get_authorize_url(state=state)


def handle_callback(code):
    """Exchange auth code for token. Returns token_info or None."""
    try:
        oauth = get_oauth()
        token_info = oauth.get_access_token(code, as_dict=True, check_cache=False)
        return token_info
    except Exception as e:
        logger.error(f"Error handling Spotify callback: {e}")
        return None


def get_current_playback():
    sp = get_spotify()
    if not sp:
        return None
    try:
        return sp.current_playback()
    except Exception as e:
        logger.error(f"Error getting current playback: {e}")
        return None


# Playback state, for callers that must tell "nothing is playing" apart from
# "we couldn't find out".
PLAYBACK_OK = 'ok'
PLAYBACK_IDLE = 'idle'
PLAYBACK_ERROR = 'error'


def get_playback_state():
    """Like get_current_playback(), but says *why* there's no track.

    get_current_playback() returns None for a dead token, a network failure and
    a genuinely idle player alike. The queue worker reads "no track" as "the
    song ended", so a momentary Wi-Fi drop mid-song used to mark the guest's
    song played (permanently un-requestable) and skip to the next one.

    Returns {'status': PLAYBACK_OK | PLAYBACK_IDLE | PLAYBACK_ERROR,
             'playback': <spotify payload or None>,
             'reason': <short string, only when status is PLAYBACK_ERROR>}
    """
    sp = get_spotify()
    if not sp:
        # No usable client: the token is missing or refresh failed. That is a
        # failure, not an idle player.
        return {'status': PLAYBACK_ERROR, 'playback': None, 'reason': 'not_authenticated'}
    try:
        playback = sp.current_playback()
    except RateLimited:
        return {'status': PLAYBACK_ERROR, 'playback': None, 'reason': 'rate_limited'}
    except Exception as e:
        logger.error(f"Error getting current playback: {e}")
        return {'status': PLAYBACK_ERROR, 'playback': None, 'reason': str(e)}

    if not playback or not playback.get('item'):
        return {'status': PLAYBACK_IDLE, 'playback': playback}
    return {'status': PLAYBACK_OK, 'playback': playback}


# Search results barely change over a night, and guests type the same artists
# and the same prefixes on the way to them. A hit costs Spotify nothing.
SEARCH_CACHE_SECONDS = 15 * 60
SEARCH_CACHE_SIZE = 1000
_search_cache = OrderedDict()   # (query, limit) -> (monotonic time, tracks)
_search_lock = threading.Lock()


def _normalize_query(query):
    return ' '.join(query.lower().split())


def search_tracks(query, limit=10):
    key = (_normalize_query(query), limit)
    with _search_lock:
        hit = _search_cache.get(key)
        if hit and time.monotonic() - hit[0] < SEARCH_CACHE_SECONDS:
            _search_cache.move_to_end(key)
            return hit[1]
    tracks = _search_spotify(query, limit)
    if tracks:
        # Only cache real answers, so a failed call is retried next time.
        with _search_lock:
            _search_cache[key] = (time.monotonic(), tracks)
            _search_cache.move_to_end(key)
            while len(_search_cache) > SEARCH_CACHE_SIZE:
                _search_cache.popitem(last=False)
    return tracks


def _search_spotify(query, limit):
    sp = get_spotify()
    if not sp:
        return []
    try:
        results = sp.search(q=query, type='track', limit=limit)
        tracks = []
        for item in results['tracks']['items']:
            tracks.append({
                'track_id': item['id'],
                'track_name': item['name'],
                'artist': ', '.join(a['name'] for a in item['artists']),
                'album': item['album']['name'],
                'album_art': item['album']['images'][0]['url'] if item['album']['images'] else '',
                'duration_ms': item['duration_ms'],
                'uri': item['uri'],
            })
        return tracks
    except Exception as e:
        logger.error(f"Error searching Spotify: {e}")
        return []


def play_track(track_uri, device_id=None):
    """Play a single specific track, overriding whatever Spotify has queued."""
    sp = get_spotify()
    if not sp:
        return False, "Not authenticated with Spotify"
    try:
        sp.start_playback(device_id=device_id, uris=[track_uri])
        # Best-effort: disable repeat so a finished single track stops instead
        # of looping (otherwise the queue never advances).
        try:
            sp.repeat('off', device_id=device_id)
        except Exception:
            pass
        return True, None
    except Exception as e:
        logger.error(f"Error playing track: {e}")
        return False, str(e)


def clear_jukebox_playlist(playlist_id):
    sp = get_spotify()
    if not sp:
        return
    try:
        sp.playlist_replace_items(playlist_id, [])
    except Exception as e:
        logger.error(f"Error clearing playlist: {e}")


PLAYLIST_ID_RE = re.compile(r'^[A-Za-z0-9]{22}$')


def parse_playlist_id(url_or_uri):
    """Extract a playlist id from a share link, a spotify: URI, or a bare id.

    Returns the id, or None if nothing playlist-shaped is found.
    """
    if not url_or_uri:
        return None
    value = url_or_uri.strip()

    # spotify:playlist:<id>
    m = re.search(r'playlist[:/]([A-Za-z0-9]{22})', value)
    if m:
        return m.group(1)

    # Bare id — strip any ?si=... tracking suffix first.
    bare = value.split('?')[0].rstrip('/')
    if PLAYLIST_ID_RE.match(bare):
        return bare
    return None


def get_playlist_meta(playlist_id):
    """Return {'id', 'name', 'track_count'} for a playlist, or None if we can't
    read it (bad id, deleted, or not visible to this account)."""
    sp = get_spotify()
    if not sp:
        return None
    try:
        pl = sp.playlist(playlist_id, fields='id,name,tracks.total')
        return {
            'id': pl['id'],
            'name': pl.get('name') or 'Untitled playlist',
            'track_count': (pl.get('tracks') or {}).get('total', 0),
        }
    except Exception as e:
        logger.error(f"Error reading playlist {playlist_id}: {e}")
        return None


def start_playlist_playback(playlist_id, device_id=None, shuffle=False,
                            random_offset=False, track_count=None):
    """Play a playlist. Defaults match the original behaviour (shuffle and
    repeat off, starting at track 1).

    shuffle:       turn Spotify shuffle on for this context.
    random_offset: begin on a random track. A context_uri alone always starts
                   at track 1 even with shuffle enabled, so a party that
                   restarts the fallback would otherwise hear the same opener
                   every time.
    """
    sp = get_spotify()
    if not sp:
        return False, "Not authenticated"
    try:
        # Shuffle must be set before playback starts, or Spotify applies it
        # only from the *next* track onward.
        try:
            sp.shuffle(bool(shuffle), device_id=device_id)
        except Exception:
            pass

        kwargs = {'device_id': device_id, 'context_uri': f'spotify:playlist:{playlist_id}'}
        if random_offset:
            if track_count is None:
                meta = get_playlist_meta(playlist_id)
                track_count = meta['track_count'] if meta else 0
            if track_count and track_count > 1:
                kwargs['offset'] = {'position': random.randrange(track_count)}

        sp.start_playback(**kwargs)
        try:
            sp.repeat('off', device_id=device_id)
        except Exception:
            pass
        return True, None
    except Exception as e:
        logger.error(f"Error starting playlist playback: {e}")
        return False, str(e)


def is_playing_our_playlist(playlist_id):
    """Return True if Spotify is currently playing from our jukebox playlist."""
    playback = get_current_playback()
    if not playback:
        return False
    context = playback.get('context') or {}
    return context.get('uri') == f'spotify:playlist:{playlist_id}'


def list_devices():
    """All Spotify Connect devices visible to this account."""
    sp = get_spotify()
    if not sp:
        return []
    try:
        devices = sp.devices() or {}
        return [
            {'id': d['id'], 'name': d.get('name', 'Unknown'),
             'type': d.get('type', ''), 'is_active': bool(d.get('is_active'))}
            for d in devices.get('devices', []) if d.get('id')
        ]
    except Exception as e:
        logger.error(f"Error listing devices: {e}")
        return []


def get_active_device_id(preferred_device_id=None):
    """Resolve which device the party should play on.

    Order: the host's pinned device (if it's still online) > whatever Spotify
    reports as active > first available. Without a pin the last case is a coin
    flip, which at a party can mean the host's phone instead of the speakers.
    """
    sp = get_spotify()
    if not sp:
        return None
    try:
        devices = sp.devices()
        if not devices or not devices.get('devices'):
            return None
        available = devices['devices']

        if preferred_device_id:
            for d in available:
                if d['id'] == preferred_device_id:
                    return d['id']
            logger.warning(
                f"Pinned device {preferred_device_id} is offline — falling back.")

        active = [d for d in available if d['is_active']]
        if active:
            return active[0]['id']
        return available[0]['id']
    except Exception as e:
        logger.error(f"Error getting device ID: {e}")
        return None
