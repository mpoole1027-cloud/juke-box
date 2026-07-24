import os
import spotipy
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

PLAYLIST_NAME = "🎃 Party Jukebox 🎃"


def get_oauth(show_dialog=False):
    return SpotifyOAuth(
        client_id=os.environ.get('SPOTIFY_CLIENT_ID', ''),
        client_secret=os.environ.get('SPOTIFY_CLIENT_SECRET', ''),
        redirect_uri=os.environ.get('SPOTIFY_REDIRECT_URI', 'http://127.0.0.1:5000/auth/callback'),
        scope=SCOPES,
        cache_handler=CacheFileHandler(cache_path=TOKEN_CACHE_PATH),
        open_browser=False,
        show_dialog=show_dialog,
    )


def get_spotify():
    """Return an authenticated Spotipy client, or None if not authenticated."""
    try:
        oauth = get_oauth()
        token_info = oauth.get_cached_token()
        if not token_info:
            return None
        if oauth.is_token_expired(token_info):
            token_info = oauth.refresh_access_token(token_info['refresh_token'])
        sp = spotipy.Spotify(auth=token_info['access_token'])
        return sp
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


def get_auth_url():
    oauth = get_oauth(show_dialog=True)  # force full consent dialog so new scopes are granted
    return oauth.get_authorize_url()


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


def search_tracks(query, limit=10):
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
        return True, None
    except Exception as e:
        logger.error(f"Error playing track: {e}")
        return False, str(e)


def pause_playback(device_id=None):
    sp = get_spotify()
    if not sp:
        return
    try:
        sp.pause_playback(device_id=device_id)
    except Exception as e:
        logger.error(f"Error pausing playback: {e}")


def add_to_spotify_queue(track_uri, device_id=None):
    sp = get_spotify()
    if not sp:
        return False, "Not authenticated with Spotify"
    try:
        sp.add_to_queue(track_uri, device_id=device_id)
        return True, None
    except Exception as e:
        logger.error(f"Error adding to Spotify queue: {e}")
        return False, str(e)


def skip_track(device_id=None):
    sp = get_spotify()
    if not sp:
        return False, "Not authenticated with Spotify"
    try:
        sp.next_track(device_id=device_id)
        return True, None
    except Exception as e:
        logger.error(f"Error skipping track: {e}")
        return False, str(e)


def get_or_create_jukebox_playlist():
    """Find our private jukebox playlist or create it. Returns playlist_id or None."""
    sp = get_spotify()
    if not sp:
        return None
    try:
        user_id = sp.me()['id']
        offset = 0
        while True:
            result = sp.current_user_playlists(limit=50, offset=offset)
            for p in result['items']:
                if p and p.get('name') == PLAYLIST_NAME:
                    return p['id']
            if not result['next']:
                break
            offset += 50
        playlist = sp.user_playlist_create(
            user_id, PLAYLIST_NAME, public=False,
            description="Managed by Party Jukebox app — do not edit manually"
        )
        logger.info(f"Created jukebox playlist: {playlist['id']}")
        return playlist['id']
    except Exception as e:
        logger.error(f"Error in get_or_create_jukebox_playlist: {e}")
        return None


def add_track_to_playlist(playlist_id, track_uri):
    sp = get_spotify()
    if not sp:
        return False, "Not authenticated"
    try:
        sp.playlist_add_items(playlist_id, [track_uri])
        return True, None
    except Exception as e:
        logger.error(f"Error adding track to playlist: {e}")
        return False, str(e)


def remove_track_from_playlist(playlist_id, track_uri):
    sp = get_spotify()
    if not sp:
        return
    try:
        sp.playlist_remove_all_occurrences_of_items(playlist_id, [track_uri])
    except Exception as e:
        logger.error(f"Error removing track from playlist: {e}")


def clear_jukebox_playlist(playlist_id):
    sp = get_spotify()
    if not sp:
        return
    try:
        sp.playlist_replace_items(playlist_id, [])
    except Exception as e:
        logger.error(f"Error clearing playlist: {e}")


def start_playlist_playback(playlist_id, device_id=None):
    """Play our jukebox playlist with shuffle and repeat off."""
    sp = get_spotify()
    if not sp:
        return False, "Not authenticated"
    try:
        sp.start_playback(device_id=device_id, context_uri=f'spotify:playlist:{playlist_id}')
        try:
            sp.shuffle(False, device_id=device_id)
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


def get_active_device_id():
    sp = get_spotify()
    if not sp:
        return None
    try:
        devices = sp.devices()
        if not devices or not devices.get('devices'):
            return None
        active = [d for d in devices['devices'] if d['is_active']]
        if active:
            return active[0]['id']
        return devices['devices'][0]['id']
    except Exception as e:
        logger.error(f"Error getting device ID: {e}")
        return None
