"""Version checker — polls GitHub for new commits and APK releases.

Checks 3 repositories every hour (alongside the RSS scheduler):
  - sakurka-cmd/videohost      (backend, private — needs GITHUB_TOKEN)
  - sakurka-cmd/yt2tg-bot      (this bot, public)
  - sakurka-cmd/videohost-tv   (APK source + download/*.apk, public)

When a new commit or APK is detected, notifies all ADMIN_IDS via Telegram
with a summary and direct GitHub links. Uses the GitHub API — needs
GITHUB_TOKEN env var for the private videohost repo (5000 req/hour with
token vs 60 req/hour without).
"""

import asyncio
import logging
import os
import re
import aiohttp
from datetime import datetime
from dotenv import load_dotenv

# load_dotenv() must run before _get_github_token() is called, so that
# GITHUB_TOKEN from .env is available in os.environ.
load_dotenv()

from bot import database as db
from bot.config import ADMIN_IDS

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"

# Optional GitHub token (needed for private repos like videohost).
# Without it, private repos return 404 and are skipped.
# Read lazily (at call time) so load_dotenv() in config.py has run first.
def _get_github_token() -> str:
    return os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")

REPOS = {
    "videohost": "sakurka-cmd/videohost",
    "yt2tg-bot": "sakurka-cmd/yt2tg-bot",
    "videohost-tv": "sakurka-cmd/videohost-tv",
}

REPO_LABELS = {
    "videohost": "Backend (videohost)",
    "yt2tg-bot": "Bot (yt2tg-bot)",
    "videohost-tv": "APK (videohost-tv)",
}

# Check interval: 1 hour (aligned with RSS scheduler)
VERSION_CHECK_INTERVAL = 3600


async def _gh_get(session: aiohttp.ClientSession, url: str) -> dict | list | None:
    """GET from GitHub API with proper headers. Returns None on error."""
    try:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "yt2tg-bot-version-checker",
        }
        if _get_github_token():
            headers["Authorization"] = f"token {_get_github_token()}"
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return await resp.json()
            elif resp.status == 403:
                # Rate limit — log and skip this cycle
                remaining = resp.headers.get("X-RateLimit-Remaining", "?")
                logger.warning("GitHub API rate limit (remaining=%s), skipping version check", remaining)
                return None
            elif resp.status == 404:
                logger.warning("GitHub API 404 for %s (private repo without token?)", url)
                return None
            else:
                logger.warning("GitHub API %s returned %d", url, resp.status)
                return None
    except Exception as e:
        logger.error("GitHub API request failed for %s: %s", url, e)
        return None


async def _get_stored(repo: str, key: str) -> str:
    """Get stored value from version_state table."""
    con = db.get_db()
    row = await con.execute_fetchall(
        "SELECT value FROM version_state WHERE repo=? AND key=?",
        (repo, key),
    )
    return row[0][0] if row else ""


async def _set_stored(repo: str, key: str, value: str):
    """Store value in version_state table."""
    con = db.get_db()
    await con.execute(
        "INSERT INTO version_state (repo, key, value, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(repo, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (repo, key, value, datetime.utcnow().isoformat()),
    )
    await con.commit()


async def _fetch_latest_commit(session: aiohttp.ClientSession, repo: str) -> dict | None:
    """Fetch latest commit from a GitHub repo. Returns {sha, message, url, author, files_count} or None."""
    full = REPOS[repo]
    data = await _gh_get(session, f"{GITHUB_API}/repos/{full}/commits?per_page=1")
    if not data or not isinstance(data, list) or len(data) == 0:
        return None
    c = data[0]
    sha = c.get("sha", "")
    message = c.get("commit", {}).get("message", "").split("\n")[0][:200]  # first line, truncated
    author = c.get("commit", {}).get("author", {}).get("name", "?")
    return {
        "sha": sha,
        "message": message,
        "url": c.get("html_url", f"{GITHUB_WEB}/{full}/commit/{sha}"),
        "author": author,
        "short_sha": sha[:7] if sha else "?",
    }


def _apk_version_key(name: str) -> tuple:
    """Extract version tuple from APK filename for natural sorting.
    e.g. 'UTubeTV-debug-v1.8.apk' -> (1, 8), 'VideoHostTV-debug-v1.0.apk' -> (1, 0)
    Falls back to (0,) if no version found.
    """
    m = re.search(r"v(\d+)(?:\.(\d+))?", name)
    if m:
        major = int(m.group(1))
        minor = int(m.group(2)) if m.group(2) else 0
        return (major, minor)
    return (0,)


async def _fetch_apk_list(session: aiohttp.ClientSession) -> list[dict] | None:
    """Fetch list of APK files from videohost-tv/download/ directory.
    Sorted by version descending (newest first)."""
    data = await _gh_get(session, f"{GITHUB_API}/repos/{REPOS['videohost-tv']}/contents/download")
    if not data or not isinstance(data, list):
        return None
    apks = []
    for item in data:
        name = item.get("name", "")
        if name.endswith(".apk"):
            apks.append({
                "name": name,
                "size": item.get("size", 0),
                "url": item.get("html_url", f"{GITHUB_WEB}/{REPOS['videohost-tv']}/blob/main/download/{name}"),
                "download_url": item.get("download_url", f"https://raw.githubusercontent.com/{REPOS['videohost-tv']}/main/download/{name}"),
            })
    # Natural sort by version (newest first): v1.8 > v1.7 > v1.5 > v1.0
    apks.sort(key=lambda a: _apk_version_key(a["name"]), reverse=True)
    return apks


def _format_size(size: int) -> str:
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.0f} MB"


async def check_versions(bot) -> dict:
    """Check all 3 repos for new commits + APK. Returns summary dict.
    Sends notifications to ADMIN_IDS if changes detected."""
    summary = {"commits": {}, "apks": [], "notified": False}
    notifications = []

    async with aiohttp.ClientSession() as session:
        # Check commits for each repo
        for repo_key in REPOS:
            commit = await _fetch_latest_commit(session, repo_key)
            if not commit:
                continue
            summary["commits"][repo_key] = commit

            stored_sha = await _get_stored(repo_key, "last_commit_sha")
            if stored_sha and stored_sha != commit["sha"]:
                # New commit detected!
                label = REPO_LABELS[repo_key]
                notifications.append(
                    f"🔔 Новая версия {label}\n\n"
                    f"📦 Commit: {commit['short_sha']}\n"
                    f"📝 {commit['message']}\n"
                    f"👤 {commit['author']}\n\n"
                    f"🔗 {commit['url']}"
                )
            elif not stored_sha:
                # First run — store but don't notify (avoid spam on bot restart)
                logger.info("First version check for %s, storing SHA (no notification)", repo_key)

            await _set_stored(repo_key, "last_commit_sha", commit["sha"])

        # Check APK files
        apks = await _fetch_apk_list(session)
        if apks:
            summary["apks"] = apks
            latest_apk = apks[0]  # newest by name
            stored_apk = await _get_stored("videohost-tv", "latest_apk_name")
            if stored_apk and stored_apk != latest_apk["name"]:
                # New APK detected!
                notifications.append(
                    f"📱 Новый APK: {latest_apk['name']}\n\n"
                    f"📦 Размер: {_format_size(latest_apk['size'])}\n\n"
                    f"🔗 Скачать: {latest_apk['download_url']}"
                )
            elif not stored_apk:
                logger.info("First APK check, storing name (no notification)")

            await _set_stored("videohost-tv", "latest_apk_name", latest_apk["name"])

    # Send notifications to all admins
    if notifications and ADMIN_IDS:
        for admin_id in ADMIN_IDS:
            for msg in notifications:
                try:
                    await bot.send_message(admin_id, msg, disable_web_page_preview=False)
                except Exception as e:
                    logger.error("Failed to send version notification to %s: %s", admin_id, e)
        summary["notified"] = True
        logger.info("Version check: sent %d notification(s) to %d admin(s)", len(notifications), len(ADMIN_IDS))

    return summary


async def get_versions_report() -> str:
    """Generate a /versions command report showing current versions of all components."""
    async with aiohttp.ClientSession() as session:
        lines = ["📋 Текущие версии:\n"]

        for repo_key in REPOS:
            commit = await _fetch_latest_commit(session, repo_key)
            label = REPO_LABELS[repo_key]
            if commit:
                lines.append(f"{label}:")
                lines.append(f"  commit: {commit['short_sha']} — {commit['message'][:80]}")
                lines.append(f"  👤 {commit['author']}")
                lines.append(f"  🔗 {commit['url']}")
            else:
                lines.append(f"{label}: не удалось получить (GitHub API)")
            lines.append("")

        # APK info
        apks = await _fetch_apk_list(session)
        if apks:
            latest = apks[0]
            lines.append("APK (последний):")
            lines.append(f"  📱 {latest['name']} ({_format_size(latest['size'])})")
            lines.append(f"  🔗 Скачать: {latest['download_url']}")
        else:
            lines.append("APK: не удалось получить список")

    return "\n".join(lines)


async def version_checker_loop(bot):
    """Background loop: check versions every VERSION_CHECK_INTERVAL seconds."""
    logger.info("Version checker started (interval: %ds)", VERSION_CHECK_INTERVAL)
    # Initial delay (let bot fully start before first check)
    await asyncio.sleep(30)
    while True:
        try:
            await check_versions(bot)
        except Exception as e:
            logger.error("Version checker error: %s", e)
        await asyncio.sleep(VERSION_CHECK_INTERVAL)
