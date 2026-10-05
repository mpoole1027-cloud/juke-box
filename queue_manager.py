import threading
import time
import logging
import database as db
import spotify_client as sc

logger = logging.getLogger(__name__)

# A track whose progress is within this many ms of its duration is treated as
# finished. This lets us distinguish "paused at end / ended" from "paused
# mid-track" without relying on the overloaded is_playing flag alone.
END_THRESHOLD_MS = 3000

# How often the worker reads Spotify. It is also the freshness of what every
# page shows, since request threads read the worker's snapshot (see
# get_cached_playback) instead of calling Spotify themselves.
POLL_SECONDS = 3

_lock = threading.Lock()
_current_spotify_track_id = None
_last_playing_queue_id = None
# True only when the currently tracked track came from the guest queue. Fallback
# playlist tracks are False, which keeps them out of played_tracks.
_current_is_ours = False
_stop_event = threading.Event()

# Idle standby. A forgotten server once controlled this account's playback for
# 44 days; after IDLE_SHUTDOWN_HOURS with no guest or host action the worker
# stops touching playback until someone explicitly resumes the party. It never
# pauses on the way out — standing down means leaving playback exactly as it is.
_last_activity = time.monotonic()
_standing_down = False
# Tracks whether we're mid-outage, so the warning logs once, not every tick.
_last_playback_error = False

# Latest known Spotify playback payload as (monotonic time, payload or None).
# Without it every guest, TV and party-lights poll was its own Spotify call:
# ~5 calls/s with a dozen phones, enough to draw 429s from Spotify.
_snapshot = None
_snapshot_lock = threading.Lock()
_fetch_lock = threading.Lock()


def _store_snapshot(playback):
    global _snapshot
    with _snapshot_lock:
        _snapshot = (time.monotonic(), playback)


def invalidate_playback_cache():
    """Call after changing what Spotify plays, so pages don't show the old song."""
    global _snapshot
    with _snapshot_lock:
        _snapshot = None


def get_cached_playback(max_age=POLL_SECONDS + 1):
    """What Spotify is playing, at most max_age seconds old.

    Normally the worker keeps this fresh. When it isn't polling (standing down,
    or between ticks after an invalidation), the first request thread through
    refreshes it and concurrent ones wait for and reuse that one result.
    """
    snap = _snapshot
    if snap and time.monotonic() - snap[0] <= max_age:
        return snap[1]
    with _fetch_lock:
        snap = _snapshot
        if snap and time.monotonic() - snap[0] <= max_age:
            return snap[1]
        playback = sc.get_current_playback()
        _store_snapshot(playback)
        return playback


def note_activity():
    """Record guest/host activity. Any real action keeps the party alive."""
    global _last_activity
    _last_activity = time.monotonic()


def is_standing_down():
    return _standing_down


def idle_seconds():
    """Seconds of inactivity before standby. 0 disables the feature."""
    return _get_int_setting('idle_shutdown_hours', 6) * 3600


def resume_party():
    """Host explicitly brings the party back after standby."""
    global _standing_down
    was = _standing_down
    _standing_down = False
    note_activity()
    if was:
        logger.info("Party resumed by host — controlling playback again.")
    return was


def _get_int_setting(key, default):
    val = db.get_setting(key, str(default))
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _check_downvote_threshold(playing_item):
    """Check if the currently playing item has exceeded the downvote threshold. Skip if so."""
    if not playing_item:
        return
    threshold = _get_int_setting('downvote_threshold', 7)
    skip_ban_threshold = _get_int_setting('skip_ban_threshold', 2)
    count = db.get_downvote_count(playing_item['id'])
    if count >= threshold:
        logger.info(f"Downvote threshold reached for queue item {playing_item['id']}, skipping.")
        db.update_queue_status(playing_item['id'], 'skipped')
        db.mark_track_played(playing_item['spotify_track_id'])
        requester = playing_item['requested_by']
        skip_count = db.increment_skip_count(requester)
        if skip_count >= skip_ban_threshold:
            db.ban_user(requester)
            logger.info(f"User {requester} banned after {skip_count} skipped songs.")

        _advance_to_next_pending()


def get_party_device_id():
    """Resolve the playback device, honouring the host's pinned choice."""
    return sc.get_active_device_id(
        preferred_device_id=db.get_setting('preferred_device_id') or None)


def get_fallback_playlist_id():
    """The host-configured fallback playlist, or None if not set."""
    return db.get_setting('fallback_playlist_id') or None


def _start_fallback(device_id=None):
    """Start the host's fallback playlist so a lull in queueing doesn't mean
    silence. No-op (returning False) if no fallback is configured.

    We never pause here: pause_playback hits the account-wide Spotify API and
    stops music on every device the host owns, which is exactly the runaway
    behaviour this feature exists to remove.
    """
    global _current_spotify_track_id, _last_playing_queue_id, _current_is_ours

    playlist_id = get_fallback_playlist_id()
    if not playlist_id:
        logger.info("Queue empty and no fallback playlist configured — leaving playback alone.")
        return False

    # Already rolling through the fallback playlist? Let Spotify continue on its
    # own rather than restarting it every poll.
    try:
        playback = sc.get_current_playback()
        if (playback and playback.get('is_playing')
                and sc.is_playing_our_playlist(playlist_id)):
            return True
    except Exception as e:
        logger.error(f"Could not confirm fallback playback state: {e}")

    if device_id is None:
        device_id = get_party_device_id()

    meta = sc.get_playlist_meta(playlist_id)
    if not meta:
        logger.error(f"Fallback playlist {playlist_id} could not be read — skipping fallback.")
        return False
    if not meta['track_count']:
        logger.error(f"Fallback playlist '{meta['name']}' is empty — nothing to fall back to.")
        return False

    ok, err = sc.start_playlist_playback(
        playlist_id, device_id=device_id,
        shuffle=True, random_offset=True, track_count=meta['track_count'],
    )
    if ok:
        invalidate_playback_cache()
        logger.info(f"Queue empty — started fallback playlist ({meta['name']}).")
        # Whatever plays now is not one of ours, so it must not be recorded as
        # played (see _finish_current_track).
        _current_spotify_track_id = None
        _last_playing_queue_id = None
        _current_is_ours = False
        return True

    logger.error(f"Failed to start fallback playlist: {err}")
    return False


def _finish_current_track():
    """Retire the track we were tracking.

    Only tracks that actually matched a queue item get written to played_tracks
    — that table backs the 'already played tonight' duplicate check, so
    recording fallback filler there would silently blacklist the whole playlist
    from being requested by guests.
    """
    global _current_spotify_track_id, _last_playing_queue_id, _current_is_ours

    if _current_spotify_track_id and _current_is_ours:
        db.update_queue_status_by_track(_current_spotify_track_id, 'played')
        db.mark_track_played(_current_spotify_track_id)

    _current_spotify_track_id = None
    _last_playing_queue_id = None
    _current_is_ours = False


def _advance_to_next_pending():
    """Play the next pending song (marking it 'playing'), or fall back to the
    host's playlist when the queue is empty. Updates the module-level tracking
    globals to match. The single source of truth for 'what plays next' — the
    skip, downvote and end-of-track paths all route through here.

    Returns the started queue item, or None if we fell back / did nothing.
    """
    global _current_spotify_track_id, _last_playing_queue_id, _current_is_ours
    device_id = get_party_device_id()
    pending = db.get_pending_queue()
    if pending:
        next_item = pending[0]
        ok, err = sc.play_track(f"spotify:track:{next_item['spotify_track_id']}", device_id=device_id)
        if ok:
            invalidate_playback_cache()
            db.update_queue_status(next_item['id'], 'playing')
            _current_spotify_track_id = next_item['spotify_track_id']
            _last_playing_queue_id = next_item['id']
            _current_is_ours = True
            logger.info(f"Advanced to next song: {next_item['track_name']}")
            return next_item
        logger.error(f"Failed to play next track: {err}")
        return None

    _start_fallback(device_id=device_id)
    return None


def advance_to_next_pending():
    """Thread-safe entry point for request handlers.

    The background worker already holds _lock for its whole iteration and calls
    the private form directly; _lock is not reentrant, so routes must come
    through here instead to avoid both deadlocking and racing the worker on the
    tracking globals.
    """
    with _lock:
        return _advance_to_next_pending()


def background_worker():
    global _current_spotify_track_id, _last_playing_queue_id, _current_is_ours
    global _standing_down, _last_playback_error

    logger.info("Queue manager background thread started.")
    while not _stop_event.is_set():
        try:
            # Standby check runs outside _lock so request threads are never
            # blocked behind an idle worker.
            limit = idle_seconds()
            if limit and not _standing_down and time.monotonic() - _last_activity > limit:
                with _lock:
                    _standing_down = True
                    _current_spotify_track_id = None
                    _last_playing_queue_id = None
                    _current_is_ours = False
                logger.info(
                    "No activity for %.1fh — standing down. Playback left as-is; "
                    "resume from the host panel to restart the party.",
                    limit / 3600.0)

            if _standing_down:
                _stop_event.wait(5)
                continue

            # Fetched outside _lock: it's a network round-trip, and holding
            # the lock across it stalls every request thread.
            state = sc.get_playback_state()
            if state['status'] != sc.PLAYBACK_ERROR:
                _store_snapshot(state['playback'])

            if state['status'] == sc.PLAYBACK_ERROR:
                # We could not find out what Spotify is doing. Say nothing,
                # change nothing, try again next tick. Treating this as "the
                # song ended" is what used to skip a guest's song and burn it
                # into played_tracks on a momentary network drop.
                if not _last_playback_error:
                    logger.warning("Spotify state unavailable (%s) — holding position.",
                                   state.get('reason', 'unknown'))
                _last_playback_error = True
                _stop_event.wait(POLL_SECONDS)
                continue

            if _last_playback_error:
                logger.info("Spotify reachable again — resuming.")
            _last_playback_error = False

            with _lock:
                playback = state['playback']

                if not playback or not playback.get('item'):
                    # Playback is idle/stopped. Only act if a track we were
                    # tracking just ended — if the host stopped Spotify while
                    # nothing of ours was playing, leave it stopped rather than
                    # forcing the fallback playlist back on.
                    if _current_spotify_track_id:
                        _finish_current_track()
                        _advance_to_next_pending()
                else:
                    spotify_track = playback['item']
                    spotify_track_id = spotify_track.get('id')
                    is_playing = playback.get('is_playing', False)
                    progress_ms = playback.get('progress_ms') or 0
                    duration_ms = spotify_track.get('duration_ms') or 0
                    near_end = bool(duration_ms) and progress_ms >= duration_ms - END_THRESHOLD_MS

                    if spotify_track_id != _current_spotify_track_id:
                        # (a) Genuine song change.
                        logger.info(f"Song changed to: {spotify_track.get('name', 'Unknown')}")

                        # Retire the previously tracked item (guarded so
                        # fallback tracks never land in played_tracks).
                        if _current_spotify_track_id:
                            _finish_current_track()

                        _current_spotify_track_id = spotify_track_id

                        # Find the matching queue item and mark it playing.
                        matched = None
                        playing_item = db.get_playing_item()
                        if playing_item and playing_item['spotify_track_id'] == spotify_track_id:
                            matched = playing_item
                            _last_playing_queue_id = playing_item['id']
                            _current_is_ours = True
                        else:
                            conn = db.get_connection()
                            try:
                                row = conn.execute(
                                    "SELECT * FROM queue WHERE spotify_track_id = ? "
                                    "AND status IN ('pending', 'playing') LIMIT 1",
                                    (spotify_track_id,)
                                ).fetchone()
                                if row:
                                    matched = dict(row)
                            finally:
                                conn.close()
                            if matched:
                                db.update_queue_status(matched['id'], 'playing')
                                _last_playing_queue_id = matched['id']
                                _current_is_ours = True

                        # Autoplay/radio hijack: Spotify moved on to a track that
                        # is not one of ours while we still have songs waiting.
                        if matched is None and db.get_pending_queue():
                            logger.info("Autoplay/radio detected — overriding with next pending song.")
                            _advance_to_next_pending()
                    else:
                        # (b) Same track still current.
                        if is_playing:
                            if near_end:
                                # Track finished — advance.
                                _finish_current_track()
                                _advance_to_next_pending()
                            else:
                                playing_item = db.get_playing_item()
                                if playing_item:
                                    _check_downvote_threshold(playing_item)
                        else:
                            if near_end:
                                # Ended / paused-at-end — advance.
                                _finish_current_track()
                                _advance_to_next_pending()
                            # else: genuinely paused mid-track — do nothing.

        except Exception as e:
            logger.error(f"Background worker error: {e}", exc_info=True)

        _stop_event.wait(POLL_SECONDS)

    logger.info("Queue manager background thread stopped.")


def start_background_thread():
    t = threading.Thread(target=background_worker, daemon=True, name="QueueManager")
    t.start()
    return t


def stop_background_thread():
    _stop_event.set()


def get_current_spotify_track_id():
    return _current_spotify_track_id
