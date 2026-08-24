"""Telegram bot handlers — commands, callbacks, FSM flows."""

import asyncio
import logging

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message, CallbackQuery

from bot import database as db
from bot.states import States
from bot.downloader import (
    download_video, extract_video_id, extract_channel_id,
    get_video_info, get_channel_info, cleanup_file, current_status,
    extract_playlist_id, get_youtube_playlist_info, list_channel_videos,
    search_youtube, find_subtitle_path, download_subtitles, probe_duration,
)
from bot.uploader import (
    upload_video, list_playlists, find_or_create_playlist, sort_playlist, video_exists,
)
from bot.keyboards import (
    quality_keyboard, yes_no_keyboard, cancel_keyboard, playlists_keyboard,
    subscriptions_keyboard, main_menu_keyboard, backfill_period_keyboard,
    filters_menu_keyboard, manage_menu_keyboard, search_results_keyboard,
)
from bot.config import ADMIN_IDS, QUALITY_LABELS, DEFAULT_QUALITY
from bot.filters import format_filter_for_display
from bot.uploader import update_playlist_lifetime, get_playlist
from bot.version_checker import get_versions_report

logger = logging.getLogger(__name__)

# Registry of active backfill/playlist-download tasks, keyed by user_id.
# Setting cancel=True causes the running task to stop after the current video.
backfill_tasks: dict[int, dict] = {}


def is_admin(user_id: int) -> bool:
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


def register_handlers(bot: AsyncTeleBot):

    # ── /start ──────────────────────────────────────────────
    @bot.message_handler(commands=["start"])
    async def cmd_start(msg: Message):
        await bot.reply_to(
            msg,
            "🎬 UTube Bot — сохранение видео с YouTube в VideoHost\n\n"
            "Выберите действие в меню ниже 👇",
            reply_markup=main_menu_keyboard(),
        )

    # ── Text button aliases (from ReplyKeyboard) ─────────────
    BUTTON_ALIASES = {
        "🔔 Подписка": "/subscribe",
        "⬇ Скачать видео": "/dl",
        "🔍 Поиск": "/search",
        "📂 YouTube плейлист": "/dl_playlist",
        "📦 Архив за период": "/backfill",
        "📋 Мои подписки": "/list",
        "🎚 Плейлисты": "/playlists",
        "🔍 Фильтры": "/filters",
        "⚙️ Управление": "/manage",
        "📊 Статус": "/status",
        "⏹ Отменить": "/cancel",
        "❓ Помощь": "/help",
    }

    @bot.message_handler(func=lambda m: m.text in BUTTON_ALIASES)
    async def handle_menu_button(msg: Message):
        """Convert reply keyboard button text to the corresponding command."""
        cmd = BUTTON_ALIASES[msg.text]  # e.g. "🔔 Подписка" -> "/subscribe"
        if cmd == "/subscribe":
            await cmd_subscribe(msg)
        elif cmd == "/dl":
            await cmd_dl(msg)
        elif cmd == "/search":
            await cmd_search(msg)
        elif cmd == "/dl_playlist":
            await cmd_dl_playlist(msg)
        elif cmd == "/backfill":
            await cmd_backfill(msg)
        elif cmd == "/list":
            await cmd_list(msg)
        elif cmd == "/playlists":
            await cmd_playlists(msg)
        elif cmd == "/filters":
            await cmd_filters(msg)
        elif cmd == "/manage":
            await cmd_manage(msg)
        elif cmd == "/status":
            await cmd_status(msg)
        elif cmd == "/cancel":
            await cmd_cancel(msg)
        elif cmd == "/help":
            await cmd_help(msg)

    # ── /help ────────────────────────────────────────────────
    @bot.message_handler(commands=["help"])
    async def cmd_help(msg: Message):
        await bot.reply_to(
            msg,
            "🎬 UTube Bot — сохранение видео с YouTube в VideoHost\n\n"
            "🔔 Подписка — авто-загрузка новых видео с канала\n"
            "⬇ Скачать видео — разовая загрузка по ссылке\n"
            "🔍 Поиск — топ-20 видео на YouTube по запросу + кнопки для скачивания\n"
            "📂 YouTube плейлист — скачать весь плейлист\n"
            "📦 Архив за период — скачать старые видео (7/30/90/180/365 дней)\n"
            "📋 Мои подписки — список подписок\n"
            "🎚 Плейлисты — плейлисты VideoHost\n"
            "🔍 Фильтры — белый/чёрный список по словам в названии\n"
            "📊 Статус — статус текущей загрузки\n"
            "⏹ Отменить — остановить текущую загрузку\n\n"
            "Качество: 480p, 720p (по умолчанию), 1080p, 4K\n\n"
            "Команды тоже работают:\n"
            "  /subscribe, /dl, /search, /dl_playlist, /backfill, /list, /playlists,\n"
            "  /filters, /status, /cancel, /unsub, /quality, /versions",
            reply_markup=main_menu_keyboard(),
        )

    # ── /versions — show current versions of all components ──
    @bot.message_handler(commands=["versions"])
    async def cmd_versions(msg: Message):
        if not is_admin(msg.from_user.id):
            await bot.reply_to(msg, "У вас нет доступа.")
            return
        await bot.reply_to(msg, "⏳ Проверяю версии на GitHub...")
        try:
            report = await get_versions_report()
            await bot.reply_to(msg, report, disable_web_page_preview=True)
        except Exception as e:
            logger.error("Failed to get versions: %s", e)
            await bot.reply_to(msg, f"⚠️ Не удалось получить версии: {e}")

    # ── /playlists ──────────────────────────────────────────
    @bot.message_handler(commands=["playlists"])
    async def cmd_playlists(msg: Message):
        try:
            playlists = await list_playlists()
            if not playlists:
                await bot.reply_to(msg, "В VideoHost нет плейлистов.\nСоздайте через веб-интерфейс.")
                return
            lines = [f"Плейлисты VideoHost ({len(playlists)}):"]
            for p in playlists:
                lines.append(f"  {p['name']} (id: {p['id']})")
            await bot.reply_to(msg, "\n".join(lines))
        except Exception as e:
            logger.exception("cmd_playlists error")
            await bot.reply_to(msg, f"Ошибка получения плейлистов: {e}")

    # ── /status ─────────────────────────────────────────────
    @bot.message_handler(commands=["status"])
    async def cmd_status(msg: Message):
        s = current_status
        lines = []

        # Active task
        if s["task"]:
            lines.append(f"📋 Активная задача: {s['task']}")
            if s["title"]:
                lines.append(f"  {s['title']}")
            if s["progress"]:
                lines.append(f"  {s['progress']}")
            if s["error"]:
                lines.append(f"  ⚠️ {s['error']}")
        else:
            lines.append("📋 Активных задач нет.")

        # Backfill task status
        uid = msg.from_user.id
        if uid in backfill_tasks:
            bt = backfill_tasks[uid]
            if bt.get("cancel"):
                lines.append("\n⏹ Загрузка отменяется...")
            else:
                lines.append(f"\n🔄 Загрузка идёт (период: {bt.get('period', '?')})")

        # Recently uploaded videos (last 24h)
        try:
            recent = await db.list_recent_videos(hours=24)
            if recent:
                lines.append(f"\n📹 Загружено за последние 24 часа ({len(recent)}):")
                for v in recent[:15]:
                    title = v.get("title", "?")
                    if len(title) > 50:
                        title = title[:47] + "..."
                    lines.append(f"  • {title}")
                if len(recent) > 15:
                    lines.append(f"  ...и ещё {len(recent) - 15}")
        except Exception:
            pass

        await bot.reply_to(msg, "\n".join(lines))

    # ═══════════════════════════════════════════════════════
    #  /subscribe — FSM: URL -> quality -> playlist -> confirm
    # ═══════════════════════════════════════════════════════
    @bot.message_handler(commands=["subscribe"])
    async def cmd_subscribe(msg: Message):
        if not is_admin(msg.from_user.id):
            await bot.reply_to(msg, "У вас нет доступа.")
            return
        await db.save_fsm_state(msg.from_user.id, States.SUB_ASK_URL, {})
        await bot.reply_to(
            msg,
            "Отправьте ссылку на YouTube-канал или на видео с этого канала:\n\n"
            "Примеры:\n"
            "  https://www.youtube.com/@channelName\n"
            "  https://www.youtube.com/channel/UC...\n"
            "  https://youtu.be/VIDEO_ID\n"
            "  https://youtube.com/watch?v=VIDEO_ID\n\n"
            "Если отправите ссылку на видео — бот определит канал автоматически.",
            reply_markup=cancel_keyboard(),
        )

    # ═══════════════════════════════════════════════════════
    #  /dl — one-off download
    # ═══════════════════════════════════════════════════════
    @bot.message_handler(commands=["dl"])
    async def cmd_dl(msg: Message):
        if not is_admin(msg.from_user.id):
            await bot.reply_to(msg, "У вас нет доступа.")
            return
        await db.save_fsm_state(msg.from_user.id, States.DL_ASK_URL, {})
        await bot.reply_to(
            msg,
            "Отправьте ссылку на YouTube-видео:",
            reply_markup=cancel_keyboard(),
        )

    # ═══════════════════════════════════════════════════════
    #  /dl_playlist — download YouTube playlist
    # ═══════════════════════════════════════════════════════
    @bot.message_handler(commands=["dl_playlist"])
    async def cmd_dl_playlist(msg: Message):
        if not is_admin(msg.from_user.id):
            await bot.reply_to(msg, "У вас нет доступа.")
            return
        await db.save_fsm_state(msg.from_user.id, States.DLPL_ASK_URL, {})
        await bot.reply_to(
            msg,
            "📂 Отправьте ссылку на YouTube-плейлист:\n\n"
            "Пример:\n"
            "  https://www.youtube.com/playlist?list=PLxxxxxxx\n"
            "  https://www.youtube.com/watch?v=XXX&list=PLxxxxxxx",
            reply_markup=cancel_keyboard(),
        )

    # ═══════════════════════════════════════════════════════
    #  /search — search YouTube, show top 20 results, download by tap
    # ═══════════════════════════════════════════════════════
    @bot.message_handler(commands=["search"])
    async def cmd_search(msg: Message):
        if not is_admin(msg.from_user.id):
            await bot.reply_to(msg, "У вас нет доступа.")
            return
        await db.save_fsm_state(msg.from_user.id, States.SEARCH_ASK_QUERY, {})
        await bot.reply_to(
            msg,
            "🔍 Отправьте поисковый запрос:\n\n"
            "Бот найдёт топ-20 видео на YouTube и покажет превью + кнопки для скачивания.\n\n"
            "Пример: «обзор android tv»",
            reply_markup=cancel_keyboard(),
        )

    # ═══════════════════════════════════════════════════════
    #  /backfill — download archive of past videos for a subscription
    # ═══════════════════════════════════════════════════════
    @bot.message_handler(commands=["backfill"])
    async def cmd_backfill(msg: Message):
        if not is_admin(msg.from_user.id):
            await bot.reply_to(msg, "У вас нет доступа.")
            return
        subs = await db.list_subscriptions()
        active_subs = [s for s in subs if s["active"]]
        if not active_subs:
            await bot.reply_to(msg, "Нет активных подписок.\nДобавьте: /subscribe")
            return
        kb = subscriptions_keyboard(active_subs)
        if not kb:
            await bot.reply_to(msg, "Нет активных подписок.")
            return
        await db.save_fsm_state(msg.from_user.id, States.BACKFILL_SELECT_SUB, {})
        await bot.reply_to(
            msg,
            "Выберите подписку для загрузки архива:\n\n"
            "Будут скачаны видео за выбранный период, которых ещё нет в VideoHost. "
            "Уже загруженные видео пропускаются.",
            reply_markup=kb,
        )

    # ═══════════════════════════════════════════════════════
    #  /cancel — cancel current backfill or FSM state
    # ═══════════════════════════════════════════════════════
    @bot.message_handler(commands=["cancel"])
    async def cmd_cancel(msg: Message):
        if not is_admin(msg.from_user.id):
            await bot.reply_to(msg, "У вас нет доступа.")
            return
        uid = msg.from_user.id
        if uid in backfill_tasks:
            backfill_tasks[uid]["cancel"] = True
            await bot.reply_to(
                msg,
                "⏹ Отменяю текущую загрузку...\n"
                "Бот остановится после текущего видео (может занять до минуты).",
            )
            return
        state, _ = await db.get_fsm_state(uid)
        if state:
            await db.clear_fsm_state(uid)
            await bot.reply_to(msg, "✅ Текущее действие отменено.")
            return
        await bot.reply_to(msg, "Нет активной задачи для отмены.", reply_markup=main_menu_keyboard())

    # ═══════════════════════════════════════════════════════
    #  /list — subscriptions
    # ═══════════════════════════════════════════════════════
    @bot.message_handler(commands=["list"])
    async def cmd_list(msg: Message):
        if not is_admin(msg.from_user.id):
            await bot.reply_to(msg, "У вас нет доступа.")
            return
        subs = await db.list_subscriptions()
        if not subs:
            await bot.reply_to(msg, "Нет подписок.\nДобавьте: /subscribe")
            return
        lines = [f"Подписки ({len(subs)}):"]
        for s in subs:
            status = "+" if s["active"] else "-"
            q = s.get("quality", "720")
            white = s.get("white_filter", "") or ""
            black = s.get("black_filter", "") or ""
            filter_tag = ""
            if white or black:
                tags = []
                if white:
                    tags.append(f"✅{format_filter_for_display(white, max_items=2)}")
                if black:
                    tags.append(f"🚫{format_filter_for_display(black, max_items=2)}")
                filter_tag = f" [{', '.join(tags)}]"
            lines.append(
                f"  {status} #{s['id']} {s.get('channel_title', s['channel_id'])} [{q}p]{filter_tag}"
            )
        lines.append("\n/unsub — удалить подписку")
        lines.append("/filters — настроить фильтры")
        await bot.reply_to(msg, "\n".join(lines))

    # ═══════════════════════════════════════════════════════
    #  /unsub — unsubscribe
    # ═══════════════════════════════════════════════════════
    @bot.message_handler(commands=["unsub"])
    async def cmd_unsub(msg: Message):
        if not is_admin(msg.from_user.id):
            await bot.reply_to(msg, "У вас нет доступа.")
            return
        subs = await db.list_subscriptions()
        if not subs:
            await bot.reply_to(msg, "Нет подписок.")
            return
        kb = subscriptions_keyboard(subs)
        if not kb:
            await bot.reply_to(msg, "Нет подписок.")
            return
        await db.save_fsm_state(msg.from_user.id, States.UNSUB_SELECT, {})
        await bot.reply_to(msg, "Выберите подписку для удаления:", reply_markup=kb)

    # ═══════════════════════════════════════════════════════
    #  /quality — change subscription quality
    # ═══════════════════════════════════════════════════════
    @bot.message_handler(commands=["quality"])
    async def cmd_quality(msg: Message):
        if not is_admin(msg.from_user.id):
            await bot.reply_to(msg, "У вас нет доступа.")
            return
        subs = await db.list_subscriptions()
        if not subs:
            await bot.reply_to(msg, "Нет подписок.")
            return
        kb = subscriptions_keyboard(subs)
        if not kb:
            await bot.reply_to(msg, "Нет подписок.")
            return
        await db.save_fsm_state(msg.from_user.id, States.QUALITY_SELECT, {})
        await bot.reply_to(msg, "Выберите подписку:", reply_markup=kb)

    # ═══════════════════════════════════════════════════════
    #  /filters — white/black list filters per subscription
    # ═══════════════════════════════════════════════════════
    @bot.message_handler(commands=["filters"])
    async def cmd_filters(msg: Message):
        if not is_admin(msg.from_user.id):
            await bot.reply_to(msg, "У вас нет доступа.")
            return
        subs = await db.list_subscriptions()
        if not subs:
            await bot.reply_to(msg, "Нет подписок.\nДобавьте: /subscribe")
            return
        kb = subscriptions_keyboard(subs)
        if not kb:
            await bot.reply_to(msg, "Нет подписок.")
            return
        await db.save_fsm_state(msg.from_user.id, States.FILTERS_SELECT_SUB, {})
        await bot.reply_to(
            msg,
            "🔍 Выберите подписку для настройки фильтров:\n\n"
            "• Белый список — видео скачиваются только если в названии есть хотя бы одно из слов\n"
            "• Чёрный список — видео не скачиваются, если в названии есть любое из слов\n\n"
            "Слова разделяются запятыми. Регистр не важен.",
            reply_markup=kb,
        )

    # ═══════════════════════════════════════════════════════
    #  /manage — unified inline menu for all subscription actions
    # ═══════════════════════════════════════════════════════
    @bot.message_handler(commands=["manage"])
    async def cmd_manage(msg: Message):
        if not is_admin(msg.from_user.id):
            await bot.reply_to(msg, "У вас нет доступа.")
            return
        subs = await db.list_subscriptions()
        if not subs:
            await bot.reply_to(msg, "Нет подписок.\nДобавьте: /subscribe")
            return
        kb = subscriptions_keyboard(subs)
        if not kb:
            await bot.reply_to(msg, "Нет подписок.")
            return
        await db.save_fsm_state(msg.from_user.id, States.MANAGE_SELECT_SUB, {})
        await bot.reply_to(
            msg,
            "⚙️ Выберите подписку для управления:\n\n"
            "Доступные действия:\n"
            "• 🗑 Отписаться\n"
            "• 🔍 Фильтры (белый/чёрный список)\n"
            "• 📦 Архив за период\n"
            "• 🎚 Качество\n"
            "• ⏱ Время жизни просмотренных",
            reply_markup=kb,
        )

    # ── Callback query handler ──────────────────────────────
    @bot.callback_query_handler(func=lambda call: True)
    async def handle_callback(call: CallbackQuery):
        try:
            uid = call.from_user.id
            data_str = call.data
            state, data = await db.get_fsm_state(uid)
            logger.info("Callback from %d, state=%s, data=%s", uid, state, data_str)

            # ── Cancel ──
            if data_str == "cancel":
                await db.clear_fsm_state(uid)
                await bot.edit_message_reply_markup(
                    call.message.chat.id, call.message.message_id, reply_markup=None
                )
                await bot.answer_callback_query(call.id, "Отменено")
                return

            # ── Quality selection (shared for subscribe and dl) ──
            # NOTE: only handle SUB_ASK_QUALITY / DL_ASK_QUALITY here.
            # QUALITY_VALUE (changing existing sub quality) is handled below.
            if data_str.startswith("q:") and state in (States.SUB_ASK_QUALITY, States.DL_ASK_QUALITY, States.DLPL_ASK_QUALITY):
                quality = data_str.split(":")[1]
                if quality not in QUALITY_LABELS:
                    await bot.answer_callback_query(call.id, "Неизвестное качество")
                    return
                data["quality"] = quality

                if state == States.SUB_ASK_QUALITY:
                    # Skip playlist selection — playlist will be auto-created on confirm
                    channel_handle = data.get("channel_handle") or data.get("channel_id", "Канал")
                    channel_title = data.get("channel_title", channel_handle)
                    await bot.edit_message_text(
                        f"Качество: {QUALITY_LABELS[quality]}\n\n"
                        f"Канал: {channel_title}\n"
                        f"Handle: @{channel_handle}\n\n"
                        f"Плейлист «{channel_handle}» будет создан автоматически.\n"
                        f"Подтвердите подписку:",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        reply_markup=yes_no_keyboard(),
                    )
                    await db.save_fsm_state(uid, States.SUB_CONFIRM, data)
                elif state == States.DL_ASK_QUALITY:
                    title = data.get("title", "Видео")
                    await bot.edit_message_text(
                        f"Качество: {QUALITY_LABELS[quality]}\n\n"
                        f"Начинаю загрузку:\n{title}\n\n"
                        f"Плейлист будет создан по имени канала автоматически.",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                    )
                    await db.clear_fsm_state(uid)
                    asyncio.create_task(_process_oneoff(uid, data))
                elif state == States.DLPL_ASK_QUALITY:
                    pl_title = data.get("playlist_title", "YouTube Playlist")
                    await bot.edit_message_text(
                        f"Качество: {QUALITY_LABELS[quality]}\n"
                        f"Плейлист: {pl_title}\n"
                        f"Видео: {data.get('video_count', '?')}\n\n"
                        f"Выберите период загрузки:",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        reply_markup=backfill_period_keyboard(),
                    )
                    await db.save_fsm_state(uid, States.DLPL_ASK_PERIOD, data)
                return

            # ── Playlist selection ──
            if data_str.startswith("pl:"):
                playlist_id = data_str.split(":")[1]

                if state == States.SUB_ASK_PLAYLIST:
                    if playlist_id == "skip":
                        await bot.answer_callback_query(call.id, "Выберите плейлист!")
                        return
                    data["playlist_id"] = playlist_id
                    await bot.edit_message_text(
                        f"Подтвердите подписку:\n\n"
                        f"Канал: {data.get('channel_title', '?')}\n"
                        f"Плейлист: {playlist_id}\n"
                        f"Качество: {QUALITY_LABELS.get(data.get('quality', '720'), '?')}\n\n"
                        f"Новые видео будут автоматически загружаться.",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        reply_markup=yes_no_keyboard(),
                    )
                    await db.save_fsm_state(uid, States.SUB_CONFIRM, data)

                elif state == States.DL_ASK_PLAYLIST:
                    data["playlist_id"] = "" if playlist_id == "skip" else playlist_id
                    await db.clear_fsm_state(uid)
                    await bot.edit_message_reply_markup(
                        call.message.chat.id, call.message.message_id, reply_markup=None
                    )
                    pl_info = (
                        f"\nПлейлист: {playlist_id}"
                        if playlist_id != "skip"
                        else "\nБез плейлиста"
                    )
                    await bot.send_message(
                        call.message.chat.id,
                        f"Начинаю загрузку:\n"
                        f"{data.get('title', '?')}\n"
                        f"Качество: {QUALITY_LABELS.get(data.get('quality', '720'), '?')}{pl_info}",
                    )
                    asyncio.create_task(_process_oneoff(uid, data))
                return

            # ── Subscribe confirmation ──
            if data_str == "yes" and state == States.SUB_CONFIRM:
                # Use the channel handle (channel_handle, extracted from yt-dlp's
                # uploader_id, which is the canonical handle YouTube uses) as the
                # playlist name — this matches the /dl flow exactly.
                channel_handle = data.get("channel_handle") or data.get("channel_id") or data.get("channel_title") or "Канал"
                channel_title = data.get("channel_title", channel_handle)
                yt_channel_id = data.get("youtube_channel_id", "")
                # Find or create playlist named after the channel handle
                pl = await find_or_create_playlist(channel_handle)
                if not pl or not pl.get("id"):
                    await bot.answer_callback_query(
                        call.id, f"Не удалось создать плейлист «{channel_handle}»"
                    )
                    return
                playlist_id = pl["id"]
                sub_id = await db.add_subscription(
                    channel_handle,                # channel_id (handle, used by RSS)
                    channel_title,                  # display name
                    playlist_id,
                    data.get("quality", DEFAULT_QUALITY),
                    youtube_channel_id=yt_channel_id,  # UCxxxxx (used by /dl matching)
                )
                await db.clear_fsm_state(uid)
                msg_lines = [
                    f"Подписка оформлена! (#{sub_id})",
                    f"Канал: {channel_title}",
                    f"Handle: @{channel_handle}",
                    f"Плейлист: «{channel_handle}» (id: {playlist_id})",
                ]
                if yt_channel_id:
                    msg_lines.append(f"YouTube ID: {yt_channel_id}")
                await bot.edit_message_text(
                    "\n".join(msg_lines),
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                )
                await bot.answer_callback_query(call.id, "Готово!")
                return

            if data_str == "no" and state == States.SUB_CONFIRM:
                await db.clear_fsm_state(uid)
                await bot.edit_message_text(
                    "Отменено.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                )
                return

            # ── Unsubscribe selection ──
            if data_str.startswith("sub:") and state == States.UNSUB_SELECT:
                sub_id = int(data_str.split(":")[1])
                sub = await db.get_subscription(sub_id)
                if sub:
                    await db.delete_subscription(sub_id)
                    await bot.edit_message_text(
                        f"Удалена подписка: {sub.get('channel_title', sub['channel_id'])}",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                    )
                await bot.answer_callback_query(call.id, "Удалено")
                return

            # ── Quality change selection ──
            if data_str.startswith("sub:") and state == States.QUALITY_SELECT:
                sub_id = int(data_str.split(":")[1])
                data["sub_id"] = sub_id
                sub = await db.get_subscription(sub_id)
                cur_q = sub.get("quality", "720") if sub else "720"
                await bot.edit_message_text(
                    f"Текущее качество: {QUALITY_LABELS.get(cur_q, cur_q)}\nВыберите новое:",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=quality_keyboard(),
                )
                await db.save_fsm_state(uid, States.QUALITY_VALUE, data)
                return

            # ── Backfill: user picked a subscription ──
            if data_str.startswith("sub:") and state == States.BACKFILL_SELECT_SUB:
                sub_id = int(data_str.split(":")[1])
                sub = await db.get_subscription(sub_id)
                if not sub:
                    await bot.answer_callback_query(call.id, "Подписка не найдена")
                    return
                data["sub_id"] = sub_id
                await bot.edit_message_text(
                    f"Загрузка архива для подписки #{sub_id}\n"
                    f"Канал: {sub.get('channel_title', sub.get('channel_id', ''))}\n\n"
                    f"Выберите период:",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=backfill_period_keyboard(),
                )
                await db.save_fsm_state(uid, States.BACKFILL_ASK_PERIOD, data)
                return

            # ── Filters: subscription selected → show filter menu ──
            if data_str.startswith("sub:") and state == States.FILTERS_SELECT_SUB:
                sub_id = int(data_str.split(":")[1])
                sub = await db.get_subscription(sub_id)
                if not sub:
                    await bot.answer_callback_query(call.id, "Подписка не найдена")
                    return
                data["sub_id"] = sub_id
                white = sub.get("white_filter", "") or ""
                black = sub.get("black_filter", "") or ""
                title = sub.get("channel_title", sub.get("channel_id", ""))
                await bot.edit_message_text(
                    f"🔍 Фильтры для подписки #{sub_id}\n"
                    f"Канал: {title}\n\n"
                    f"✅ Белый список (только эти слова в названии):\n"
                    f"  {format_filter_for_display(white)}\n\n"
                    f"🚫 Чёрный список (эти слова блокируют загрузку):\n"
                    f"  {format_filter_for_display(black)}\n\n"
                    f"Пустой список = фильтр отключен.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=filters_menu_keyboard(),
                )
                await db.save_fsm_state(uid, States.FILTERS_MENU, data)
                return

            # ── Filters: edit white list → ask for text input ──
            if data_str == "fw:edit" and state == States.FILTERS_MENU:
                await bot.edit_message_text(
                    "✅ Введите новый белый список.\n\n"
                    "Слова через запятую. Видео будут скачиваться только если "
                    "в названии есть хотя бы одно из слов.\n"
                    "Пустое сообщение или '-' = отключить белый список.\n\n"
                    "Пример: tutorial,обзор,распаковка",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=cancel_keyboard(),
                )
                await db.save_fsm_state(uid, States.FILTERS_ASK_WHITE, data)
                await bot.answer_callback_query(call.id)
                return

            # ── Filters: edit black list → ask for text input ──
            if data_str == "fb:edit" and state == States.FILTERS_MENU:
                await bot.edit_message_text(
                    "🚫 Введите новый чёрный список.\n\n"
                    "Слова через запятую. Видео НЕ будут скачиваться, если в названии "
                    "есть любое из этих слов.\n"
                    "Пустое сообщение или '-' = отключить чёрный список.\n\n"
                    "Пример: shorts,short,премьера",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=cancel_keyboard(),
                )
                await db.save_fsm_state(uid, States.FILTERS_ASK_BLACK, data)
                await bot.answer_callback_query(call.id)
                return

            # ── Filters: clear both ──
            if data_str == "fc:clear" and state == States.FILTERS_MENU:
                sub_id = data.get("sub_id")
                if sub_id:
                    await db.update_subscription_filters(sub_id, "", "")
                await db.clear_fsm_state(uid)
                await bot.edit_message_text(
                    "🧹 Фильтры очищены.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                )
                await bot.answer_callback_query(call.id, "Очищено")
                return

            # ═══════════════════════════════════════════════════════
            # /manage — unified inline menu callbacks
            # ═══════════════════════════════════════════════════════

            # ── Manage: subscription selected → show action menu ──
            if data_str.startswith("sub:") and state == States.MANAGE_SELECT_SUB:
                sub_id = int(data_str.split(":")[1])
                sub = await db.get_subscription(sub_id)
                if not sub:
                    await bot.answer_callback_query(call.id, "Подписка не найдена")
                    return
                data["sub_id"] = sub_id
                title = sub.get("channel_title", sub.get("channel_id", ""))
                white = sub.get("white_filter", "") or ""
                black = sub.get("black_filter", "") or ""
                q = sub.get("quality", "720")
                pl_id = sub.get("playlist_id", "") or ""
                # Build info text
                info_lines = [f"⚙️ Управление подпиской #{sub_id}", f"Канал: {title}", f"Качество: {QUALITY_LABELS.get(q, q + 'p')}"]
                if white or black:
                    info_lines.append(f"Фильтры: ✅{format_filter_for_display(white, max_items=2)} 🚫{format_filter_for_display(black, max_items=2)}")
                else:
                    info_lines.append("Фильтры: —")
                if pl_id:
                    info_lines.append(f"Плейлист: {pl_id[:20]}...")
                await bot.edit_message_text(
                    "\n".join(info_lines),
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=manage_menu_keyboard(),
                )
                await db.save_fsm_state(uid, States.MANAGE_MENU, data)
                return

            # ── Manage: unsub action ──
            if data_str == "mm:unsub" and state == States.MANAGE_MENU:
                sub_id = data.get("sub_id")
                if not sub_id:
                    await bot.answer_callback_query(call.id, "Ошибка: подписка не выбрана")
                    return
                sub = await db.get_subscription(sub_id)
                if not sub:
                    await bot.answer_callback_query(call.id, "Подписка не найдена")
                    return
                await db.delete_subscription(sub_id)
                await db.clear_fsm_state(uid)
                title = sub.get("channel_title", sub.get("channel_id", ""))
                await bot.edit_message_text(
                    f"🗑 Отписка выполнена.\nКанал: {title}\nПлейлист на VideoHost сохранён.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                )
                await bot.answer_callback_query(call.id, "Отписано")
                return

            # ── Manage: filters action → transition to FILTERS_MENU ──
            if data_str == "mm:filters" and state == States.MANAGE_MENU:
                sub_id = data.get("sub_id")
                if not sub_id:
                    await bot.answer_callback_query(call.id, "Ошибка: подписка не выбрана")
                    return
                sub = await db.get_subscription(sub_id)
                if not sub:
                    await bot.answer_callback_query(call.id, "Подписка не найдена")
                    return
                white = sub.get("white_filter", "") or ""
                black = sub.get("black_filter", "") or ""
                title = sub.get("channel_title", sub.get("channel_id", ""))
                await bot.edit_message_text(
                    f"🔍 Фильтры для подписки #{sub_id}\n"
                    f"Канал: {title}\n\n"
                    f"✅ Белый список:\n  {format_filter_for_display(white)}\n\n"
                    f"🚫 Чёрный список:\n  {format_filter_for_display(black)}\n\n"
                    f"Пустой список = фильтр отключен.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=filters_menu_keyboard(),
                )
                await db.save_fsm_state(uid, States.FILTERS_MENU, data)
                await bot.answer_callback_query(call.id)
                return

            # ── Manage: backfill action → transition to BACKFILL_ASK_PERIOD ──
            if data_str == "mm:backfill" and state == States.MANAGE_MENU:
                sub_id = data.get("sub_id")
                if not sub_id:
                    await bot.answer_callback_query(call.id, "Ошибка: подписка не выбрана")
                    return
                sub = await db.get_subscription(sub_id)
                if not sub:
                    await bot.answer_callback_query(call.id, "Подписка не найдена")
                    return
                title = sub.get("channel_title", sub.get("channel_id", ""))
                await bot.edit_message_text(
                    f"📦 Архив за период для подписки #{sub_id}\nКанал: {title}\n\nВыберите период:",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=backfill_period_keyboard(),
                )
                await db.save_fsm_state(uid, States.BACKFILL_ASK_PERIOD, data)
                await bot.answer_callback_query(call.id)
                return

            # ── Manage: quality action → transition to QUALITY_VALUE ──
            if data_str == "mm:quality" and state == States.MANAGE_MENU:
                sub_id = data.get("sub_id")
                if not sub_id:
                    await bot.answer_callback_query(call.id, "Ошибка: подписка не выбрана")
                    return
                await bot.edit_message_text(
                    f"🎚 Выберите новое качество для подписки #{sub_id}:",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=quality_keyboard(),
                )
                await db.save_fsm_state(uid, States.QUALITY_VALUE, data)
                await bot.answer_callback_query(call.id)
                return

            # ── Manage: lifetime action → ask for text input ──
            if data_str == "mm:lifetime" and state == States.MANAGE_MENU:
                sub_id = data.get("sub_id")
                if not sub_id:
                    await bot.answer_callback_query(call.id, "Ошибка: подписка не выбрана")
                    return
                sub = await db.get_subscription(sub_id)
                if not sub:
                    await bot.answer_callback_query(call.id, "Подписка не найдена")
                    return
                pl_id = sub.get("playlist_id", "") or ""
                # Fetch current lifetime from VideoHost (best-effort)
                cur_lifetime = "?"
                if pl_id:
                    try:
                        pl = await get_playlist(pl_id)
                        if pl is not None:
                            lt = pl.get("lifetimeDays")
                            cur_lifetime = f"{lt} дней" if lt else "отключено"
                    except Exception:
                        pass
                await bot.edit_message_text(
                    f"⏱ Время жизни просмотренных для подписки #{sub_id}\n\n"
                    f"Введите количество дней (например, 30).\n"
                    f"Видео, отмеченные «просмотренным», будут автоматически удалены "
                    f"через указанное число дней, если они не отмечены «избранным».\n\n"
                    f"0 или '-' = отключить (видео не удаляются автоматически).\n"
                    f"Текущее значение: {cur_lifetime}",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=cancel_keyboard(),
                )
                await db.save_fsm_state(uid, States.MANAGE_ASK_LIFETIME, data)
                await bot.answer_callback_query(call.id)
                return

            if data_str.startswith("q:") and state == States.QUALITY_VALUE:
                quality = data_str.split(":")[1]
                if quality not in QUALITY_LABELS:
                    await bot.answer_callback_query(call.id, "?")
                    return
                sub_id = data.get("sub_id")
                if sub_id:
                    await db.update_subscription_quality(sub_id, quality)
                    await db.clear_fsm_state(uid)
                    await bot.edit_message_text(
                        f"Качество изменено на {QUALITY_LABELS[quality]}",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                    )
                await bot.answer_callback_query(call.id, "Готово!")
                return

            # ═══════════════════════════════════════════════════════
            #  Backfill: period selection (bp:) for BACKFILL_ASK_PERIOD
            # ═══════════════════════════════════════════════════════
            if data_str.startswith("bp:") and state == States.BACKFILL_ASK_PERIOD:
                period = data_str.split(":")[1]
                sub_id = data.get("sub_id")
                if not sub_id:
                    await bot.answer_callback_query(call.id, "Сессия устарела")
                    return
                period_name = {
                    "7": "7 дней", "30": "30 дней", "90": "90 дней",
                    "180": "180 дней", "365": "1 год", "all": "всё время",
                }.get(period, period)
                await db.clear_fsm_state(uid)
                await bot.edit_message_text(
                    f"🚀 Запускаю загрузку архива за {period_name}...\n"
                    f"Это может занять несколько минут. Я сообщу о результате.\n"
                    f"Чтобы отменить — /cancel",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                )
                asyncio.create_task(_process_backfill(uid, sub_id, period))
                return

            # ═══════════════════════════════════════════════════════
            #  YouTube playlist download: period selection (bp:) for DLPL_ASK_PERIOD
            # ═══════════════════════════════════════════════════════
            if data_str.startswith("bp:") and state == States.DLPL_ASK_PERIOD:
                period = data_str.split(":")[1]
                period_name = {
                    "7": "7 дней", "30": "30 дней", "90": "90 дней",
                    "180": "180 дней", "365": "1 год", "all": "всё время",
                }.get(period, period)
                await db.clear_fsm_state(uid)
                await bot.edit_message_text(
                    f"🚀 Запускаю загрузку плейлиста за {period_name}...\n"
                    f"Это может занять несколько минут. Я сообщу о результате.\n"
                    f"Чтобы отменить — /cancel",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                )
                asyncio.create_task(_process_dl_playlist(uid, data, period))
                return

            # ═══════════════════════════════════════════════════════
            #  Search results: tap on a video to start /dl flow for it
            # ═══════════════════════════════════════════════════════
            if data_str.startswith("sr:") and state == States.SEARCH_RESULTS:
                action = data_str.split(":", 1)[1]
                if action == "new":
                    # User wants a new search
                    await db.save_fsm_state(uid, States.SEARCH_ASK_QUERY, {})
                    await bot.edit_message_text(
                        "🔍 Отправьте новый поисковый запрос:",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        reply_markup=cancel_keyboard(),
                    )
                    await bot.answer_callback_query(call.id)
                    return
                # action is an index into results list
                try:
                    idx = int(action)
                except ValueError:
                    await bot.answer_callback_query(call.id, "Неверный индекс")
                    return
                results = data.get("results") or []
                if idx < 0 or idx >= len(results):
                    await bot.answer_callback_query(call.id, "Результат не найден")
                    return
                r = results[idx]
                url = r["url"]
                title = r["title"]
                yt_id = r["id"]
                # Transition to DL_ASK_QUALITY (skip DL_ASK_URL — we already have the URL)
                data["url"] = url
                data["youtube_id"] = yt_id
                data["title"] = title
                # Clear results from state to avoid storing large list
                data.pop("results", None)
                await db.save_fsm_state(uid, States.DL_ASK_QUALITY, data)
                await bot.edit_message_text(
                    f"🎬 Выбрано: {title}\n"
                    f"   Канал: {r.get('channel', '?')}\n"
                    f"   Ссылка: {url}\n\n"
                    f"Выберите качество:",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=quality_keyboard(),
                )
                await bot.answer_callback_query(call.id)
                return

            await bot.answer_callback_query(call.id, "")

        except Exception as e:
            logger.exception("handle_callback error: %s", e)
            try:
                await bot.answer_callback_query(call.id, f"Ошибка: {e}")
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════
    #  Text handler (MUST be last — catch-all for FSM states)
    # ═══════════════════════════════════════════════════════
    @bot.message_handler(func=lambda m: True, content_types=["text"])
    async def handle_text(msg: Message):
        try:
            state, data = await db.get_fsm_state(msg.from_user.id)
            text = msg.text.strip()
            uid = msg.from_user.id
            logger.info("Text from %d, state=%s, text=%s", uid, state, text[:100])

            if state == States.SUB_ASK_URL:
                ch_id = extract_channel_id(text)
                # If not a channel URL, check if it's a video URL —
                # extract channel info from the video's metadata
                if not ch_id:
                    yt_vid_id = extract_video_id(text)
                    if yt_vid_id:
                        await bot.reply_to(msg, "📦 Это ссылка на видео. Определяю канал...")
                        try:
                            vinfo = await get_video_info(text)
                            if vinfo and vinfo.get("channel_id"):
                                # Build channel URL from video metadata
                                yt_channel_id = vinfo["channel_id"]
                                channel_url = f"https://www.youtube.com/channel/{yt_channel_id}"
                                text = channel_url  # use this for get_channel_info
                                ch_id = yt_channel_id
                                # Skip get_channel_info — we already have all data from video info
                                from bot.downloader import clean_handle
                                channel_handle = clean_handle(vinfo.get("uploader_id", "")) or \
                                                 clean_handle(vinfo.get("uploader_url", "")) or \
                                                 vinfo.get("channel", "") or yt_channel_id
                                title = vinfo.get("channel", "") or channel_handle
                                data["channel_id"] = channel_handle
                                data["channel_handle"] = channel_handle
                                data["channel_title"] = title
                                data["youtube_channel_id"] = yt_channel_id
                                data["original_url"] = text
                                await db.save_fsm_state(uid, States.SUB_ASK_QUALITY, data)
                                await bot.reply_to(
                                    msg, f"Канал: {title}\nHandle: @{channel_handle}\nYouTube ID: {yt_channel_id}\n\nВыберите качество:",
                                    reply_markup=quality_keyboard(),
                                )
                                return
                            else:
                                await bot.reply_to(msg, "Не удалось определить канал из ссылки на видео.")
                                return
                        except Exception as e:
                            logger.error("get_video_info for channel detection error: %s", e)
                            await bot.reply_to(msg, f"Ошибка при получении информации о видео: {e}")
                            return
                    else:
                        await bot.reply_to(msg, "Не удалось распознать ссылку. Отправьте ссылку на канал или видео.")
                        return

                await bot.reply_to(msg, "Получаю информацию о канале...")
                try:
                    info = await get_channel_info(text)
                except Exception as e:
                    logger.error("get_channel_info error: %s", e)
                    info = None
                yt_channel_id = (info.get("channel_id") if info else "") or ""
                channel_handle = (info.get("channel_handle") if info else "") or ch_id
                title = (info.get("title") if info else "") or channel_handle
                data["channel_id"] = channel_handle
                data["channel_handle"] = channel_handle
                data["channel_title"] = title
                data["youtube_channel_id"] = yt_channel_id
                data["original_url"] = text
                await db.save_fsm_state(uid, States.SUB_ASK_QUALITY, data)
                await bot.reply_to(
                    msg, f"Канал: {title}\nHandle: @{channel_handle}\nВыберите качество:",
                    reply_markup=quality_keyboard(),
                )

            elif state == States.DL_ASK_URL:
                yt_id = extract_video_id(text)
                if not yt_id:
                    await bot.reply_to(msg, "Не удалось распознать ссылку на видео.")
                    return
                await bot.reply_to(msg, "Получаю информацию о видео...")
                try:
                    info = await get_video_info(text)
                except Exception as e:
                    logger.error("get_video_info error: %s", e)
                    info = None
                title = info["title"] if info else yt_id
                data["url"] = text
                data["youtube_id"] = yt_id
                data["title"] = title
                await db.save_fsm_state(uid, States.DL_ASK_QUALITY, data)
                await bot.reply_to(
                    msg, f"Видео: {title}\nВыберите качество:",
                    reply_markup=quality_keyboard(),
                )

            elif state == States.DLPL_ASK_URL:
                pl_id = extract_playlist_id(text)
                if not pl_id:
                    await bot.reply_to(msg, "Не удалось распознать ссылку на плейлист. Ищите параметр ?list=PL...")
                    return
                await bot.reply_to(msg, "📂 Получаю информацию о плейлисте...")
                try:
                    pl_info = await get_youtube_playlist_info(text)
                except Exception as e:
                    logger.error("get_youtube_playlist_info error: %s", e)
                    pl_info = None
                if not pl_info or not pl_info.get("videos"):
                    await bot.reply_to(msg, "Не удалось получить список видео из плейлиста.")
                    return
                pl_title = pl_info["title"]
                data["playlist_url"] = text
                data["playlist_title"] = pl_title
                data["video_count"] = len(pl_info["videos"])
                await db.save_fsm_state(uid, States.DLPL_ASK_QUALITY, data)
                await bot.reply_to(
                    msg,
                    f"📂 Плейлист: {pl_title}\n"
                    f"Видео в плейлисте: {len(pl_info['videos'])}\n\n"
                    f"Выберите качество:",
                    reply_markup=quality_keyboard(),
                )

            elif state == States.PLAYLIST_ASK_NAME:
                data["new_playlist_name"] = text
                await db.save_fsm_state(uid, state, data)
                await bot.reply_to(
                    msg,
                    f"Плейлист \"{text}\" будет создан через веб-интерфейс.\n"
                    f"Пожалуйста, создайте плейлист \"{text}\" в VideoHost и вернитесь.",
                    reply_markup=cancel_keyboard(),
                )

            # ── Filters: text input for white/black list ──
            elif state == States.FILTERS_ASK_WHITE:
                sub_id = data.get("sub_id")
                if not sub_id:
                    await bot.reply_to(msg, "Ошибка: подписка не выбрана. Начните заново: /filters")
                    await db.clear_fsm_state(uid)
                    return
                # Treat empty or '-' as "disable"
                new_white = "" if (text in ("", "-", "—")) else text
                # Fetch current black to preserve it
                sub = await db.get_subscription(sub_id)
                cur_black = sub.get("black_filter", "") or "" if sub else ""
                await db.update_subscription_filters(sub_id, new_white, cur_black)
                await db.clear_fsm_state(uid)
                await bot.reply_to(
                    msg,
                    f"✅ Белый список обновлён:\n  {format_filter_for_display(new_white)}\n\n"
                    f"🚫 Чёрный список (без изменений):\n  {format_filter_for_display(cur_black)}",
                    reply_markup=main_menu_keyboard(),
                )
                return

            elif state == States.FILTERS_ASK_BLACK:
                sub_id = data.get("sub_id")
                if not sub_id:
                    await bot.reply_to(msg, "Ошибка: подписка не выбрана. Начните заново: /filters")
                    await db.clear_fsm_state(uid)
                    return
                new_black = "" if (text in ("", "-", "—")) else text
                sub = await db.get_subscription(sub_id)
                cur_white = sub.get("white_filter", "") or "" if sub else ""
                await db.update_subscription_filters(sub_id, cur_white, new_black)
                await db.clear_fsm_state(uid)
                await bot.reply_to(
                    msg,
                    f"✅ Белый список (без изменений):\n  {format_filter_for_display(cur_white)}\n\n"
                    f"🚫 Чёрный список обновлён:\n  {format_filter_for_display(new_black)}",
                    reply_markup=main_menu_keyboard(),
                )
                return

            # ── Manage: lifetime text input ──
            elif state == States.MANAGE_ASK_LIFETIME:
                sub_id = data.get("sub_id")
                if not sub_id:
                    await bot.reply_to(msg, "Ошибка: подписка не выбрана. Начните заново: /manage")
                    await db.clear_fsm_state(uid)
                    return
                sub = await db.get_subscription(sub_id)
                if not sub:
                    await bot.reply_to(msg, "Подписка не найдена.")
                    await db.clear_fsm_state(uid)
                    return
                pl_id = sub.get("playlist_id", "") or ""
                if not pl_id:
                    await bot.reply_to(msg, "У подписки нет плейлиста. Невозможно установить время жизни.")
                    await db.clear_fsm_state(uid)
                    return
                # Parse input
                if text in ("", "-", "—", "0"):
                    lifetime_days = None
                    label = "отключено"
                else:
                    try:
                        n = int(text)
                        if n <= 0:
                            lifetime_days = None
                            label = "отключено"
                        else:
                            lifetime_days = n
                            label = f"{n} дней"
                    except ValueError:
                        await bot.reply_to(msg, "Введите целое число дней (например, 30) или 0 для отключения.")
                        return
                # Call VideoHost API to update playlist lifetimeDays
                result = await update_playlist_lifetime(pl_id, lifetime_days)
                if result:
                    await bot.reply_to(
                        msg,
                        f"⏱ Время жизни просмотренных: {label}\n"
                        f"Подписка #{sub_id}, плейлист {pl_id[:20]}...",
                        reply_markup=main_menu_keyboard(),
                    )
                else:
                    await bot.reply_to(
                        msg,
                        f"⚠️ Не удалось обновить время жизни. Проверьте логи бота.",
                        reply_markup=main_menu_keyboard(),
                    )
                await db.clear_fsm_state(uid)
                return

            # ── Search: query text input ──
            elif state == States.SEARCH_ASK_QUERY:
                query = text.strip()
                if not query:
                    await bot.reply_to(msg, "Пустой запрос. Введите текст для поиска:")
                    return
                if len(query) > 200:
                    await bot.reply_to(msg, "Слишком длинный запрос (макс. 200 символов). Попробуйте короче:")
                    return
                status_msg = await bot.reply_to(msg, f"🔍 Ищу на YouTube: «{query}»...")
                try:
                    results = await search_youtube(query, max_results=20)
                except Exception as e:
                    logger.error("search_youtube error: %s", e)
                    await bot.edit_message_text(
                        f"Ошибка поиска: {e}",
                        chat_id=status_msg.chat.id,
                        message_id=status_msg.message_id,
                    )
                    return
                if not results:
                    await bot.edit_message_text(
                        "Ничего не найдено. Попробуйте другой запрос: /search",
                        chat_id=status_msg.chat.id,
                        message_id=status_msg.message_id,
                        reply_markup=main_menu_keyboard(),
                    )
                    await db.clear_fsm_state(uid)
                    return
                # Save results in FSM state so callback handler can access them by index
                data["query"] = query
                data["results"] = results
                await db.save_fsm_state(uid, States.SEARCH_RESULTS, data)
                # Format message with thumbnails
                lines = [f"🔍 Запрос: «{query}»", f"Найдено: {len(results)} видео", ""]
                for i, r in enumerate(results, 1):
                    title = r["title"]
                    if len(title) > 70:
                        title = title[:67] + "..."
                    dur = r.get("duration") or 0
                    if dur > 0:
                        dur = int(dur)  # yt-dlp returns float, format codes need int
                        if dur >= 3600:
                            dur_str = f"{dur // 3600}:{(dur % 3600) // 60:02d}:{dur % 60:02d}"
                        else:
                            dur_str = f"{dur // 60}:{dur % 60:02d}"
                    else:
                        dur_str = "?"
                    channel = r.get("channel") or ""
                    views = r.get("view_count") or 0
                    views_str = f"{views // 1000}K просмотров" if views >= 1000 else f"{views} просм."
                    lines.append(f"{i}. {title}")
                    lines.append(f"   {channel} · {dur_str} · {views_str}")
                lines.append("")
                lines.append("Нажмите кнопку ниже, чтобы скачать выбранное видео:")
                await bot.edit_message_text(
                    "\n".join(lines),
                    chat_id=status_msg.chat.id,
                    message_id=status_msg.message_id,
                    reply_markup=search_results_keyboard(results),
                )

        except Exception as e:
            logger.exception("handle_text unhandled error: %s", e)
            try:
                await bot.reply_to(msg, f"Ошибка: {e}")
            except Exception:
                pass

    # ── Background task: one-off download ────────────────────────
    async def _process_oneoff(user_id: int, data: dict):
        url = data["url"]
        yt_id = data.get("youtube_id", "")
        title = data.get("title", yt_id)
        quality = data.get("quality", DEFAULT_QUALITY)

        try:
            # Check if already processed — but verify the video still exists on VideoHost.
            # If it was deleted from VideoHost, drop the cached record so we can re-upload.
            existing = await db.get_processed_video(yt_id)
            if existing:
                # If user explicitly deleted the video on VideoHost, never re-upload
                if existing.get("user_deleted", 0):
                    await bot.send_message(user_id, f"Видео было удалено ранее: {title}")
                    return
                vh_id_old = existing.get("videohost_id", "") or ""
                if not vh_id_old:
                    # Processed but never actually uploaded (empty videohost_id).
                    # For /dl (user explicitly requested) we DO want to re-upload —
                    # clear the stale record and fall through to the download.
                    logger.info("Video %s has empty videohost_id — re-uploading via /dl", yt_id)
                    await db.unmark_video_processed(yt_id)
                    existing = None
                elif not await video_exists(vh_id_old):
                    # Video was deleted on VideoHost (by user OR cleanup) — mark as
                    # user-deleted, never re-upload. The previous version set
                    # `existing = None` here, which caused the `if existing: return`
                    # check below to be skipped → the video was re-downloaded and
                    # re-uploaded. With `return` here we abort the /dl flow entirely.
                    logger.info("Video %s (%s) was deleted on VideoHost — marking as user-deleted (won't re-upload)",
                                yt_id, vh_id_old)
                    await db.mark_video_user_deleted(yt_id)
                    await bot.send_message(user_id, f"⚠️ Видео ранее было удалено с хостинга: {title}")
                    return
            if existing:
                current_status.update({"task": "", "progress": "", "error": ""})
                await bot.send_message(user_id, f"Видео уже загружено ранее: {title}")
                return

            # Get channel handle + upload_date + thumbnail + UC channel_id from video info
            info = await get_video_info(url)
            yt_channel_id = (info.get("channel_id") if info else "") or ""
            # channel_handle = canonical handle from uploader_id (without @, may
            # have a numeric suffix like "russiancrashchannel6171" if YouTube
            # added one when the channel was created). This matches what
            # /subscribe would have stored.
            from bot.downloader import clean_handle
            channel_handle = clean_handle(info.get("uploader_id") if info else "") \
                or clean_handle(info.get("uploader_url") if info else "") \
                or (info.get("uploader") if info else "") \
                or data.get("channel_id") or data.get("channel_title") or "YouTube"
            published_at = (info.get("upload_date") if info else "") or ""
            thumbnail_url = (info.get("thumbnail") if info else "") or ""
            # Re-fetch title from info (more accurate than what user passed in /dl url)
            if info and info.get("title"):
                title = info["title"]
            current_status.update({"task": "download", "url": url, "title": title,
                                   "progress": "0%", "error": ""})

            # Check if user is already subscribed to this channel (by UCxxxxx).
            # If yes, reuse the existing playlist instead of creating a new one.
            playlist_id = ""
            reused_sub = False
            if yt_channel_id:
                sub = await db.find_subscription_by_youtube_channel_id(yt_channel_id)
                if sub:
                    playlist_id = sub.get("playlist_id", "") or ""
                    if playlist_id:
                        reused_sub = True
                        logger.info("Video %s belongs to subscribed channel UC=%s → reusing playlist %s",
                                    yt_id, yt_channel_id, playlist_id)
                        await bot.send_message(
                            user_id,
                            f"📡 Канал: @{channel_handle} (подписка #{sub['id']})\n"
                            f"Использую существующий плейлист...",
                        )

            if not playlist_id:
                await bot.send_message(
                    user_id,
                    f"📡 Канал: @{channel_handle}\nСоздаю плейлист...",
                )
                pl = await find_or_create_playlist(channel_handle)
                playlist_id = pl.get("id", "") if pl else ""

            file_path = await download_video(url, quality)
            if not file_path or file_path == "TOO_LARGE" or file_path == "PERMANENT_FAIL" or file_path == "NO_SPACE" or file_path == "AUTH_REQUIRED":
                if file_path == "TOO_LARGE":
                    await bot.send_message(user_id, f"⚠️ Файл слишком большой: {title}")
                elif file_path == "PERMANENT_FAIL":
                    # yt-dlp detected a permanent failure (live event not started,
                    # premiere scheduled, private/deleted, members-only). Tell
                    # the user and mark as user-deleted so subscriptions don't
                    # keep retrying this same URL every cycle.
                    err_detail = current_status.get("error", "")[:200]
                    await bot.send_message(
                        user_id,
                        f"⚠️ Видео недоступно для скачивания: {title}\n"
                        f"Причина: {err_detail or 'live event / premiere / private'}\n\n"
                        f"Это премьера, идёт прямой эфир, или видео скрыто.\n"
                        f"Если это премьера — попробуйте снова после её начала."
                    )
                    await db.mark_video_user_deleted(yt_id)
                elif file_path == "NO_SPACE":
                    # Disk full on the bot's VPS. NOT a video problem —
                    # the user can retry after the operator cleans up /tmp.
                    await bot.send_message(
                        user_id,
                        f"⚠️ Нет места на диске сервера: {title}\n\n"
                        f"Бот не может скачать видео — заполнен /tmp.\n"
                        f"Попробуйте позже, после очистки диска."
                    )
                elif file_path == "AUTH_REQUIRED":
                    # YouTube anti-bot challenge — not permanent, will work
                    # later when YouTube relaxes the protection.
                    await bot.send_message(
                        user_id,
                        f"⚠️ YouTube заблокировал скачивание: {title}\n\n"
                        f"Срабатывает анти-бот защита (\"Sign in to confirm you're not a bot\").\n"
                        f"Это временно — попробуйте позже."
                    )
                else:
                    err_detail = current_status.get("error", "")[:200]
                    await bot.send_message(
                        user_id,
                        f"⚠️ Ошибка скачивания: {title}\n"
                        f"Причина: {err_detail or 'неизвестна'}\n\n"
                        f"Возможно, видео недоступно или YouTube требует обновления yt-dlp.\n"
                        f"Попробуйте позже или другое качество."
                    )
                return

            sub_path = await download_subtitles(url, yt_id) if yt_id else None
            # Duration: prefer yt-dlp info, fallback to ffprobe on the file
            dur = (info.get("duration") if info else None) or probe_duration(file_path)
            result = await upload_video(
                file_path, title, playlist_id or None,
                published_at=published_at,
                thumbnail_url=thumbnail_url,
                youtube_id=yt_id,
                subtitle_path=sub_path or "",
                duration=dur,
            )
            cleanup_file(file_path)
            if sub_path:
                cleanup_file(sub_path)

            vh_id = result.get("id", "") if result else ""
            if vh_id:
                # Re-sort the playlist chronologically by publishedAt
                if playlist_id:
                    await sort_playlist(playlist_id)
                await db.mark_video_processed(yt_id, None, title, quality, vh_id)
                msg_text = f"✅ Загружено: {title}\nКанал: @{channel_handle}"
                if playlist_id:
                    if reused_sub:
                        msg_text += f"\nПлейлист: существующий (id: {playlist_id})"
                    else:
                        msg_text += f"\nПлейлист: «{channel_handle}» (id: {playlist_id})"
                msg_text += f"\nID: {vh_id}"
                await bot.send_message(user_id, msg_text)
            else:
                await bot.send_message(user_id, f"Ошибка загрузки на сервер: {title}")
        except Exception as e:
            logger.exception("_process_oneoff error: %s", e)
            try:
                await bot.send_message(user_id, f"Ошибка при загрузке: {e}")
            except Exception:
                pass
        finally:
            current_status.update({
                "task": "", "progress": "", "error": "", "url": "", "title": ""
            })
    # ── Background task: download YouTube playlist ────────────
    async def _process_dl_playlist(user_id: int, data: dict, period: str):
        """Download all videos from a YouTube playlist into a VideoHost playlist
        named 'ytpls_<playlist_title>'.
        """
        from datetime import datetime, timedelta, timezone

        backfill_tasks[user_id] = {"sub_id": 0, "period": period, "cancel": False}

        try:
            playlist_url = data["playlist_url"]
            pl_title = data.get("playlist_title", "YouTube Playlist")
            quality = data.get("quality", DEFAULT_QUALITY)
            vh_playlist_name = f"ytpls_{pl_title}"

            # Create VideoHost playlist
            pl = await find_or_create_playlist(vh_playlist_name)
            if not pl or not pl.get("id"):
                await bot.send_message(user_id, f"❌ Не удалось создать плейлист «{vh_playlist_name}»")
                return
            playlist_id = pl["id"]

            await bot.send_message(
                user_id,
                f"📡 Получаю список видео из плейлиста «{pl_title}»...\n"
                f"Плейлист на VideoHost: «{vh_playlist_name}»\n"
                f"Чтобы отменить — /cancel",
            )

            # Get all videos from YouTube playlist
            pl_info = await get_youtube_playlist_info(playlist_url)
            if not pl_info or not pl_info.get("videos"):
                await bot.send_message(user_id, "❌ Не удалось получить видео из плейлиста.")
                return

            all_videos = pl_info["videos"]

            # Filter by period
            if period == "all":
                cutoff = None
                period_label = "всё время"
            else:
                days = int(period)
                cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
                period_label = f"{days} дн."

            in_period = []
            skipped_no_date = 0
            too_old = 0
            for v in all_videos:
                upload_date_str = v.get("upload_date", "")
                if not upload_date_str or len(upload_date_str) != 8:
                    # For playlists, include videos without date (unlike backfill)
                    in_period.append(v)
                    continue
                try:
                    pub_dt = datetime.strptime(upload_date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
                    if cutoff and pub_dt < cutoff:
                        too_old += 1
                        continue
                    v["_pub_dt"] = pub_dt
                    in_period.append(v)
                except Exception:
                    in_period.append(v)

            # Filter out already processed
            to_download = []
            already_done = 0
            for v in in_period:
                existing = await db.get_processed_video(v["id"])
                if existing:
                    # If user explicitly deleted the video on VideoHost, never re-upload
                    if existing.get("user_deleted", 0):
                        already_done += 1
                        continue
                    vh_id_old = existing.get("videohost_id", "") or ""
                    if not vh_id_old:
                        # Processed but never actually uploaded — re-upload
                        await db.unmark_video_processed(v["id"])
                        to_download.append(v)
                    elif not await video_exists(vh_id_old):
                        # Video was deleted on VideoHost — mark as user-deleted, never re-upload.
                        # The previous version called unmark_video_processed here, which reset
                        # user_deleted=0 AND cleared videohost_id, causing re-download. Bug fix.
                        logger.info("Video %s was deleted on VideoHost — marking as user-deleted (won't re-upload)",
                                    v["id"])
                        await db.mark_video_user_deleted(v["id"])
                        already_done += 1
                    else:
                        already_done += 1
                else:
                    to_download.append(v)

            to_download.sort(key=lambda x: x.get("_pub_dt") or datetime.min.replace(tzinfo=timezone.utc))

            summary = (
                f"📊 Плейлист: {pl_title}\n"
                f"Всего видео: {len(all_videos)}\n"
                f"В периоде «{period_label}»: {len(in_period)}\n"
            )
            if too_old > 0:
                summary += f"Старше периода: {too_old}\n"
            summary += f"Уже загружено: {already_done}\n"
            summary += f"К загрузке: {len(to_download)}"
            await bot.send_message(user_id, summary)

            if not to_download:
                await bot.send_message(user_id, "✅ Нечего загружать — все видео уже есть.")
                return

            uploaded_count = 0
            failed_count = 0
            for i, v in enumerate(to_download, 1):
                if backfill_tasks.get(user_id, {}).get("cancel"):
                    await bot.send_message(
                        user_id,
                        f"⏹ Загрузка отменена.\nЗагружено: {uploaded_count}, ошибок: {failed_count}",
                    )
                    break

                yt_id = v["id"]
                title = v.get("title", yt_id)
                url = v.get("url") or f"https://www.youtube.com/watch?v={yt_id}"
                pub_dt = v.get("_pub_dt")
                published_at = pub_dt.strftime("%Y%m%d") if pub_dt else ""

                try:
                    await bot.send_message(user_id, f"[{i}/{len(to_download)}] ⬇ {title}")

                    file_path = await download_video(url, quality)
                    if not file_path or file_path == "TOO_LARGE" or file_path == "PERMANENT_FAIL" or file_path == "NO_SPACE" or file_path == "AUTH_REQUIRED":
                        if file_path == "TOO_LARGE":
                            await db.mark_video_processed(yt_id, None, title, quality, "")
                        elif file_path == "PERMANENT_FAIL":
                            # Live event / premiere / private — skip permanently
                            await db.mark_video_processed(yt_id, None, title, quality, "")
                            await db.mark_video_user_deleted(yt_id)
                        elif file_path == "NO_SPACE":
                            # Disk full — don't mark, retry next cycle
                            logger.error("Disk full during /backfill — skipping %s, will retry", yt_id)
                        elif file_path == "AUTH_REQUIRED":
                            # YouTube anti-bot — don't mark, retry next cycle
                            logger.warning("YouTube anti-bot challenge during /backfill — skipping %s, will retry", yt_id)
                        failed_count += 1
                        continue

                    yt_thumb = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"
                    sub_path = await download_subtitles(url, yt_id) if yt_id else None
                    dur = v.get("duration") or probe_duration(file_path)
                    result = await upload_video(
                        file_path, title, playlist_id,
                        published_at=published_at,
                        thumbnail_url=yt_thumb,
                        youtube_id=yt_id,
                        subtitle_path=sub_path or "",
                        duration=dur,
                    )
                    cleanup_file(file_path)
                    if sub_path:
                        cleanup_file(sub_path)

                    if result:
                        vh_id = result.get("id", "")
                        await db.mark_video_processed(yt_id, None, title, quality, vh_id)
                        uploaded_count += 1
                        await sort_playlist(playlist_id)
                    else:
                        failed_count += 1
                except Exception as e:
                    logger.exception("dl_playlist error on %s: %s", yt_id, e)
                    failed_count += 1

            await bot.send_message(
                user_id,
                f"🏁 Загрузка плейлиста завершена!\n"
                f"Плейлист: «{vh_playlist_name}»\n"
                f"Загружено: {uploaded_count}\n"
                f"Ошибок: {failed_count}\n"
                f"Уже было: {already_done}",
            )

        except Exception as e:
            logger.exception("_process_dl_playlist error: %s", e)
            try:
                await bot.send_message(user_id, f"Ошибка: {e}")
            except Exception:
                pass
        finally:
            backfill_tasks.pop(user_id, None)
            current_status.update({"task": "", "progress": "", "error": "", "url": "", "title": ""})

    # ── Background task: backfill (archive download) ────────────
    async def _process_backfill(user_id: int, sub_id: int, period: str):
        """Download all videos from a subscription's channel within the given period."""
        from datetime import datetime, timedelta, timezone

        backfill_tasks[user_id] = {"sub_id": sub_id, "period": period, "cancel": False}

        try:
            sub = await db.get_subscription(sub_id)
            if not sub:
                await bot.send_message(user_id, "Подписка не найдена.")
                return

            channel_handle = sub.get("channel_id", "")
            yt_channel_id = sub.get("youtube_channel_id", "") or ""
            playlist_id = sub.get("playlist_id", "")
            quality = sub.get("quality", DEFAULT_QUALITY)
            sub_title = sub.get("channel_title", channel_handle)

            if yt_channel_id:
                channel_url = f"https://www.youtube.com/channel/{yt_channel_id}/videos"
            elif channel_handle:
                channel_url = f"https://www.youtube.com/@{channel_handle}/videos"
            else:
                await bot.send_message(user_id, "Не удалось построить URL канала.")
                return

            await bot.send_message(
                user_id,
                f"📡 Получаю список видео канала «{sub_title}»...\nЧтобы отменить — /cancel",
            )

            all_videos = await list_channel_videos(channel_url, max_count=200)
            if not all_videos:
                await bot.send_message(user_id, "Не удалось получить список видео.")
                return

            if period == "all":
                cutoff = None
                period_label = "всё время"
            else:
                days = int(period)
                cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
                period_label = f"{days} дн."

            in_period = []
            skipped_no_date = 0
            too_old = 0
            for v in all_videos:
                upload_date_str = v.get("upload_date", "")
                if not upload_date_str or len(upload_date_str) != 8:
                    skipped_no_date += 1
                    continue
                try:
                    pub_dt = datetime.strptime(upload_date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
                    if cutoff and pub_dt < cutoff:
                        too_old += 1
                        continue
                    v["_pub_dt"] = pub_dt
                    in_period.append(v)
                except Exception:
                    skipped_no_date += 1

            to_download = []
            already_done = 0
            for v in in_period:
                existing = await db.get_processed_video(v["id"])
                if existing:
                    vh_id_old = existing.get("videohost_id", "") or ""
                    if vh_id_old and not await video_exists(vh_id_old):
                        await db.unmark_video_processed(v["id"])
                        to_download.append(v)
                    else:
                        already_done += 1
                else:
                    to_download.append(v)

            to_download.sort(key=lambda x: x.get("_pub_dt") or datetime.min.replace(tzinfo=timezone.utc))

            summary_lines = [
                f"📊 Найдено видео: {len(all_videos)}",
                f"В периоде «{period_label}»: {len(in_period)}",
            ]
            if skipped_no_date > 0:
                summary_lines.append(f"Без даты (пропущено): {skipped_no_date}")
            if too_old > 0:
                summary_lines.append(f"Старше периода: {too_old}")
            summary_lines.append(f"Уже загружено: {already_done}")
            summary_lines.append(f"К загрузке: {len(to_download)}")
            await bot.send_message(user_id, "\n".join(summary_lines))

            if not to_download:
                await bot.send_message(user_id, "✅ Нечего загружать — все видео уже есть.")
                return

            uploaded_count = 0
            failed_count = 0
            for i, v in enumerate(to_download, 1):
                if backfill_tasks.get(user_id, {}).get("cancel"):
                    await bot.send_message(user_id, f"⏹ Отменено. Загружено: {uploaded_count}, ошибок: {failed_count}")
                    break

                yt_id = v["id"]
                title = v.get("title", yt_id)
                url = v.get("url") or f"https://www.youtube.com/watch?v={yt_id}"
                pub_dt = v.get("_pub_dt")
                published_at = pub_dt.strftime("%Y%m%d") if pub_dt else ""

                try:
                    await bot.send_message(user_id, f"[{i}/{len(to_download)}] ⬇ {title}")
                    file_path = await download_video(url, quality)
                    if not file_path or file_path == "TOO_LARGE" or file_path == "PERMANENT_FAIL" or file_path == "NO_SPACE" or file_path == "AUTH_REQUIRED":
                        if file_path == "TOO_LARGE":
                            await db.mark_video_processed(yt_id, sub_id, title, quality, "")
                        elif file_path == "PERMANENT_FAIL":
                            # Live event / premiere / private — skip permanently
                            await db.mark_video_processed(yt_id, sub_id, title, quality, "")
                            await db.mark_video_user_deleted(yt_id)
                        elif file_path == "NO_SPACE":
                            # Disk full — don't mark, retry next cycle
                            logger.error("Disk full during /backfill — skipping %s, will retry", yt_id)
                        elif file_path == "AUTH_REQUIRED":
                            # YouTube anti-bot — don't mark, retry next cycle
                            logger.warning("YouTube anti-bot challenge during /backfill — skipping %s, will retry", yt_id)
                        failed_count += 1
                        continue

                    yt_thumb = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"
                    sub_path = await download_subtitles(url, yt_id) if yt_id else None
                    dur = v.get("duration") or probe_duration(file_path)
                    result = await upload_video(
                        file_path, title, playlist_id or None,
                        published_at=published_at,
                        thumbnail_url=yt_thumb,
                        youtube_id=yt_id,
                        subtitle_path=sub_path or "",
                        duration=dur,
                    )
                    cleanup_file(file_path)
                    if sub_path:
                        cleanup_file(sub_path)

                    if result:
                        vh_id = result.get("id", "")
                        await db.mark_video_processed(yt_id, sub_id, title, quality, vh_id)
                        uploaded_count += 1
                        if playlist_id:
                            await sort_playlist(playlist_id)
                    else:
                        failed_count += 1
                except Exception as e:
                    logger.exception("Backfill error on %s: %s", yt_id, e)
                    failed_count += 1

            await bot.send_message(
                user_id,
                f"🏁 Загрузка архива завершена!\nКанал: «{sub_title}»\nПериод: {period_label}\n"
                f"Загружено: {uploaded_count}\nОшибок: {failed_count}\nУже было: {already_done}",
            )

        except Exception as e:
            logger.exception("_process_backfill error: %s", e)
            try:
                await bot.send_message(user_id, f"Ошибка: {e}")
            except Exception:
                pass
        finally:
            backfill_tasks.pop(user_id, None)
            current_status.update({"task": "", "progress": "", "error": "", "url": "", "title": ""})
