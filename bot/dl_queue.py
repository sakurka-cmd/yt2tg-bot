"""Global download queue — serializes all yt-dlp downloads.

Why this exists: download_video() does pre-flight cleanup of stale
*.part / *.temp.* / *.f*.mp4 files in TMP_DIR before every download.
That is only safe if ONE download runs at a time. Concurrent downloads
(several /dl links in a row, scheduler overlapping a manual download)
used to delete each other's in-progress files: the newer download wiped
the older one's .part files, so the older yt-dlp crashed and only the
LAST link survived.

How it works:
- DL_LOCK (asyncio.Lock, FIFO) — guarantees a single active yt-dlp.
  asyncio.Lock grants waiters in arrival order, so the queue order is
  FIFO by first call to download_video().
- Registry (_active + _waiting) — gives /status a live queue view,
  gives dedup (same yt_id requested twice → DUPLICATE sentinel) and
  lets /cancel drop a job that has not started yet.

Sentinels returned by download_video():
  "DUPLICATE"  — this yt_id is already active or waiting (dedup)
  "CANCELLED"  — the job was removed from the queue via /cancel
                 while it waited for the lock

Sources tracked for the UI: "oneoff" (user /dl), "dl_playlist",
"backfill", "scheduler" (subscriptions).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class AlreadyQueuedError(Exception):
    """The same yt_id is already active or waiting in the queue."""


@dataclass
class Job:
    yt_id: str
    url: str = ""
    title: str = ""
    user_id: int = 0
    source: str = ""          # oneoff | dl_playlist | backfill | scheduler
    enqueued_at: float = field(default_factory=time.time)

    def short_title(self, limit: int = 50) -> str:
        t = self.title or self.yt_id
        return t if len(t) <= limit else t[: limit - 3] + "..."


# FIFO lock — waiters are granted in arrival order (asyncio.Lock guarantee).
DL_LOCK: asyncio.Lock = asyncio.Lock()

_active: Job | None = None
_waiting: list[Job] = []


def _find(yt_id: str) -> Job | None:
    if _active and _active.yt_id == yt_id:
        return _active
    for j in _waiting:
        if j.yt_id == yt_id:
            return j
    return None


def find(yt_id: str) -> Job | None:
    """Public alias of _find — lets handlers do an early dedup check."""
    return _find(yt_id)


def enqueue(yt_id: str, url: str = "", title: str = "",
            user_id: int = 0, source: str = "") -> int:
    """Register a download intent. Returns the number of jobs AHEAD of this
    one: 0 = nothing ahead, the download starts immediately; N = N jobs
    will finish first. Raises AlreadyQueuedError if this yt_id is already
    active or waiting."""
    if _find(yt_id):
        raise AlreadyQueuedError(yt_id)
    job = Job(yt_id=yt_id, url=url, title=title or yt_id,
              user_id=user_id, source=source)
    _waiting.append(job)
    return len(_waiting) - 1


def set_active(yt_id: str) -> bool:
    """Move a waiting job to active. Called by download_video AFTER it has
    acquired DL_LOCK. Returns False (and does nothing) if the job is no
    longer waiting — i.e. it was cancelled via /cancel while waiting."""
    global _active
    job = None
    for j in _waiting:
        if j.yt_id == yt_id:
            job = j
            break
    if job is None:
        return False
    _waiting.remove(job)
    _active = job
    return True


def release(yt_id: str) -> None:
    """Mark the active download as finished (called in a finally block)."""
    global _active
    if _active and _active.yt_id == yt_id:
        _active = None


def discard(yt_id: str) -> None:
    """Safety net: remove yt_id from wherever it is (waiting or active).
    Idempotent — safe to call even if the job is already gone."""
    global _active
    for j in list(_waiting):
        if j.yt_id == yt_id:
            _waiting.remove(j)
    if _active and _active.yt_id == yt_id:
        _active = None


def remove_waiting(yt_id: str) -> Job | None:
    """User-initiated cancel of a not-yet-started job. Returns the removed
    job, or None if it wasn't waiting (e.g. it is already active — an
    in-progress yt-dlp subprocess cannot be cancelled from here)."""
    for j in list(_waiting):
        if j.yt_id == yt_id:
            _waiting.remove(j)
            return j
    return None


def remove_for_user(user_id: int) -> list[Job]:
    """Remove ALL pending jobs submitted by this user (for /cancel).
    Returns the list of removed jobs (may be empty)."""
    removed = []
    for j in list(_waiting):
        if j.user_id == user_id:
            _waiting.remove(j)
            removed.append(j)
    return removed


def jobs_ahead(yt_id: str) -> int | None:
    """0 = active now, N = N jobs ahead of this one, None = not in queue."""
    if _active and _active.yt_id == yt_id:
        return 0
    for i, j in enumerate(_waiting):
        if j.yt_id == yt_id:
            return i
    return None


def queue_len() -> int:
    """Number of jobs currently WAITING (the active one not included)."""
    return len(_waiting)


def snapshot() -> dict:
    """Consistent view for /status: active job + FIFO waiting list."""
    return {
        "active": {
            "yt_id": _active.yt_id,
            "title": _active.title,
            "url": _active.url,
            "user_id": _active.user_id,
            "source": _active.source,
            "since": _active.enqueued_at,
        } if _active else None,
        "waiting": [
            {
                "yt_id": j.yt_id,
                "title": j.title,
                "url": j.url,
                "user_id": j.user_id,
                "source": j.source,
            }
            for j in _waiting
        ],
    }
