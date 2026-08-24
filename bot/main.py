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


async def main():
    await db.init_db()
    logger.info("Bot started (admins: %s)", ADMIN_IDS)

    # Register commands with Telegram (shows in / command autocomplete)
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

    # Start scheduler in background
    admin_id = ADMIN_IDS[0] if ADMIN_IDS else 0
    asyncio.create_task(scheduler_loop(bot, admin_id))

    # Start version checker in background (notifies admins of new commits/APK)
    asyncio.create_task(version_checker_loop(bot))

    # Start polling
    await bot.infinity_polling()


if __name__ == "__main__":
    asyncio.run(main())