"""SQLite database for yt2tg bot."""

import json
import aiosqlite
import os
from datetime import datetime

from bot.config import DATABASE_URL

_db: aiosqlite.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    channel_title TEXT DEFAULT '',
    playlist_id TEXT NOT NULL,
    quality TEXT DEFAULT '720',
    check_interval INTEGER DEFAULT 3600,
    last_check TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS processed_videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    youtube_id TEXT NOT NULL UNIQUE,
    subscription_id INTEGER,
    title TEXT DEFAULT '',
    quality TEXT DEFAULT '720',
    videohost_id TEXT DEFAULT '',
    uploaded_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS oneoff_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER DEFAULT 0,
    url TEXT NOT NULL,
    youtube_id TEXT DEFAULT '',
    playlist_id TEXT DEFAULT '',
    quality TEXT DEFAULT '720',
    status TEXT DEFAULT 'pending',
    title TEXT DEFAULT '',
    videohost_id TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    completed_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS fsm_states (
    user_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS version_state (
    repo TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (repo, key)
);

CREATE INDEX IF NOT EXISTS idx_processed_ytid ON processed_videos(youtube_id);
CREATE INDEX IF NOT EXISTS idx_subs_active ON subscriptions(active, channel_id);
"""


async def init_db() -> aiosqlite.Connection:
    global _db
    os.makedirs(os.path.dirname(DATABASE_URL) or ".", exist_ok=True)
    _db = await aiosqlite.connect(DATABASE_URL)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA busy_timeout=5000")
    await _db.executescript(SCHEMA)
    # Additive migrations (idempotent — ignore "duplicate column" errors)
    try:
        await _db.execute("ALTER TABLE subscriptions ADD COLUMN youtube_channel_id TEXT DEFAULT ''")
    except Exception:
        pass  # Column already exists
    try:
        await _db.execute("ALTER TABLE subscriptions ADD COLUMN white_filter TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        await _db.execute("ALTER TABLE subscriptions ADD COLUMN black_filter TEXT DEFAULT ''")
    except Exception:
        pass
    await _db.commit()
    return _db


def get_db() -> aiosqlite.Connection:
    assert _db is not None, "Database not initialized"
    return _db


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None


# ── FSM helpers ──────────────────────────────────────────────

async def save_fsm_state(user_id: int, state, data: dict):
    db = get_db()
    state_val = state.value if hasattr(state, "value") else str(state)
    await db.execute(
        "INSERT OR REPLACE INTO fsm_states (user_id, state, data, updated_at) VALUES (?, ?, ?, datetime('now'))",
        (user_id, state_val, json.dumps(data, ensure_ascii=False)),
    )
    await db.commit()


async def get_fsm_state(user_id: int) -> tuple[str, dict]:
    db = get_db()
    cur = await db.execute("SELECT state, data FROM fsm_states WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    if row:
        return row["state"], json.loads(row["data"])
    return "", {}


async def clear_fsm_state(user_id: int):
    db = get_db()
    await db.execute("DELETE FROM fsm_states WHERE user_id=?", (user_id,))
    await db.commit()


# ── Subscriptions CRUD ────────────────────────────────────────

async def add_subscription(channel_id: str, channel_title: str,
                           playlist_id: str, quality: str = "720",
                           check_interval: int = 3600,
                           youtube_channel_id: str = "") -> int:
    db = get_db()
    # Dedup by channel_id (handle) OR by youtube_channel_id (UCxxxxx) if set
    existing = None
    if youtube_channel_id:
        cur = await db.execute(
            "SELECT id FROM subscriptions WHERE youtube_channel_id=?",
            (youtube_channel_id,),
        )
        existing = await cur.fetchone()
    if not existing:
        cur = await db.execute("SELECT id FROM subscriptions WHERE channel_id=?", (channel_id,))
        existing = await cur.fetchone()
    if existing:
        await db.execute(
            "UPDATE subscriptions SET playlist_id=?, quality=?, active=1, channel_title=?, "
            "youtube_channel_id=? WHERE id=?",
            (playlist_id, quality, channel_title, youtube_channel_id, existing["id"]),
        )
        await db.commit()
        return existing["id"]
    cur = await db.execute(
        "INSERT INTO subscriptions (channel_id, channel_title, playlist_id, quality, check_interval, youtube_channel_id) "
        "VALUES (?,?,?,?,?,?)",
        (channel_id, channel_title, playlist_id, quality, check_interval, youtube_channel_id),
    )
    await db.commit()
    return cur.lastrowid


async def find_subscription_by_youtube_channel_id(youtube_channel_id: str) -> dict | None:
    """Find an active subscription by its canonical YouTube channel_id (UCxxxxx).

    Used by /dl to discover that a video belongs to a channel the user is
    already subscribed to, so we can reuse the existing playlist instead of
    creating a new one.
    """
    if not youtube_channel_id:
        return None
    db = get_db()
    cur = await db.execute(
        "SELECT * FROM subscriptions WHERE youtube_channel_id=? AND active=1 LIMIT 1",
        (youtube_channel_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_subscriptions() -> list[dict]:
    db = get_db()
    cur = await db.execute("SELECT * FROM subscriptions ORDER BY created_at DESC")
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_subscription(sub_id: int) -> dict | None:
    db = get_db()
    cur = await db.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def delete_subscription(sub_id: int):
    db = get_db()
    await db.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
    await db.commit()


async def update_subscription_quality(sub_id: int, quality: str):
    db = get_db()
    await db.execute("UPDATE subscriptions SET quality=? WHERE id=?", (quality, sub_id))
    await db.commit()


async def update_subscription_filters(sub_id: int, white_filter: str, black_filter: str):
    """Update the white/black list filters for a subscription.

    Both are comma-separated lists of substrings. Empty string = filter disabled.
    Stored as-is (lowercased on write for case-insensitive matching).
    """
    db = get_db()
    await db.execute(
        "UPDATE subscriptions SET white_filter=?, black_filter=? WHERE id=?",
        (white_filter.lower().strip(), black_filter.lower().strip(), sub_id),
    )
    await db.commit()


async def toggle_subscription(sub_id: int, active: bool):
    db = get_db()
    await db.execute("UPDATE subscriptions SET active=? WHERE id=?", (int(active), sub_id))
    await db.commit()


async def update_last_check(sub_id: int):
    db = get_db()
    await db.execute(
        "UPDATE subscriptions SET last_check=datetime('now') WHERE id=?", (sub_id,)
    )
    await db.commit()


# ── Processed videos ─────────────────────────────────────────

async def is_video_processed(youtube_id: str) -> bool:
    db = get_db()
    cur = await db.execute("SELECT 1 FROM processed_videos WHERE youtube_id=?", (youtube_id,))
    return await cur.fetchone() is not None


async def get_processed_video(youtube_id: str) -> dict | None:
    """Return the full processed_videos row for a given YouTube ID, or None."""
    db = get_db()
    cur = await db.execute("SELECT * FROM processed_videos WHERE youtube_id=?", (youtube_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def mark_video_processed(youtube_id: str, sub_id: int | None,
                                title: str, quality: str, videohost_id: str):
    db = get_db()
    # Use ON CONFLICT DO UPDATE — preserves user_deleted flag if record already exists.
    # This prevents re-upload of user-deleted videos: if user_deleted=1 was set earlier,
    # a failed upload attempt won't reset it to 0.
    await db.execute(
        """INSERT INTO processed_videos (youtube_id, subscription_id, title, quality, videohost_id)
        VALUES (?,?,?,?,?)
        ON CONFLICT(youtube_id) DO UPDATE SET
            subscription_id=excluded.subscription_id,
            title=excluded.title,
            quality=excluded.quality,
            videohost_id=excluded.videohost_id""",
        (youtube_id, sub_id, title, quality, videohost_id),
    )
    await db.commit()


async def unmark_video_processed(youtube_id: str):
    """Remove the processed_videos record for a given YouTube ID.

    Called when re-uploading a video that had an empty videohost_id (bug-induced).
    """
    db = get_db()
    await db.execute("DELETE FROM processed_videos WHERE youtube_id=?", (youtube_id,))
    await db.commit()


async def mark_video_user_deleted(youtube_id: str):
    """Mark a video as user-deleted on VideoHost.

    When video_exists() returns 404 for a previously uploaded video, it means
    the user deleted it via the VideoHost UI. We keep the record (to remember
    the youtube_id) but set user_deleted=1 so the bot never re-uploads it.
    User can still force re-upload via /backfill.
    """
    db = get_db()
    await db.execute(
        "UPDATE processed_videos SET user_deleted=1 WHERE youtube_id=?",
        (youtube_id,),
    )
    await db.commit()


async def list_recent_videos(hours: int = 24) -> list[dict]:
    """Return videos uploaded in the last N hours, newest first."""
    db = get_db()
    cur = await db.execute(
        "SELECT * FROM processed_videos "
        "WHERE uploaded_at >= datetime('now', ?) "
        "AND videohost_id != '' "
        "ORDER BY uploaded_at DESC",
        (f"-{hours} hours",),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── One-off tasks ────────────────────────────────────────────

async def create_oneoff_task(user_id: int, url: str, playlist_id: str = "",
                              quality: str = "720") -> int:
    db = get_db()
    cur = await db.execute(
        "INSERT INTO oneoff_tasks (user_id, url, playlist_id, quality, status) VALUES (?,?,?,?,?)",
        (user_id, url, playlist_id, quality, "pending"),
    )
    await db.commit()
    return cur.lastrowid


async def update_task_status(task_id: int, status: str, **kwargs):
    db = get_db()
    sets = ["status=?"]
    vals = [status]
    for k, v in kwargs.items():
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(task_id)
    await db.execute(
        f"UPDATE oneoff_tasks SET {', '.join(sets)}, completed_at=datetime('now') WHERE id=?",
        vals,
    )
    await db.commit()


async def get_pending_task(user_id: int) -> dict | None:
    db = get_db()
    cur = await db.execute(
        "SELECT * FROM oneoff_tasks WHERE user_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None