"""Configuration from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TG_BOT_TOKEN: str = os.environ["TG_BOT_TOKEN"]
ADMIN_IDS: list[int] = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]

# VideoHost
VIDEOHOST_URL: str = os.environ.get("VIDEOHOST_URL", "http://127.0.0.1:3002")
VIDEOHOST_TOKEN: str = os.environ.get("VIDEOHOST_TOKEN", "")

# Database
DATABASE_URL: str = os.environ.get("DATABASE_URL", "data/yt2tg_bot.db")

# Downloader
CHECK_INTERVAL: int = int(os.environ.get("CHECK_INTERVAL", "3600"))
TMP_DIR: str = os.environ.get("TMP_DIR", "/tmp/yt2tg")
# Max file size for yt-dlp downloads. Accepts human-readable values like "2G",
# "500M", "100K", or a bare byte count. "0" = no limit.
# yt-dlp's --max-filesize aborts downloads that exceed this size, preventing
# the 10 GB VPS1 disk from filling up on long videos (e.g. a 7-hour 720p
# stream is ~6 GB — way too big for this VPS).
MAX_FILE_SIZE: str = os.environ.get("MAX_FILE_SIZE", "2G")
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

DEFAULT_QUALITY = "720"

QUALITIES = {
    "480": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "720": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "4k": "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
}

QUALITY_LABELS = {"480": "480p", "720": "720p", "1080": "1080p", "4k": "4K"}
