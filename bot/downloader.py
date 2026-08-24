"""yt-dlp wrapper for downloading YouTube videos."""

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

from bot.config import QUALITIES, DEFAULT_QUALITY, MAX_FILE_SIZE, TMP_DIR

logger = logging.getLogger(__name__)

YOUTUBE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?"
    r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)"
    r"([a-zA-Z0-9_-]{11})"
)

CHANNEL_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?youtube\.com/(?:channel/|@|c/)([a-zA-Z0-9_.-]+)"
)

YOUTUBE_PLAYLIST_RE = re.compile(
    r"(?:https?://)?(?:www\.)?youtube\.com/.*[?&]list=([a-zA-Z0-9_-]+)"
)

YTDLP_BIN = shutil.which("yt-dlp") or "yt-dlp"

# Global status for /status command
current_status: dict = {
    "task": "",
    "url": "",
    "title": "",
    "progress": "",
    "error": "",
}


def extract_video_id(url: str) -> str | None:
    m = YOUTUBE_URL_RE.search(url)
    return m.group(1) if m else None


def extract_channel_id(url: str) -> str | None:
    m = CHANNEL_URL_RE.search(url)
    return m.group(1) if m else None


def get_format_string(quality: str) -> str:
    fmt = QUALITIES.get(quality, QUALITIES[DEFAULT_QUALITY])
    return fmt


async def get_video_info(url: str) -> dict | None:
    cmd = [YTDLP_BIN, "--dump-json", "--no-download", "--no-playlist", "--no-warnings", url]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            logger.error("yt-dlp info failed: %s", stderr.decode(errors="replace")[:500])
            return None
        import json
        info = json.loads(stdout.decode())
        return {
            "id": info.get("id", ""),
            "title": info.get("title", ""),
            "duration": info.get("duration", 0),
            # channel = display name (e.g. "Асафьев. Жизнь" or "Russian Car Crash channel")
            "channel": info.get("channel", ""),
            # uploader_id = full handle with @ (e.g. "@russiancrashchannel6171")
            # This is the canonical handle on YouTube — use this (without @) for
            # playlist naming so /dl and /subscribe produce the same name.
            "uploader_id": info.get("uploader_id", ""),
            # uploader = display name (NOT handle) for many YouTube videos — kept for backward compat only
            "uploader": info.get("uploader", "") or info.get("channel", ""),
            "uploader_url": info.get("uploader_url", "") or info.get("channel_url", ""),
            # channel_id = UCxxxxxxx (unique YouTube channel ID, never changes)
            # This is the reliable key for matching /dl → subscription
            "channel_id": info.get("channel_id", ""),
            "description": info.get("description", "")[:500],
            "thumbnail": info.get("thumbnail", ""),
            "filesize_approx": info.get("filesize_approx", 0),
            # yt-dlp returns upload_date as YYYYMMDD string
            "upload_date": info.get("upload_date", ""),
        }
    except asyncio.TimeoutError:
        logger.error("yt-dlp info timeout for %s", url)
        return None
    except Exception as e:
        logger.error("yt-dlp info error: %s", e)
        return None


def clean_handle(s: str) -> str:
    """Extract a clean handle (without @, without URL prefix) from various
    yt-dlp fields: uploader_id (@russiancrashchannel6171) or uploader_url
    (https://www.youtube.com/@russiancrashchannel6171).

    Returns the bare handle, e.g. 'russiancrashchannel6171'.
    Returns empty string if input is empty.
    """
    if not s:
        return ""
    s = s.strip()
    if s.startswith("@"):
        return s[1:]
    if s.startswith("http"):
        from urllib.parse import urlparse
        path = urlparse(s).path.strip("/")
        if path.startswith("@"):
            return path[1:].split("/")[0]
    return s


async def get_channel_info(url: str) -> dict | None:
    """Get channel info via yt-dlp (preferred) or RSS feed (fallback).

    Returns: {
        "id": <channel_id UCxxxxx or handle>,
        "channel_id": <UCxxxxx>,           # unique YouTube channel ID
        "channel_handle": <handle without @>,  # e.g. "russiancrashchannel6171"
        "title": <display name>,            # e.g. "Russian Car Crash channel"
        "link": <channel URL>,
    }
    """
    # Try yt-dlp first — it returns the canonical channel_id (UCxxxxx)
    # and uploader_id (the actual handle YouTube uses, which may differ
    # from the handle in the URL the user pasted).
    cmd = [
        YTDLP_BIN, "--dump-json", "--no-download",
        "--playlist-items", "1",  # only fetch first video to save time
        "--no-warnings",
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0:
            import json
            info = json.loads(stdout.decode())
            channel_id = info.get("channel_id", "")
            uploader_id = info.get("uploader_id", "")
            handle = clean_handle(uploader_id) or clean_handle(info.get("uploader_url", ""))
            display_name = info.get("channel", "") or handle or channel_id
            return {
                "id": channel_id or handle or extract_channel_id(url) or "",
                "channel_id": channel_id,
                "channel_handle": handle,
                "title": display_name,
                "link": info.get("uploader_url", url),
            }
        else:
            logger.warning("yt-dlp channel info failed, falling back to RSS: %s",
                          stderr.decode(errors="replace")[:300])
    except asyncio.TimeoutError:
        logger.warning("yt-dlp channel info timeout, falling back to RSS")
    except Exception as e:
        logger.warning("yt-dlp channel info error, falling back to RSS: %s", e)

    # Fallback to RSS (used when yt-dlp can't resolve the channel)
    return await _get_channel_info_rss(url)


async def _get_channel_info_rss(url: str) -> dict | None:
    """Fallback: get channel info via RSS feed (less accurate, no UCxxxxx)."""
    channel_id = extract_channel_id(url)
    if not channel_id:
        return None
    import feedparser
    if channel_id.startswith("@") or channel_id.startswith("UC"):
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    else:
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id=@{channel_id}"
    try:
        feed = feedparser.parse(feed_url)
        if not feed.entries:
            feed_url = f"https://www.youtube.com/feeds/videos.xml?search_query={channel_id}"
            feed = feedparser.parse(feed_url)
        if feed.feed:
            return {
                "id": channel_id,
                "channel_id": "",  # RSS doesn't reliably give UCxxxxx
                "channel_handle": clean_handle(channel_id) or channel_id,
                "title": feed.feed.get("title", channel_id),
                "link": feed.feed.get("link", url),
            }
    except Exception as e:
        logger.error("Channel info RSS error: %s", e)
    return None


async def download_video(url: str, quality: str = DEFAULT_QUALITY) -> str | None:
    """Download video, return path. Caller must delete. Returns 'TOO_LARGE' if too big.

    Only the video is downloaded here — subtitles are fetched separately via
    download_subtitles() so that a 429 on the subtitle endpoint doesn't abort
    the video download (yt-dlp exits non-zero on subtitle 429 even though the
    video itself downloaded fine, and on some videos it bails BEFORE starting
    the video download, leaving the user with nothing).
    """
    global current_status
    os.makedirs(TMP_DIR, exist_ok=True)

    # ── Disk space pre-flight + stale file cleanup ────────────────────────
    # VPS1 root filesystem is small (~10 GB). yt-dlp downloads video + audio
    # streams separately then merges them, which means at peak we have:
    #   <id>.f<format_id>.mp4   (~video size)
    #   <id>.f<format_id>.m4a   (~audio size)
    #   <id>.temp.mp4           (merge output, ~video+audio)
    #   <id>.mp4                (final, replaces temp)
    # For a 1 GB 720p video that's ~3 GB peak. If /tmp fills up mid-download
    # yt-dlp fails with "[Errno 28] No space left on device" AND leaves the
    # partial files behind — they accumulate across failed downloads and
    # eventually make the problem permanent.
    #
    # Cleanup strategy:
    # 1. Before each download, remove ALL stale *.part / *.temp.* / *.f*.mp4
    #    files. The bot is single-threaded — only one download runs at a
    #    time — so ANY .part file is stale. (Previously only files older
    #    than 1 hour were touched, but a .part file can grow to 4+ GB in
    #    under an hour on long videos, filling the disk before cleanup
    #    kicks in.)
    # 2. Check free space on TMP_DIR's filesystem. If less than 4 GB free,
    #    refuse to start the download and return a clear error rather than
    #    letting yt-dlp fail mid-way with a cryptic errno 28.
    try:
        for f in Path(TMP_DIR).iterdir():
            if not f.is_file():
                continue
            name = f.name
            # Stale partial/merge files from yt-dlp — clean ALL of them
            # (bot is single-threaded, no concurrent downloads)
            if (name.endswith(".part") or ".temp." in name or
                (".f" in name and (name.endswith(".mp4") or name.endswith(".m4a") or
                                    name.endswith(".webm")))):
                try:
                    f.unlink()
                    logger.info("Pre-flight cleanup: removed %s", name)
                except OSError:
                    pass
        # Free space check
        stat = os.statvfs(TMP_DIR)
        free_bytes = stat.f_bavail * stat.f_frsize
        free_gb = free_bytes / (1024 ** 3)
        MIN_FREE_GB = 1.5
        if free_gb < MIN_FREE_GB:
            msg = (f"Insufficient disk space: {free_gb:.1f} GB free on {TMP_DIR} "
                   f"(need at least {MIN_FREE_GB} GB). Cleaned stale files but "
                   f"still not enough. Manual cleanup required.")
            logger.error(msg)
            current_status["error"] = msg[:200]
            current_status["task"] = ""
            current_status["progress"] = ""
            return "NO_SPACE"
    except Exception as e:
        logger.warning("Pre-download cleanup failed (continuing): %s", e)

    fmt = get_format_string(quality)
    output_template = os.path.join(TMP_DIR, "%(id)s.%(ext)s")

    cmd = [
        YTDLP_BIN, "-f", fmt, "--merge-output-format", "mp4",
        "-o", output_template, "--no-playlist", "--no-cache-dir",
        "--newline", "--progress",
        # Suppress non-fatal warnings (Python 3.10 deprecation notice, etc.)
        # so they don't leak into current_status["error"] on successful downloads.
        # Real errors still set non-zero returncode and get reported separately.
        "--no-warnings",
        # Impersonate Chrome at the TLS layer (curl_cffi backend). YouTube
        # returns HTTP 429 to plain requests on some videos — impersonation
        # makes the requests look like a real browser and largely avoids
        # the 429 path. curl_cffi must be installed in the system Python.
        "--impersonate", "chrome",
        # Be tolerant of transient network errors — YouTube throttles
        # aggressively and a single failed fragment shouldn't kill the whole
        # download.
        "--retries", "10",
        "--fragment-retries", "10",
        url,
    ]
    if MAX_FILE_SIZE and MAX_FILE_SIZE != "0":
        cmd.extend(["--max-filesize", MAX_FILE_SIZE])

    yt_id = extract_video_id(url) or "unknown"
    current_status.update({"task": "download", "url": url, "title": yt_id, "progress": "0%", "error": ""})
    logger.info("Downloading: %s (quality: %s)", url, quality)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            # Parse progress: [download]  45.2% of ~150.00MiB
            if "[download]" in text and "%" in text:
                pct = text.split("%")[0].split()[-1] if "%" in text else ""
                current_status["progress"] = pct

        await asyncio.wait_for(proc.wait(), timeout=600)

        # Check if the video file made it to disk despite a non-zero exit.
        # yt-dlp exits with code 1 when subtitle download fails (e.g. YouTube
        # returns HTTP 429 for some subtitle languages) even though the video
        # itself downloaded successfully. We treat the download as successful
        # if the video file exists and is non-trivial in size.
        video_path: str | None = None
        for ext in ("mp4", "mkv", "webm", "flv"):
            path = os.path.join(TMP_DIR, f"{yt_id}.{ext}")
            if os.path.exists(path) and os.path.getsize(path) > 1024:
                video_path = path
                break
        if not video_path:
            # Fallback: find any recent file in TMP_DIR matching this yt_id
            files = sorted(Path(TMP_DIR).glob(f"{yt_id}*"))
            for f in files:
                if f.stat().st_size > 1024 and f.suffix.lower() in (".mp4", ".mkv", ".webm", ".flv"):
                    video_path = str(f)
                    break

        if proc.returncode != 0:
            err = (await proc.stderr.read()).decode(errors="replace")
            if video_path:
                # Video downloaded successfully, but something non-fatal failed
                # (rare now that subtitles are out of the main command — but
                # yt-dlp can still exit non-zero on a marginal audio fragment
                # merge etc.). Log the warning and return the video path.
                logger.warning("yt-dlp returned non-zero but video file exists, treating as success: %s", err[:300])
                return video_path
            # Strip the Python 3.10 deprecation warning from the error — it
            # always appears first in yt-dlp's stderr and pushes the actual
            # error out of the [:200] window, making admin reports show
            # "Deprecated Feature..." instead of the real problem.
            err_clean = err
            for strip_prefix in (
                "Deprecated Feature: Support for Python version 3.10 has been deprecated. "
                "Please update to Python 3.11 or above\n",
                "Deprecated Feature: Support for Python version 3.10 has been deprecated.\n",
            ):
                if err_clean.startswith(strip_prefix):
                    err_clean = err_clean[len(strip_prefix):]
                    break
            # Also strip any remaining "Deprecated Feature" line(s)
            err_lines = [l for l in err_clean.splitlines() if not l.startswith("Deprecated Feature")]
            err_clean = "\n".join(err_lines).strip()

            logger.error("yt-dlp download failed: %s", err_clean[:500])
            current_status["error"] = err_clean[:200]
            # Clear the stale "active task" so /status doesn't lie to the user.
            current_status["task"] = ""
            current_status["progress"] = ""
            if "File is larger than max-filesize" in err_clean:
                return "TOO_LARGE"
            # Detect "No space left on device" — transient disk pressure.
            if "no space left on device" in err_clean.lower() or "errno 28" in err_clean.lower():
                logger.error("Disk full during download of %s — not marking as processed, will retry next cycle", yt_id)
                return "NO_SPACE"
            # Detect YouTube anti-bot challenge — "Sign in to confirm you're
            # not a bot". This is NOT permanent (YouTube turns it on/off
            # periodically) and NOT a simple transient error. Return a
            # dedicated sentinel so the scheduler can report it clearly and
            # retry on the next cycle without marking as processed.
            if "sign in to confirm" in err_clean.lower() or "not a bot" in err_clean.lower():
                logger.warning("YouTube anti-bot challenge for %s — will retry next cycle", yt_id)
                return "AUTH_REQUIRED"
            # Detect permanent failures — the video will NEVER be downloadable
            err_lower = err_clean.lower()
            PERMANENT_PATTERNS = [
                "this live event will begin",      # upcoming live / premiere
                "live event has not started",
                "premieres in",                     # scheduled premiere
                "video unavailable",                # deleted / private
                "private video",
                "members-only",
                "this video is private",
                "this video is not available",
                "login required to confirm your age",
            ]
            for pat in PERMANENT_PATTERNS:
                if pat in err_lower:
                    logger.warning("Permanent failure for %s (pattern: %r) — won't retry",
                                   yt_id, pat)
                    return "PERMANENT_FAIL"
            return None

        if not video_path:
            logger.error("Downloaded file not found for %s (returncode=0)", yt_id)
            current_status["task"] = ""
            current_status["progress"] = ""
            return None
        return video_path
    except asyncio.TimeoutError:
        logger.error("yt-dlp download timeout for %s", url)
        current_status["error"] = "Timeout"
        current_status["task"] = ""
        current_status["progress"] = ""
        return None
    except Exception as e:
        logger.error("yt-dlp download error: %s", e)
        current_status["error"] = str(e)
        current_status["task"] = ""
        current_status["progress"] = ""
        return None


def find_subtitle_path(yt_id: str) -> str | None:
    """Find the VTT subtitle sidecar file downloaded by yt-dlp for this video.

    yt-dlp writes subtitles as `<yt_id>.<lang>.vtt` (e.g. `dQw4w9WgXcQ.ru.vtt`).
    Returns the path to the Russian subtitle if available, else the first .vtt
    file matching this yt_id. Returns None if no subtitle was downloaded.
    """
    if not yt_id:
        return None
    candidates = sorted(Path(TMP_DIR).glob(f"{yt_id}.ru.vtt"))
    if candidates:
        return str(candidates[0])
    candidates = sorted(Path(TMP_DIR).glob(f"{yt_id}.*.vtt"))
    if candidates:
        return str(candidates[0])
    return None


FFPROBE_BIN = shutil.which("ffprobe") or "ffprobe"


def probe_duration(file_path: str) -> float | None:
    """Get video duration in seconds via ffprobe. Returns None on any failure.

    Used by the scheduler (which uses RSS, no yt-dlp info dict) so the
    duration can still be passed to upload_video() and shown on the APK
    thumbnail badge. Fast — runs locally on the downloaded file, no network.

    NOTE: uses '-show_format' (OLD syntax) instead of '-show_entries
    format=duration' (new syntax). On VPS1, ffprobe 4.4.2 has a bug where
    '-show_entries format=duration' fails with "Option not found" when
    called from a Python subprocess (but works from bash). The old
    '-show_format' syntax is reliable across all ffprobe versions.
    We parse the full output and extract the 'duration=' line manually.
    """
    if not file_path or not os.path.exists(file_path):
        logger.warning("probe_duration: file does not exist: %s", file_path)
        return None
    import subprocess
    try:
        out = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-show_format",
             "-of", "default=nw=0:nk=0", file_path],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout:
            # Output contains many key=value lines. Find 'duration=X.XXXXXX'
            for line in out.stdout.splitlines():
                line = line.strip()
                if line.startswith("duration="):
                    val = line.split("=", 1)[1].strip()
                    if val and val != "N/A":
                        dur = float(val)
                        logger.info("probe_duration: %s -> %.2f sec", os.path.basename(file_path), dur)
                        return dur
            logger.warning("probe_duration: no duration= line in ffprobe output for %s", file_path)
        else:
            logger.warning("probe_duration: ffprobe rc=%d stdout=%r stderr=%r for %s",
                           out.returncode, out.stdout[:100], out.stderr[:100], file_path)
    except Exception as e:
        logger.warning("ffprobe duration failed for %s: %s", file_path, e)
    return None


async def download_subtitles(url: str, yt_id: str) -> str | None:
    """Download auto-generated Russian subtitles as a VTT sidecar file.

    This is split from download_video() because yt-dlp, when asked to download
    both video and subtitles in one invocation, sometimes bails out on the
    subtitle HTTP 429 BEFORE starting the video download — leaving the user
    with nothing. By running subs as a separate `--skip-download` command,
    a 429 here only means "no subtitles" rather than "no video either".

    Returns the path to the .vtt file (via find_subtitle_path), or None on
    any failure. Never raises — subtitles are always optional.
    """
    if not yt_id:
        return None
    os.makedirs(TMP_DIR, exist_ok=True)
    output_template = os.path.join(TMP_DIR, "%(id)s.%(ext)s")
    cmd = [
        YTDLP_BIN,
        "--write-auto-sub", "--sub-lang", "ru",
        "--sub-format", "vtt", "--convert-subs", "vtt",
        "--skip-download",          # only subtitles, no video
        "--no-playlist", "--no-warnings", "--no-cache-dir",
        "--impersonate", "chrome",  # same impersonation as the video command
        "--retries", "5",
        "-o", output_template,
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.wait(), timeout=120)
    except asyncio.TimeoutError:
        logger.warning("Subtitle download timeout for %s", yt_id)
    except Exception as e:
        logger.warning("Subtitle download error for %s: %s", yt_id, e)
    # yt-dlp returns non-zero if YouTube returns 429 for subtitles — that's
    # fine, we just won't have subs for this video. The .vtt file may still
    # have been partially written in some cases, so check for it.
    sub = find_subtitle_path(yt_id)
    if sub:
        logger.info("Subtitles downloaded for %s: %s", yt_id, sub)
    else:
        logger.info("No subtitles for %s (429 or not available)", yt_id)
    return sub


async def list_channel_videos(channel_url: str, max_count: int = 200) -> list[dict]:
    """Fetch list of videos on a YouTube channel/playlist.

    Tries RSS first (for channels with UC ID), then falls back to yt-dlp --flat-playlist.
    """
    # Try RSS for channel URLs with UC ID
    from urllib.parse import urlparse
    parsed = urlparse(channel_url)
    path_parts = [p for p in parsed.path.split("/") if p]
    channel_id = ""
    if "channel" in path_parts:
        idx = path_parts.index("channel")
        if idx + 1 < len(path_parts):
            channel_id = path_parts[idx + 1]
    if channel_id and channel_id.startswith("UC"):
        rss_videos = await _list_channel_videos_rss(channel_id)
        if rss_videos:
            if max_count > len(rss_videos):
                flat_videos = await _list_channel_videos_flat(channel_url, max_count)
                rss_ids = {v["id"] for v in rss_videos}
                for v in flat_videos:
                    if v["id"] not in rss_ids:
                        rss_videos.append(v)
                        rss_ids.add(v["id"])
            return rss_videos[:max_count]
    # Fallback: flat-playlist only
    return await _list_channel_videos_flat(channel_url, max_count)


async def _list_channel_videos_rss(channel_id: str) -> list[dict]:
    """Fetch videos via YouTube RSS feed (up to 15 most recent WITH dates)."""
    import feedparser
    if not channel_id.startswith("UC"):
        return []
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        feed = feedparser.parse(feed_url)
        if not feed.entries:
            return []
        results = []
        for entry in feed.entries:
            yt_url = entry.get("link", "")
            yt_id = extract_video_id(yt_url)
            if not yt_id:
                continue
            published = entry.get("published", "")
            upload_date = ""
            if published:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    upload_date = dt.strftime("%Y%m%d")
                except Exception:
                    pass
            results.append({
                "id": yt_id,
                "title": entry.get("title", "Untitled"),
                "upload_date": upload_date,
                "url": yt_url or f"https://www.youtube.com/watch?v={yt_id}",
            })
        return results
    except Exception as e:
        logger.warning("RSS fetch failed for %s: %s", channel_id, e)
        return []


async def _list_channel_videos_flat(channel_url: str, max_count: int = 200) -> list[dict]:
    """Fetch videos via yt-dlp --flat-playlist."""
    import json as _json
    cmd = [
        YTDLP_BIN, "--flat-playlist", "--dump-json",
        "--playlist-items", f"1-{max_count}",
        "--no-warnings",
        channel_url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:500]
            logger.error("yt-dlp flat-playlist failed for %s: %s", channel_url, err)
            return []
        results = []
        for line in stdout.decode().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = _json.loads(line)
            except Exception:
                continue
            vid = item.get("id", "")
            if not vid:
                continue
            results.append({
                "id": vid,
                "title": item.get("title", "Untitled"),
                "upload_date": item.get("upload_date") or "",
                "url": item.get("url") or f"https://www.youtube.com/watch?v={vid}",
            })
        logger.info("flat-playlist returned %d videos for %s", len(results), channel_url)
        return results
    except asyncio.TimeoutError:
        logger.error("yt-dlp flat-playlist timeout for %s", channel_url)
        return []
    except Exception as e:
        logger.error("yt-dlp flat-playlist error: %s", e)
        return []


def cleanup_file(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
            logger.info("Cleaned up: %s", path)
    except OSError as e:
        logger.warning("Cleanup failed for %s: %s", path, e)

def extract_playlist_id(url: str) -> str | None:
    m = YOUTUBE_PLAYLIST_RE.search(url)
    return m.group(1) if m else None


async def get_youtube_playlist_info(playlist_url: str) -> dict | None:
    """Get YouTube playlist title + list of videos using yt-dlp --flat-playlist.

    Returns: {"title": "...", "videos": [{"id":..., "title":..., "upload_date":"YYYYMMDD", "url":...}, ...]}
    or None on failure.
    """
    import json as _json
    # First get the playlist title via --dump-json (first item has playlist metadata)
    cmd_title = [
        YTDLP_BIN, "--flat-playlist", "--dump-json",
        "--playlist-items", "0",
        "--no-warnings",
        playlist_url,
    ]
    playlist_title = "YouTube Playlist"
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_title, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        for line in stdout.decode().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = _json.loads(line)
                # Playlist title is in the "playlist" field or we get it from --print
                if "playlist" in item:
                    playlist_title = item["playlist"]
                    break
            except Exception:
                pass
    except Exception:
        pass

    # Get all video entries
    videos = await list_channel_videos(playlist_url, max_count=200)
    if not videos:
        # list_channel_videos tries RSS first which won't work for playlists.
        # Use flat-playlist directly.
        videos = await _list_channel_videos_flat(playlist_url, max_count=200)

    return {
        "title": playlist_title,
        "videos": videos or [],
    }


async def search_youtube(query: str, max_results: int = 20) -> list[dict]:
    """Search YouTube via yt-dlp and return top results with metadata + thumbnail URL.

    Uses yt-dlp's 'ytsearchN:' pseudo-URL: yt-dlp "ytsearch20:query" --flat-playlist
    returns N videos with id, title, duration, upload_date, channel, view_count.

    Returns: list of dicts, each:
      {
        "id": "<11-char YouTube video ID>",
        "title": "...",
        "url": "https://www.youtube.com/watch?v=...",
        "duration": 123,        # seconds (0 if unknown)
        "upload_date": "YYYYMMDD",
        "channel": "Channel Name",
        "view_count": 12345,
        "thumbnail": "https://i.ytimg.com/vi/<id>/mqdefault.jpg",
      }
    """
    import json as _json
    # ytsearchN: returns flat-playlist of search results
    # --flat-playlist avoids downloading each video (fast)
    cmd = [
        YTDLP_BIN, "--flat-playlist", "--dump-json",
        "--no-warnings", "--no-playlist",
        f"ytsearch{max_results}:{query}",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:500]
            logger.error("yt-dlp search failed for %r: %s", query, err)
            return []
        results = []
        for line in stdout.decode().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = _json.loads(line)
            except Exception:
                continue
            vid = item.get("id", "")
            if not vid or len(vid) != 11:
                continue
            # duration in flat-playlist may be missing or 0 — best-effort
            duration = item.get("duration") or 0
            results.append({
                "id": vid,
                "title": item.get("title", "Untitled"),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "duration": duration,
                "upload_date": item.get("upload_date", ""),
                "channel": item.get("channel") or item.get("uploader") or "",
                "view_count": item.get("view_count") or 0,
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            })
        logger.info("yt-dlp search for %r returned %d results", query, len(results))
        return results
    except asyncio.TimeoutError:
        logger.error("yt-dlp search timeout for %r", query)
        return []
    except Exception as e:
        logger.error("yt-dlp search error: %s", e)
        return []
