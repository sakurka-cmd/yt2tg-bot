"""YouTube channel RSS checker for subscriptions."""

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
import feedparser

from bot import database as db
from bot.downloader import (
    extract_video_id, download_video, download_subtitles,
    cleanup_file, current_status, find_subtitle_path, probe_duration,
    YTDLP_BIN,
)
from bot.uploader import upload_video, sort_playlist, video_exists
from bot.config import CHECK_INTERVAL, MAX_FILE_SIZE
from bot.filters import should_download

logger = logging.getLogger(__name__)

# YouTube RSS is extremely flaky — the same channel can return 200, 404,
# or 500 in rapid succession (confirmed by diagnosis: Agit_Prop was 404
# then 200, AsafevLife was 200 then 404, all within minutes). So we
# retry on ALL errors (404, 500, empty 200) — none are truly permanent.
# Without retry we'd miss new videos whenever YouTube has a hiccup.
#
# UPDATE 2026-08-07: YouTube DELETED the /feeds/videos.xml?channel_id=
# endpoint entirely (returns 404 for all channels). RSS is now only a
# first-attempt fallback; the real feed source is yt-dlp --flat-playlist
# (see get_channel_feed_ytdlp). Keep the RSS retry logic because YouTube
# sometimes brings endpoints back, and the search_query variant still
# works for some channels.
FEED_RETRY_ATTEMPTS = 3
FEED_RETRY_BACKOFF_SEC = 10


async def _fetch_feed_body(session: aiohttp.ClientSession, url: str) -> tuple[int, str | None]:
    """Fetch URL body via aiohttp. Returns (status_code, body_text or None)."""
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        body = await resp.text() if resp.status == 200 else None
        return resp.status, body


async def get_channel_feed_ytdlp(channel_id: str) -> feedparser.FeedParserDict | None:
    """Build a feedparser-like dict from yt-dlp --flat-playlist output.

    Used as a fallback when YouTube RSS returns 404 (which is now the
    default — YouTube deleted the /feeds/videos.xml endpoint in Aug 2026).
    yt-dlp --flat-playlist fetches the channel's video tab and returns
    one JSON-per-line with id, title, upload_date. We synthesise a
    FeedParserDict-shaped object so the rest of process_subscription
    can use the same `feed.entries` iteration logic.

    channel_id may be UCxxxxx, @handle, or bare handle.
    """
    if channel_id.startswith("@"):
        channel_url = f"https://www.youtube.com/{channel_id}"
    elif channel_id.startswith("UC"):
        channel_url = f"https://www.youtube.com/channel/{channel_id}"
    else:
        channel_url = f"https://www.youtube.com/@{channel_id}"

    cmd = [
        YTDLP_BIN, "--flat-playlist", "--dump-json",
        "--playlist-items", "1-15",  # last 15 videos — same as RSS
        "--no-warnings",
        # approximate_date: tell yt-dlp to extract approximate upload dates
        # from the channel page's video positioning. Without this, flat-playlist
        # returns upload_date=NONE for most videos, which breaks the age filter
        # in process_subscription (old videos get downloaded because we can't
        # tell they're old). With approximate_date, yt-dlp fills in a best-guess
        # YYYYMMDD for most (not all — Shorts tabs still return NONE).
        "--extractor-args", "youtubetab:approximate_date",
        channel_url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:300]
            logger.warning("yt-dlp flat-playlist failed for %s: %s", channel_id, err)
            return None

        import json as _json
        entries = []
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
                # yt-dlp can return tab URLs (not video IDs) for shorts/live tabs
                continue
            title = item.get("title", "Untitled")
            upload_date = item.get("upload_date", "")  # YYYYMMDD
            # Synthesise a published_parsed tuple (struct_time-like) for the
            # age check in process_subscription. feedparser returns time.struct_time.
            published_parsed = None
            if upload_date and len(upload_date) == 8:
                try:
                    y, m, d = int(upload_date[:4]), int(upload_date[4:6]), int(upload_date[6:8])
                    published_parsed = (y, m, d, 0, 0, 0, 0, 0, 0)
                except Exception:
                    pass
            # Use a dict (not namedtuple) so entry.get("link") / entry.get("title")
            # works identically to feedparser.FeedParserDict.
            entries.append({
                "link": f"https://www.youtube.com/watch?v={vid}",
                "title": title,
                "published_parsed": published_parsed,
            })
        if not entries:
            logger.warning("yt-dlp flat-playlist returned no video entries for %s", channel_id)
            return None
        # Wrap in a feed-like object with .entries (dict with 'entries' key
        # would also work, but a small namedtuple mirrors feedparser's API).
        from collections import namedtuple
        Feed = namedtuple("Feed", ["entries"])
        logger.info("yt-dlp fallback for %s: %d entries", channel_id, len(entries))
        return Feed(entries=entries)
    except asyncio.TimeoutError:
        logger.warning("yt-dlp flat-playlist timeout for %s", channel_id)
        return None
    except Exception as e:
        logger.warning("yt-dlp flat-playlist error for %s: %s", channel_id, e)
        return None


async def get_channel_feed(channel_id: str) -> feedparser.FeedParserDict | None:
    """Try YouTube RSS first, fall back to yt-dlp --flat-playlist on 404.

    YouTube deleted the /feeds/videos.xml?channel_id= endpoint in Aug 2026,
    so RSS will almost always 404 now. The yt-dlp fallback uses the channel
    page's video tab and produces the same shape of feed.entries.
    """
    if channel_id.startswith("@") or channel_id.startswith("UC"):
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    else:
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id=@{channel_id}"

    rss_failed = False
    async with aiohttp.ClientSession() as session:
        for attempt in range(1, FEED_RETRY_ATTEMPTS + 1):
            try:
                status, body = await _fetch_feed_body(session, feed_url)

                if status != 200:
                    if attempt < FEED_RETRY_ATTEMPTS:
                        logger.info("RSS HTTP %d for %s (attempt %d/%d), retrying in %ds",
                                    status, channel_id, attempt, FEED_RETRY_ATTEMPTS, FEED_RETRY_BACKOFF_SEC)
                        await asyncio.sleep(FEED_RETRY_BACKOFF_SEC)
                        continue
                    else:
                        logger.warning("RSS HTTP %d for channel %s after %d attempts — falling back to yt-dlp",
                                       status, channel_id, FEED_RETRY_ATTEMPTS)
                        rss_failed = True
                        break

                # 200 OK — parse the body
                feed = feedparser.parse(body)
                if feed.entries:
                    return feed

                # 200-empty — retry (transient YouTube cache refresh).
                if attempt < FEED_RETRY_ATTEMPTS:
                    logger.info("Empty RSS feed for %s (attempt %d/%d), retrying in %ds",
                                channel_id, attempt, FEED_RETRY_ATTEMPTS, FEED_RETRY_BACKOFF_SEC)
                    await asyncio.sleep(FEED_RETRY_BACKOFF_SEC)
            except Exception as e:
                logger.error("RSS fetch error for %s (attempt %d/%d): %s",
                             channel_id, attempt, FEED_RETRY_ATTEMPTS, e)
                if attempt < FEED_RETRY_ATTEMPTS:
                    await asyncio.sleep(FEED_RETRY_BACKOFF_SEC)
                else:
                    rss_failed = True
                    break

    # Fallback: yt-dlp --flat-playlist. Used when RSS 404s (now the default
    # since YouTube deleted the endpoint) or when all RSS attempts failed.
    if rss_failed:
        logger.info("Falling back to yt-dlp flat-playlist for %s", channel_id)
        return await get_channel_feed_ytdlp(channel_id)

    logger.warning("No feed entries for channel %s after %d attempts — trying yt-dlp fallback",
                   channel_id, FEED_RETRY_ATTEMPTS)
    return await get_channel_feed_ytdlp(channel_id)


async def process_subscription(sub: dict) -> tuple[int, list[str], list[dict]]:
    """Returns (uploaded_count, uploaded_titles, failed_downloads).

    failed_downloads is a list of dicts: {"title", "reason", "url", "channel"}.
    The caller includes this in the admin report so the operator can see
    which videos failed and why, with a direct YouTube link to retry.

    NOTE: ALL return paths must return a 3-tuple — the caller does
    `count, titles, failures = await process_subscription(sub)`.
    """
    channel_id = sub["channel_id"]
    # Prefer youtube_channel_id (UCxxxxx) for RSS — handles don't work with RSS feed
    yt_channel_id = sub.get("youtube_channel_id", "") or ""
    feed_id = yt_channel_id if yt_channel_id else channel_id
    playlist_id = sub["playlist_id"]
    quality = sub["quality"]
    sub_id = sub["id"]
    white_filter = sub.get("white_filter", "") or ""
    black_filter = sub.get("black_filter", "") or ""

    feed = await get_channel_feed(feed_id)
    if not feed:
        logger.warning("No feed entries for channel %s (feed_id=%s)", channel_id, feed_id)
        return 0, [], []

    uploaded = 0
    uploaded_titles: list[str] = []
    failed_downloads: list[dict] = []  # {"title","reason","url","channel"}
    for entry in feed.entries:
        yt_url = entry.get("link", "")
        yt_id = extract_video_id(yt_url)
        if not yt_id:
            continue

        # Skip if already processed — but verify the video still exists on VideoHost.
        # If it was deleted from VideoHost (by user OR by cleanup), drop the cached
        # record so we can re-upload. This matches the /dl logic in handlers.py.
        existing = await db.get_processed_video(yt_id)
        if existing:
            # If user explicitly deleted the video on VideoHost, never re-upload
            if existing.get("user_deleted", 0):
                continue
            vh_id_old = existing.get("videohost_id", "") or ""
            if not vh_id_old:
                # Processed but never actually uploaded (empty videohost_id).
                # This used to call unmark_video_processed() + set existing=None
                # which caused an infinite loop: unmark → re-check → too old →
                # mark_video_processed(empty videohost_id) → next cycle → unmark
                # → repeat forever. Each cycle produced hundreds of log lines
                # and blocked real downloads.
                #
                # Now we mark as user-deleted and skip permanently. If the user
                # wants this video, /dl will fetch it fresh.
                logger.info("Video %s has empty videohost_id — marking as user-deleted (won't re-upload)",
                            yt_id)
                await db.mark_video_user_deleted(yt_id)
                continue
            elif not await video_exists(vh_id_old):
                # Video was deleted on VideoHost (by user OR cleanup) — mark as
                # user-deleted, never re-upload. The previous version set
                # `existing = None` here, which caused the `if existing: continue`
                # check below to be skipped → the video was re-downloaded and
                # re-uploaded. With `continue` here we skip this entry entirely.
                logger.info("Video %s (%s) was deleted on VideoHost — marking as user-deleted (won't re-upload)",
                            yt_id, vh_id_old)
                await db.mark_video_user_deleted(yt_id)
                continue
        if existing:
            continue

        title = entry.get("title", "Untitled")

        # Apply white/black list filters (skip silently — don't mark as processed,
        # so if user later changes filters, the video can still be downloaded).
        if not should_download(title, white_filter, black_filter):
            logger.info("Filter skip: %s (%s) — white=%r black=%r",
                        title, yt_id, white_filter, black_filter)
            continue

        # Skip videos older than 7 days. If we can't determine the upload date
        # (yt-dlp flat-playlist returns upload_date=NONE for some tabs like
        # Shorts), skip the video rather than risk downloading a years-old
        # video. The RSS feed (when it works) always has dates; the yt-dlp
        # fallback with approximate_date fills in most, but not all.
        published = entry.get("published_parsed")
        if published:
            pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
            age = (datetime.now(tz=timezone.utc) - pub_dt).days
            if age > 7:
                logger.info("Skipping old video: %s (%d days)", title, age)
                await db.mark_video_processed(yt_id, sub_id, title, quality, "")
                continue
        else:
            # No upload date available — can't verify age. Skip to avoid
            # downloading old videos that happened to appear in the channel
            # tab listing. The video will be retried next cycle; if it's
            # genuinely new, it'll eventually get a date.
            logger.info("Skipping video with unknown date: %s (%s)", title, yt_id)
            continue

        logger.info("New video from %s: %s (%s)", channel_id, title, yt_id)

        # Get publication date from RSS entry (used for playlist sorting)
        published_iso = ""
        if published:
            try:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                published_iso = pub_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass

        # ── Download with simple retry queue ─────────────────────────────────
        # The previous code did a single download_video() call. On transient
        # errors (yt-dlp 403, network hiccup, YouTube throttling) the video
        # was silently skipped and only retried on the NEXT subscription
        # cycle (1 hour later). With 16 subscriptions and 15 videos each,
        # a 30-second YouTube hiccup could miss dozens of videos.
        #
        # Now we retry up to DOWNLOAD_RETRY_ATTEMPTS times with backoff.
        # Only transient errors (download_video returns None) are retried —
        # PERMANENT_FAIL and TOO_LARGE are honoured immediately. The retry
        # is sequential within process_subscription (no parallelism) to
        # avoid hammering YouTube and triggering more 429s.
        DOWNLOAD_RETRY_ATTEMPTS = 2
        DOWNLOAD_RETRY_BACKOFF_SEC = 15
        file_path = None
        for attempt in range(1, DOWNLOAD_RETRY_ATTEMPTS + 1):
            file_path = await download_video(yt_url, quality)
            # Success or non-retryable: stop retrying
            if file_path and file_path not in ("TOO_LARGE", "PERMANENT_FAIL", "NO_SPACE", "AUTH_REQUIRED"):
                break
            if file_path in ("TOO_LARGE", "PERMANENT_FAIL", "NO_SPACE", "AUTH_REQUIRED"):
                # NO_SPACE is not retried here — retrying immediately would
                # just hit the same disk-full condition. Skip this cycle,
                # retry on the next subscription run (1 hour later) after
                # the operator (or the pre-flight cleanup) frees up space.
                break
            # Transient None — retry with backoff
            if attempt < DOWNLOAD_RETRY_ATTEMPTS:
                logger.info("Transient download failure for %s (attempt %d/%d), retrying in %ds",
                            yt_id, attempt, DOWNLOAD_RETRY_ATTEMPTS, DOWNLOAD_RETRY_BACKOFF_SEC)
                await asyncio.sleep(DOWNLOAD_RETRY_BACKOFF_SEC)

        if not file_path or file_path == "TOO_LARGE" or file_path == "PERMANENT_FAIL" or file_path == "NO_SPACE" or file_path == "AUTH_REQUIRED":
            # Build a human-readable failure reason for the admin report.
            # Each failure includes the original YouTube URL so the admin
            # can click and retry manually, or understand what was skipped.
            if file_path == "TOO_LARGE":
                reason = f"слишком большое видео (>{MAX_FILE_SIZE})"
                logger.warning("Video too large, skipping: %s", title)
                await db.mark_video_processed(yt_id, sub_id, title, quality, "")
            elif file_path == "PERMANENT_FAIL":
                err_detail = current_status.get("error", "")[:120]
                reason = f"недоступно ({err_detail})" if err_detail else "live/premiere/private"
                logger.warning("Permanent failure for %s — marking as user-deleted: %s", yt_id, title)
                await db.mark_video_processed(yt_id, sub_id, title, quality, "")
                await db.mark_video_user_deleted(yt_id)
            elif file_path == "NO_SPACE":
                reason = "нет места на диске сервера"
                logger.error("Disk full — skipping %s (%s) this cycle, will retry next cycle", yt_id, title)
            elif file_path == "AUTH_REQUIRED":
                reason = "YouTube анти-бот защита (требуется вход / cookies)"
                logger.warning("YouTube anti-bot challenge for %s — will retry next cycle", yt_id)
            else:
                # Plain None — transient error (network, 403, 429, etc.)
                err_detail = current_status.get("error", "")[:120]
                reason = f"сетевая ошибка ({err_detail})" if err_detail else "неизвестная ошибка"
                logger.warning("Transient failure for %s (%s): %s", yt_id, title, reason)
            failed_downloads.append({
                "title": title,
                "reason": reason,
                "url": yt_url,
                "channel": channel_id,
            })
            continue

        # YouTube thumbnail URL — always available for public videos
        yt_thumb = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg" if yt_id else ""

        # Probe duration locally via ffprobe (RSS doesn't give duration).
        # Lets the APK show the duration badge on the thumbnail.
        duration = probe_duration(file_path)

        # Subtitles are downloaded separately so a 429 on the subtitle endpoint
        # doesn't block the video download (see download_subtitles docstring).
        sub_path = await download_subtitles(yt_url, yt_id) if yt_id else None

        result = await upload_video(
            file_path, title, playlist_id or None,
            published_at=published_iso,
            thumbnail_url=yt_thumb,
            youtube_id=yt_id,
            subtitle_path=sub_path or "",
            duration=duration,
        )
        cleanup_file(file_path)
        if sub_path:
            cleanup_file(sub_path)

        if result:
            vh_id = result.get("id", "")
            await db.mark_video_processed(yt_id, sub_id, title, quality, vh_id)
            uploaded += 1
            uploaded_titles.append(title)
            logger.info("Uploaded: %s → %s", title, vh_id)
            # Re-sort playlist chronologically
            if playlist_id:
                await sort_playlist(playlist_id)
        else:
            logger.error("Failed to upload: %s", title)

    await db.update_last_check(sub_id)
    return uploaded, uploaded_titles, failed_downloads


async def scheduler_loop(bot=None, admin_chat_id: int = 0):
    logger.info("Scheduler started (interval: %ds)", CHECK_INTERVAL)
    while True:
        try:
            subs = await db.list_subscriptions()
            active_subs = [s for s in subs if s["active"]]
            logger.info("Checking %d active subscriptions", len(active_subs))

            total_uploaded = 0
            all_uploaded_titles: list[str] = []
            all_failures: list[dict] = []  # collected across all subscriptions
            for sub in active_subs:
                sub_id_log = sub["id"]
                channel_log = sub.get("channel_id", "?")
                logger.info(">>> Processing subscription %s (channel=%s)", sub_id_log, channel_log)
                try:
                    count, titles, failures = await process_subscription(sub)
                    total_uploaded += count
                    all_uploaded_titles.extend(titles)
                    all_failures.extend(failures)
                    logger.info("<<< Subscription %s done: %d uploaded, %d failed",
                                sub_id_log, count, len(failures))
                except Exception as e:
                    logger.error("Error processing subscription %s: %s", sub_id_log, e)

            # Send admin report — always send if there are failures, even if
            # nothing was uploaded. The admin needs to know what went wrong.
            if admin_chat_id and bot and (total_uploaded > 0 or all_failures):
                try:
                    lines = [f"📡 Проверка подписок: загружено {total_uploaded} новых видео.", ""]
                    if all_uploaded_titles:
                        for i, title in enumerate(all_uploaded_titles, 1):
                            t = title[:80] + "..." if len(title) > 80 else title
                            lines.append(f"{i}. {t}")
                    # Failures section — include reason + original YouTube URL
                    if all_failures:
                        lines.append("")
                        lines.append(f"⚠️ Не удалось загрузить ({len(all_failures)}):")
                        for i, f in enumerate(all_failures, 1):
                            t = f["title"][:60] + "..." if len(f["title"]) > 60 else f["title"]
                            lines.append(f"{i}. {t}")
                            lines.append(f"   Причина: {f['reason']}")
                            lines.append(f"   {f['url']}")
                    msg = "\n".join(lines)
                    # Telegram message limit is 4096 chars
                    if len(msg) > 4000:
                        msg = msg[:3990] + "\n..."
                    await bot.send_message(admin_chat_id, msg)
                except Exception:
                    pass

        except Exception as e:
            logger.error("Scheduler error: %s", e)

        await asyncio.sleep(CHECK_INTERVAL)