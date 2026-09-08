"""Entry point for yt2tg Telegram bot."""

import asyncio
import os
import logging

from telebot.async_telebot import AsyncTeleBot
from bot.config import TG_BOT_TOKEN, ADMIN_IDS
from bot import database as db
from bot.handlers import register_handlers
from bot.scheduler import scheduler_loop
from bot.version_checker import version_checker_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("yt2tg")

# AsyncTeleBot uses aiohttp which doesn't respect HTTPS_PROXY env var.
# Monkey-patch aiohttp to respect HTTPS_PROXY env var
# (AsyncTeleBot creates its own session without trust_env=True)
import aiohttp
_orig_session_init = aiohttp.ClientSession.__init__
def _patched_init(self, *args, **kwargs):
    kwargs.setdefault("trust_env", True)
    _orig_session_init(self, *args, **kwargs)
aiohttp.ClientSession.__init__ = _patched_init

bot = AsyncTeleBot(TG_BOT_TOKEN)
register_handlers(bot)

# Keep strong references to background tasks so they are never GC-ed.
_bg_tasks = []


async def _register_commands() -> None:
    """Register the /-command list with Telegram (client autocomplete)."""
    from telebot.types import BotCommand
    await bot.set_my_commands([
        BotCommand("subscribe", "🔔 Подписка на канал"),
        BotCommand("dl", "⬇ Скачать видео"),
        BotCommand("search", "🔍 Поиск на YouTube"),
        BotCommand("dl_playlist", "📂 Скачать плейлист"),
        BotCommand("backfill", "📦 Архив за период"),
        BotCommand("list", "📋 Мои подписки"),
        BotCommand("playlists", "🎚 Плейлисты"),
        BotCommand("filters", "🔍 Фильтры"),
        BotCommand("manage", "⚙️ Управление"),
        BotCommand("status", "📊 Статус"),
        BotCommand("cancel", "⏹ Отменить"),
        BotCommand("help", "❓ Помощь"),
        BotCommand("versions", "📋 Версии"),
    ])


async def _register_commands_with_retry() -> None:
    """Retry command registration in the background until it succeeds.

    set_my_commands is cosmetic: if the AWG tunnel / gost bridge is still
    cold right after boot, the call may time out — that must never kill
    startup or prevent polling (incident 2026-09-07).
    """
    delays = (15, 30, 60, 120)  # backoff, then every 120 s
    attempt = 0
    while True:
        try:
            await _register_commands()
            logger.info("Bot commands registered with Telegram")
            return
        except Exception as exc:  # any API/transport error is retried
            delay = delays[min(attempt, len(delays) - 1)]
            attempt += 1
            logger.warning(
                "set_my_commands failed (attempt %d, retry in %ds): %r",
                attempt, delay, exc,
            )
            await asyncio.sleep(delay)


async def main():
    await db.init_db()
    logger.info("Bot started (admins: %s)", ADMIN_IDS)

    # Register commands with Telegram (shows in / command autocomplete).
    # Non-critical: runs in the background with retries, never blocks startup.
    _bg_tasks.append(asyncio.create_task(_register_commands_with_retry()))

    # Start polling FIRST — this is the main event loop driver.
    # Scheduler and version_checker run as background tasks.
    # Polling must be responsive even when scheduler is downloading videos.
    admin_id = ADMIN_IDS[0] if ADMIN_IDS else 0

    # Start scheduler in background (will yield CPU to polling via asyncio)
    asyncio.create_task(scheduler_loop(bot, admin_id))

    # Start version checker in background
    asyncio.create_task(version_checker_loop(bot))

    # Start polling — this blocks (runs forever), but asyncio ensures
    # scheduler tasks get CPU time between polling cycles.
    # Added timeout=10 (short long-poll cycle) for faster response.
    # If polling ever dies (proxy hiccup etc.), restart it instead of
    # exiting: a dead-but-"active" process is invisible to systemd
    # Restart=always (2026-09-07 incident).
    while True:
        try:
            await bot.infinity_polling(timeout=10, skip_pending=False)
            logger.warning("infinity_polling returned unexpectedly — restarting in 15s")
        except Exception as exc:  # keep the bot alive on any polling error
            logger.exception("infinity_polling crashed: %r — restarting in 15s", exc)
        await asyncio.sleep(15)


if __name__ == "__main__":
    asyncio.run(main())
