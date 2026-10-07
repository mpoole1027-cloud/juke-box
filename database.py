import sqlite3
import secrets
import threading
import os

from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.environ.get('JUKEBOX_DB') or os.path.join(os.path.dirname(__file__), 'jukebox.db')
_db_lock = threading.Lock()

# Passwords that must never guard a public party: the old built-in default and
# the .env.example placeholder.
DEFAULT_HOST_PASSWORD = 'party2024'
PLACEHOLDER_HOST_PASSWORDS = (DEFAULT_HOST_PASSWORD, 'choose_a_password')


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _add_sort_order_column(conn):
    """Migration: add sort_order to queue if absent, then back-fill with id."""
    try:
        conn.execute("ALTER TABLE queue ADD COLUMN sort_order INTEGER")
    except Exception:
        pass
    conn.execute("UPDATE queue SET sort_order = id WHERE sort_order IS NULL")


def _add_dedication_column(conn):
    """Migration: add dedication to queue if absent."""
    try:
        conn.execute("ALTER TABLE queue ADD COLUMN dedication TEXT")
    except Exception:
        pass


def _add_costume_photo_column(conn):
    """Migration: add photo (a filename under photos/) to costume_entries if absent."""
    try:
        conn.execute("ALTER TABLE costume_entries ADD COLUMN photo TEXT")
    except Exception:
        pass


def _add_reaction_queue_id_column(conn):
    """Migration: add queue_id to reactions if absent."""
    try:
        conn.execute("ALTER TABLE reactions ADD COLUMN queue_id INTEGER")
    except Exception:
        pass


def init_db():
    with _db_lock:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL,
                    songs_skipped_count INTEGER DEFAULT 0,
                    is_banned INTEGER DEFAULT 0,
                    first_seen TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    spotify_track_id TEXT NOT NULL,
                    track_name TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    album_art TEXT,
                    duration_ms INTEGER,
                    requested_by TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (requested_by) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS downvotes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    UNIQUE(queue_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS reactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    reaction TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS upvotes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    UNIQUE(queue_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS played_tracks (
                    spotify_track_id TEXT PRIMARY KEY,
                    played_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                -- Disposable camera shots. party_code is the party they were
                -- taken at (it rotates per party), shot_id is the phone's own
                -- ID for the shot so a retried upload is stored only once.
                CREATE TABLE IF NOT EXISTS photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    shot_id TEXT NOT NULL,
                    party_code TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    width INTEGER,
                    height INTEGER,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, shot_id)
                );

                -- Costume contest. Both tables are per party_code, so a new
                -- party starts with an empty contest without deleting history.
                CREATE TABLE IF NOT EXISTS costume_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    party_code TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    costume TEXT NOT NULL,
                    photo TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(party_code, user_id)
                );

                -- One vote per guest per party; voting again moves it.
                CREATE TABLE IF NOT EXISTS costume_votes (
                    party_code TEXT NOT NULL,
                    voter_id TEXT NOT NULL,
                    entry_id INTEGER NOT NULL,
                    voted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (party_code, voter_id)
                );
            """)

            _add_sort_order_column(conn)
            _add_dedication_column(conn)
            _add_reaction_queue_id_column(conn)
            _add_costume_photo_column(conn)

            # Insert default settings if not present
            defaults = [
                # .env seeds these once; the host panel owns them after that.
                ('downvote_threshold', os.environ.get('DOWNVOTE_THRESHOLD', '7')),
                ('max_queue_per_user', os.environ.get('MAX_QUEUE_PER_USER', '2')),
                ('skip_ban_threshold', os.environ.get('SKIP_BAN_THRESHOLD', '2')),
                ('party_url', os.environ.get('PARTY_URL', 'http://localhost:5001')),
                ('camera_enabled', '1'),
                ('camera_shots_per_guest', os.environ.get('CAMERA_SHOTS_PER_GUEST', '24')),
                ('costume_phase', 'off'),
            ]
            for key, value in defaults:
                cursor.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, value)
                )
            _ensure_host_password_hash(cursor)
            cursor.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES ('party_code', ?)",
                (generate_party_code(),))
            conn.commit()
        finally:
            conn.close()


def _ensure_host_password_hash(cursor):
    """Keep the host password only as a hash, migrating any plaintext copy.

    HOST_PASSWORD seeds the hash once; after that the host panel owns it. The
    exception is a hash that still matches the built-in default: a real
    HOST_PASSWORD then replaces it, so setting the env var is always enough to
    get off the default.
    """
    def value(key):
        row = cursor.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    stored = value('host_password_hash')
    legacy = value('host_password')
    env_pw = os.environ.get('HOST_PASSWORD', '').strip()

    seed = None
    if stored is None:
        if legacy and legacy != DEFAULT_HOST_PASSWORD:
            seed = legacy
        else:
            seed = env_pw or legacy or DEFAULT_HOST_PASSWORD
    elif env_pw and check_password_hash(stored, DEFAULT_HOST_PASSWORD):
        seed = env_pw

    if seed:
        cursor.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('host_password_hash', ?)",
            (generate_password_hash(seed),))
    cursor.execute("DELETE FROM settings WHERE key = 'host_password'")


# No 0/O or 1/I/L, so a code read off a screen can be typed back reliably.
PARTY_CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
PARTY_CODE_LENGTH = 6


def generate_party_code():
    return ''.join(secrets.choice(PARTY_CODE_ALPHABET) for _ in range(PARTY_CODE_LENGTH))


def rotate_party_code():
    """New code for a new party, so links from earlier parties stop working."""
    code = generate_party_code()
    set_setting('party_code', code)
    return code


def check_host_password(password):
    stored = get_setting('host_password_hash')
    return bool(stored and password) and check_password_hash(stored, password)


def set_host_password(password):
    set_setting('host_password_hash', generate_password_hash(password))


def host_password_is_placeholder():
    return any(check_host_password(pw) for pw in PLACEHOLDER_HOST_PASSWORDS)


def get_setting(key, default=None):
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row['value'] if row else default
        finally:
            conn.close()


def set_setting(key, value):
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value))
            )
            conn.commit()
        finally:
            conn.close()


def get_user(user_id):
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def create_user(user_id, nickname):
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, nickname) VALUES (?, ?)",
                (user_id, nickname)
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def get_banned_users():
    with _db_lock:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT user_id, nickname FROM users WHERE is_banned = 1"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_all_users():
    with _db_lock:
        conn = get_connection()
        try:
            rows = conn.execute("SELECT * FROM users ORDER BY first_seen").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def ban_user(user_id):
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,)
            )
            conn.commit()
        finally:
            conn.close()


def unban_user(user_id):
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,)
            )
            conn.commit()
        finally:
            conn.close()


def increment_skip_count(user_id):
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE users SET songs_skipped_count = songs_skipped_count + 1 WHERE user_id = ?",
                (user_id,)
            )
            conn.commit()
            row = conn.execute(
                "SELECT songs_skipped_count FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return row['songs_skipped_count'] if row else 0
        finally:
            conn.close()


def get_pending_queue():
    with _db_lock:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT q.*, u.user_id, u.nickname, "
                "COALESCE(uc.upvote_count, 0) AS upvote_count "
                "FROM queue q JOIN users u ON q.requested_by = u.user_id "
                "LEFT JOIN (SELECT queue_id, COUNT(*) AS upvote_count FROM upvotes GROUP BY queue_id) uc "
                "ON uc.queue_id = q.id "
                "WHERE q.status = 'pending' "
                "ORDER BY upvote_count DESC, q.sort_order ASC, q.id ASC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_playing_item():
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT q.*, u.nickname FROM queue q JOIN users u ON q.requested_by = u.user_id "
                "WHERE q.status = 'playing' ORDER BY q.added_at DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def add_to_queue(spotify_track_id, track_name, artist, album_art, duration_ms, requested_by, dedication=None):
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) as mx FROM queue WHERE status = 'pending'"
            ).fetchone()
            next_order = (row['mx'] or 0) + 1
            cursor = conn.execute(
                "INSERT INTO queue (spotify_track_id, track_name, artist, album_art, duration_ms, requested_by, sort_order, dedication) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (spotify_track_id, track_name, artist, album_art, duration_ms, requested_by, next_order, dedication)
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


def update_queue_status(queue_id, status):
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE queue SET status = ? WHERE id = ?", (status, queue_id)
            )
            conn.commit()
        finally:
            conn.close()


def update_queue_status_by_track(spotify_track_id, status):
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE queue SET status = ? WHERE spotify_track_id = ? AND status = 'playing'",
                (status, spotify_track_id)
            )
            conn.commit()
        finally:
            conn.close()


def get_queue_item(queue_id):
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM queue WHERE id = ?", (queue_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def count_user_pending(user_id):
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM queue WHERE requested_by = ? AND status = 'pending'",
                (user_id,)
            ).fetchone()
            return row['cnt'] if row else 0
        finally:
            conn.close()


def track_played_tonight(spotify_track_id):
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT 1 FROM played_tracks WHERE spotify_track_id = ?",
                (spotify_track_id,)
            ).fetchone()
            if row:
                return True
            row2 = conn.execute(
                "SELECT 1 FROM queue WHERE spotify_track_id = ? AND status IN ('pending', 'playing')",
                (spotify_track_id,)
            ).fetchone()
            return row2 is not None
        finally:
            conn.close()


def mark_track_played(spotify_track_id):
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO played_tracks (spotify_track_id) VALUES (?)",
                (spotify_track_id,)
            )
            conn.commit()
        finally:
            conn.close()


def add_downvote(queue_id, user_id):
    """Returns True if downvote was added, False if already voted."""
    with _db_lock:
        conn = get_connection()
        try:
            try:
                conn.execute(
                    "INSERT INTO downvotes (queue_id, user_id) VALUES (?, ?)",
                    (queue_id, user_id)
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False
        finally:
            conn.close()


def get_downvote_count(queue_id):
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM downvotes WHERE queue_id = ?",
                (queue_id,)
            ).fetchone()
            return row['cnt'] if row else 0
        finally:
            conn.close()


def user_has_downvoted(queue_id, user_id):
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT 1 FROM downvotes WHERE queue_id = ? AND user_id = ?",
                (queue_id, user_id)
            ).fetchone()
            return row is not None
        finally:
            conn.close()


def add_upvote(queue_id, user_id):
    """Returns True if upvote was added, False if already voted."""
    with _db_lock:
        conn = get_connection()
        try:
            try:
                conn.execute(
                    "INSERT INTO upvotes (queue_id, user_id) VALUES (?, ?)",
                    (queue_id, user_id)
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False
        finally:
            conn.close()


def get_upvote_count(queue_id):
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM upvotes WHERE queue_id = ?",
                (queue_id,)
            ).fetchone()
            return row['cnt'] if row else 0
        finally:
            conn.close()


def get_user_upvoted_ids(user_id):
    """IDs of every queue item this user has upvoted, in one query. /api/status
    used to ask per queued song, which with a long queue and many guests
    polling was most of the server's work."""
    with _db_lock:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT queue_id FROM upvotes WHERE user_id = ?", (user_id,)
            ).fetchall()
            return {r['queue_id'] for r in rows}
        finally:
            conn.close()


def add_reaction(user_id, reaction, queue_id=None):
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO reactions (user_id, reaction, queue_id) VALUES (?, ?, ?)",
                (user_id, reaction, queue_id)
            )
            conn.commit()
        finally:
            conn.close()


def get_reaction_counts(queue_id=None):
    with _db_lock:
        conn = get_connection()
        try:
            if queue_id is not None:
                rows = conn.execute(
                    "SELECT reaction, COUNT(*) as cnt FROM reactions WHERE queue_id = ? GROUP BY reaction",
                    (queue_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT reaction, COUNT(*) as cnt FROM reactions GROUP BY reaction"
                ).fetchall()
            counts = {'fire': 0, 'heart': 0}
            for r in rows:
                counts[r['reaction']] = r['cnt']
            return counts
        finally:
            conn.close()


def clear_reactions():
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM reactions")
            conn.commit()
        finally:
            conn.close()


def start_new_party(clear_users=False):
    """Reset the per-party state so a fresh party starts clean.

    played_tracks backs the 'already played tonight' duplicate check, but
    nothing ever cleared it — so songs from a previous party (or anything the
    app happened to observe while running) stayed permanently un-requestable.
    Same for skip counts and bans, which are meant to be per-night.

    Returns a dict of what was cleared, for reporting back to the host.
    """
    with _db_lock:
        conn = get_connection()
        try:
            played = conn.execute("SELECT COUNT(*) FROM played_tracks").fetchone()[0]
            banned = conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_banned = 1").fetchone()[0]
            queued = conn.execute(
                "SELECT COUNT(*) FROM queue WHERE status IN ('pending','playing')"
            ).fetchone()[0]

            conn.execute("DELETE FROM played_tracks")
            conn.execute("UPDATE users SET songs_skipped_count = 0, is_banned = 0")
            conn.execute(
                "UPDATE queue SET status = 'skipped' WHERE status IN ('pending','playing')")
            if clear_users:
                # Votes and reactions reference queue rows, and queue has an FK
                # to users — with foreign_keys=ON the delete order matters.
                conn.execute("DELETE FROM upvotes")
                conn.execute("DELETE FROM downvotes")
                conn.execute("DELETE FROM reactions")
                conn.execute("DELETE FROM queue")
                conn.execute("DELETE FROM users")
            conn.commit()
            return {'played_cleared': played, 'unbanned': banned, 'queue_cleared': queued}
        finally:
            conn.close()


def clear_pending_queue():
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE queue SET status = 'skipped' WHERE status = 'pending'"
            )
            conn.commit()
        finally:
            conn.close()


def reorder_queue_item(queue_id, direction):
    """Swap sort_order with the adjacent pending item. Returns True if swapped."""
    with _db_lock:
        conn = get_connection()
        try:
            item = conn.execute(
                "SELECT id, sort_order FROM queue WHERE id = ? AND status = 'pending'",
                (queue_id,)
            ).fetchone()
            if not item:
                return False
            cur_order = item['sort_order']
            if direction == 'up':
                swap = conn.execute(
                    "SELECT id, sort_order FROM queue WHERE status = 'pending' AND sort_order < ? "
                    "ORDER BY sort_order DESC LIMIT 1",
                    (cur_order,)
                ).fetchone()
            else:
                swap = conn.execute(
                    "SELECT id, sort_order FROM queue WHERE status = 'pending' AND sort_order > ? "
                    "ORDER BY sort_order ASC LIMIT 1",
                    (cur_order,)
                ).fetchone()
            if not swap:
                return False
            conn.execute("UPDATE queue SET sort_order = ? WHERE id = ?", (swap['sort_order'], queue_id))
            conn.execute("UPDATE queue SET sort_order = ? WHERE id = ?", (cur_order, swap['id']))
            conn.commit()
            return True
        finally:
            conn.close()


def set_user_nickname(user_id, nickname):
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE users SET nickname = ? WHERE user_id = ?",
                (nickname, user_id)
            )
            conn.commit()
        finally:
            conn.close()


def remove_pending_by_user(queue_id, user_id):
    """Mark a pending queue item as skipped if owned by user. Returns True if changed."""
    with _db_lock:
        conn = get_connection()
        try:
            cursor = conn.execute(
                "UPDATE queue SET status = 'skipped' "
                "WHERE id = ? AND requested_by = ? AND status = 'pending'",
                (queue_id, user_id)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def get_party_stats():
    with _db_lock:
        conn = get_connection()
        try:
            songs_played = conn.execute(
                "SELECT COUNT(*) as cnt FROM queue WHERE status = 'played'"
            ).fetchone()['cnt']
            guest_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM users WHERE user_id != 'host'"
            ).fetchone()['cnt']
            total_reactions = conn.execute(
                "SELECT COUNT(*) as cnt FROM reactions"
            ).fetchone()['cnt']
            party_start = conn.execute(
                "SELECT MIN(first_seen) as ps FROM users WHERE user_id != 'host'"
            ).fetchone()['ps']
            return {
                'songs_played': songs_played,
                'guest_count': guest_count,
                'total_reactions': total_reactions,
                'party_start': party_start,
            }
        finally:
            conn.close()


def get_leaderboards():
    with _db_lock:
        conn = get_connection()
        try:
            top_djs = conn.execute(
                "SELECT u.nickname, COUNT(*) as count "
                "FROM queue q JOIN users u ON q.requested_by = u.user_id "
                "WHERE q.status = 'played' AND q.requested_by != 'host' "
                "GROUP BY q.requested_by "
                "ORDER BY count DESC LIMIT 5"
            ).fetchall()

            crowd_favorites = conn.execute(
                "SELECT q.track_name, q.artist, COUNT(r.id) as reaction_count "
                "FROM queue q JOIN reactions r ON r.queue_id = q.id "
                "WHERE q.status IN ('played', 'playing') "
                "GROUP BY q.id "
                "HAVING reaction_count >= 1 "
                "ORDER BY reaction_count DESC LIMIT 5"
            ).fetchall()

            most_skipped = conn.execute(
                "SELECT nickname, songs_skipped_count "
                "FROM users WHERE songs_skipped_count > 0 AND user_id != 'host' "
                "ORDER BY songs_skipped_count DESC LIMIT 5"
            ).fetchall()

            return {
                'top_djs': [dict(r) for r in top_djs],
                'crowd_favorites': [dict(r) for r in crowd_favorites],
                'most_skipped': [dict(r) for r in most_skipped],
            }
        finally:
            conn.close()


def get_recent_reactions(limit=20):
    with _db_lock:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT r.id, r.reaction, r.created_at, u.nickname "
                "FROM reactions r JOIN users u ON r.user_id = u.user_id "
                "ORDER BY r.id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_or_create_host_user():
    """Ensure a special 'host' system user exists for host-queued songs."""
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, nickname) VALUES ('host', 'DJ Host')"
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Disposable camera
# ---------------------------------------------------------------------------
PHOTO_STATUSES = ('pending', 'approved', 'rejected')


def add_photo(user_id, shot_id, party_code, filename, width, height, limit):
    """Record a shot unless the guest is out of film.

    Returns ('ok', id), ('duplicate', id) when this shot was already stored (a
    retried upload), or ('full', None). The count and the insert share one lock
    so two uploads racing for the last shot can't both get it.
    """
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM photos WHERE user_id = ? AND shot_id = ?",
                (user_id, shot_id)).fetchone()
            if row:
                return 'duplicate', row['id']
            used = conn.execute(
                "SELECT COUNT(*) FROM photos WHERE user_id = ? AND party_code = ?",
                (user_id, party_code)).fetchone()[0]
            if used >= limit:
                return 'full', None
            cursor = conn.execute(
                "INSERT INTO photos (user_id, shot_id, party_code, filename, width, height) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, shot_id, party_code, filename, width, height))
            conn.commit()
            return 'ok', cursor.lastrowid
        finally:
            conn.close()


def has_photo_shot(user_id, shot_id):
    with _db_lock:
        conn = get_connection()
        try:
            return conn.execute(
                "SELECT 1 FROM photos WHERE user_id = ? AND shot_id = ?",
                (user_id, shot_id)).fetchone() is not None
        finally:
            conn.close()


def count_user_photos(user_id, party_code):
    with _db_lock:
        conn = get_connection()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM photos WHERE user_id = ? AND party_code = ?",
                (user_id, party_code)).fetchone()[0]
        finally:
            conn.close()


def get_photo(photo_id):
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def list_photos(party_code=None, status=None):
    """Photos oldest first, with the photographer's nickname."""
    sql = ("SELECT p.*, COALESCE(u.nickname, '?') AS nickname "
           "FROM photos p LEFT JOIN users u ON u.user_id = p.user_id WHERE 1=1")
    args = []
    if party_code:
        sql += " AND p.party_code = ?"
        args.append(party_code)
    if status:
        sql += " AND p.status = ?"
        args.append(status)
    sql += " ORDER BY p.created_at, p.id"
    with _db_lock:
        conn = get_connection()
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()


def set_photo_status(photo_ids, status):
    """Set the review status of one or more photos. Returns how many changed."""
    if status not in PHOTO_STATUSES:
        raise ValueError(f'Unknown photo status: {status}')
    ids = [int(i) for i in photo_ids]
    if not ids:
        return 0
    with _db_lock:
        conn = get_connection()
        try:
            cursor = conn.execute(
                f"UPDATE photos SET status = ? WHERE id IN ({','.join('?' * len(ids))})",
                (status, *ids))
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()


def photo_parties():
    """Every party that has photos, newest first, with counts by status."""
    with _db_lock:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT party_code, MIN(created_at) AS first_shot, "
                "MAX(created_at) AS last_shot, COUNT(*) AS total, "
                "SUM(status = 'pending') AS pending, "
                "SUM(status = 'approved') AS approved, "
                "SUM(status = 'rejected') AS rejected "
                "FROM photos GROUP BY party_code ORDER BY MAX(created_at) DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Costume contest
# ---------------------------------------------------------------------------
# off: hidden. open: guests enter and vote. closed: voting over, results shown.
COSTUME_PHASES = ('off', 'open', 'closed')


def save_costume_entry(party_code, user_id, costume, photo=None):
    """Enter the contest, or update an existing entry (votes stay with it).

    photo is the stored filename; None keeps the entry's current photo.
    Returns (entry_id, replaced_photo) so the caller can delete the old file.
    """
    with _db_lock:
        conn = get_connection()
        try:
            old = conn.execute(
                "SELECT photo FROM costume_entries WHERE party_code = ? AND user_id = ?",
                (party_code, user_id)).fetchone()
            conn.execute(
                "INSERT INTO costume_entries (party_code, user_id, costume, photo) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(party_code, user_id) DO UPDATE SET costume = excluded.costume, "
                "photo = COALESCE(excluded.photo, costume_entries.photo)",
                (party_code, user_id, costume, photo))
            conn.commit()
            entry_id = conn.execute(
                "SELECT id FROM costume_entries WHERE party_code = ? AND user_id = ?",
                (party_code, user_id)).fetchone()['id']
            replaced = old['photo'] if old and photo and old['photo'] != photo else None
            return entry_id, replaced
        finally:
            conn.close()


def get_costume_entry(entry_id):
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM costume_entries WHERE id = ?", (entry_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def delete_costume_entry(entry_id):
    """Remove an entry and the votes cast for it. Returns the removed row
    (so the caller can delete its photo file), or None if it didn't exist."""
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM costume_entries WHERE id = ?", (entry_id,)).fetchone()
            conn.execute("DELETE FROM costume_votes WHERE entry_id = ?", (entry_id,))
            conn.execute("DELETE FROM costume_entries WHERE id = ?", (entry_id,))
            conn.commit()
            return dict(row) if row else None
        finally:
            conn.close()


def cast_costume_vote(party_code, voter_id, entry_id):
    """Vote for an entry, replacing the guest's earlier vote.

    Returns 'ok', 'no_entry' (not in tonight's contest) or 'own_entry'.
    """
    with _db_lock:
        conn = get_connection()
        try:
            entry = conn.execute(
                "SELECT user_id FROM costume_entries WHERE id = ? AND party_code = ?",
                (entry_id, party_code)).fetchone()
            if not entry:
                return 'no_entry'
            if entry['user_id'] == voter_id:
                return 'own_entry'
            conn.execute(
                "INSERT OR REPLACE INTO costume_votes (party_code, voter_id, entry_id) "
                "VALUES (?, ?, ?)", (party_code, voter_id, entry_id))
            conn.commit()
            return 'ok'
        finally:
            conn.close()


def get_costume_vote(party_code, voter_id):
    """The entry this guest voted for tonight, or None."""
    with _db_lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT entry_id FROM costume_votes WHERE party_code = ? AND voter_id = ?",
                (party_code, voter_id)).fetchone()
            return row['entry_id'] if row else None
        finally:
            conn.close()


def costume_board(party_code):
    """Tonight's entries with nickname and vote count, most votes first.

    Ties go to whoever entered first.
    """
    with _db_lock:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT e.id, e.user_id, e.costume, e.photo, e.created_at, "
                "COALESCE(u.nickname, '?') AS nickname, COUNT(v.voter_id) AS votes "
                "FROM costume_entries e "
                "LEFT JOIN users u ON u.user_id = e.user_id "
                "LEFT JOIN costume_votes v ON v.entry_id = e.id AND v.party_code = e.party_code "
                "WHERE e.party_code = ? "
                "GROUP BY e.id ORDER BY votes DESC, e.created_at, e.id",
                (party_code,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
