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

_lock = threading.Lock()
_current_spotify_track_id = None
_last_playing_queue_id = None
_stop_event = threading.Event()


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

        device_id = sc.get_active_device_id()
        pending = db.get_pending_queue()
        if pending:
            next_item = pending[0]
            ok, err = sc.play_track(f"spotify:track:{next_item['spotify_track_id']}", device_id=device_id)
            if ok:
                db.update_queue_status(next_item['id'], 'playing')
            else:
                logger.error(f"Failed to play next track after skip: {err}")
        else:
            sc.pause_playback(device_id=device_id)


def _resume_playlist_if_needed():
    """If nothing is playing but we have pending songs, start the next one."""
    pending = db.get_pending_queue()
    if not pending:
        return
    next_item = pending[0]
    device_id = sc.get_active_device_id()
    ok, err = sc.play_track(f"spotify:track:{next_item['spotify_track_id']}", device_id=device_id)
    if ok:
        db.update_queue_status(next_item['id'], 'playing')
        logger.info(f"Started next song: {next_item['track_name']}")
    else:
        logger.error(f"Failed to start next song: {err}")


def _advance_to_next_pending():
    """Play the next pending song (marking it 'playing') or pause if the queue
    is empty. Updates the module-level tracking globals to match. Mirrors the
    skip/advance path. Returns the started queue item, or None."""
    global _current_spotify_track_id, _last_playing_queue_id
    device_id = sc.get_active_device_id()
    pending = db.get_pending_queue()
    if pending:
        next_item = pending[0]
        ok, err = sc.play_track(f"spotify:track:{next_item['spotify_track_id']}", device_id=device_id)
        if ok:
            db.update_queue_status(next_item['id'], 'playing')
            _current_spotify_track_id = next_item['spotify_track_id']
            _last_playing_queue_id = next_item['id']
            logger.info(f"Advanced to next song: {next_item['track_name']}")
            return next_item
        logger.error(f"Failed to play next track: {err}")
        return None
    sc.pause_playback(device_id=device_id)
    _current_spotify_track_id = None
    _last_playing_queue_id = None
    return None


def background_worker():
    global _current_spotify_track_id, _last_playing_queue_id

    logger.info("Queue manager background thread started.")
    while not _stop_event.is_set():
        try:
            with _lock:
                playback = sc.get_current_playback()

                if not playback or not playback.get('item'):
                    # Playback is idle/stopped.
                    if _current_spotify_track_id:
                        # The track we were tracking ended.
                        db.update_queue_status_by_track(_current_spotify_track_id, 'played')
                        db.mark_track_played(_current_spotify_track_id)
                        _current_spotify_track_id = None
                        _last_playing_queue_id = None
                        _resume_playlist_if_needed()
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

                        # Mark the previously tracked item as played.
                        if _current_spotify_track_id:
                            db.update_queue_status_by_track(_current_spotify_track_id, 'played')
                            db.mark_track_played(_current_spotify_track_id)

                        _current_spotify_track_id = spotify_track_id

                        # Find the matching queue item and mark it playing.
                        matched = None
                        playing_item = db.get_playing_item()
                        if playing_item and playing_item['spotify_track_id'] == spotify_track_id:
                            matched = playing_item
                            _last_playing_queue_id = playing_item['id']
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
                                db.update_queue_status_by_track(_current_spotify_track_id, 'played')
                                db.mark_track_played(_current_spotify_track_id)
                                _advance_to_next_pending()
                            else:
                                playing_item = db.get_playing_item()
                                if playing_item:
                                    _check_downvote_threshold(playing_item)
                        else:
                            if near_end:
                                # Ended / paused-at-end — advance.
                                db.update_queue_status_by_track(_current_spotify_track_id, 'played')
                                db.mark_track_played(_current_spotify_track_id)
                                _advance_to_next_pending()
                            # else: genuinely paused mid-track — do nothing.

        except Exception as e:
            logger.error(f"Background worker error: {e}", exc_info=True)

        _stop_event.wait(5)

    logger.info("Queue manager background thread stopped.")


def start_background_thread():
    t = threading.Thread(target=background_worker, daemon=True, name="QueueManager")
    t.start()
    return t


def stop_background_thread():
    _stop_event.set()


def get_current_spotify_track_id():
    return _current_spotify_track_id
