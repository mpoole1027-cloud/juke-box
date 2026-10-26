"""Tonight's setlist as a private Spotify playlist. Used by the host panel and
by deploy/party.py off, so it works without a browser session too."""
import logging
import time
from datetime import datetime, timezone

import database as db
import spotify_client as sc

logger = logging.getLogger(__name__)


class NothingPlayed(Exception):
    pass


def _name():
    try:
        start = datetime.strptime(db.get_setting('party_started_at', ''), '%Y-%m-%d %H:%M:%S').replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        start = time.time()
    day = datetime.fromtimestamp(start).astimezone()
    return f"Party setlist · {day.strftime('%b')} {day.day}, {day.year}"


def save_tonight():
    """Save or update tonight's playlist. Returns {'url', 'songs', 'updated'}.
    Raises NothingPlayed, or whatever Spotify raised."""
    party = db.get_setting('party_code', '')
    tracks = [p['spotify_track_id'] for p in db.get_plays(party)]
    if not tracks:
        raise NothingPlayed('Nothing has played yet tonight.')
    existing = (db.get_setting('setlist_playlist_id', '')
                if db.get_setting('setlist_party_code', '') == party else '')
    playlist_id, url = sc.save_setlist(
        tracks, _name(), f'Every song played at the party, in order. {len(tracks)} songs.',
        playlist_id=existing or None)
    db.set_setting('setlist_playlist_id', playlist_id)
    db.set_setting('setlist_playlist_url', url)
    db.set_setting('setlist_party_code', party)
    return {'url': url, 'songs': len(tracks), 'updated': bool(existing)}
