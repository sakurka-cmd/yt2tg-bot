# yt2tg-bot

YouTube → VideoHost Telegram Bot

Автоматическая загрузка видео с YouTube-каналов в приватный видеохостинг [VideoHost](https://github.com/vladimir-turin/videohost) с управлением через Telegram.

## Возможности

- **Подписка на YouTube-каналы** с автоматической загрузкой новых видео по RSS
- **Разовая загрузка** видео по ссылке на YouTube
- **Выбор качества**: 480p, 720p (по умолчанию), 1080p, 4K
- **Управление плейлистами** VideoHost (выбор при загрузке)
- **Дедупликация** — одно видео не загружается дважды
- **FSM-машина состояний** с персистентностью в SQLite
- **Отслеживание прогресса** загрузки в реальном времени
- **Постоянная клавиатура** для удобного управления
- **Фоновый планировщик** для проверки подписок

## Архитектура

```
YouTube RSS → yt-dlp (скачать) → /tmp → VideoHost Bot API → Плейлист VideoHost
```

```
bot/
├── __init__.py           # Точка входа
├── config.py           # Конфигурация из .env
├── database.py         # SQLite: подписки, дедупликация, FSM
├── downloader.py       # yt-dlp обёртка: скачивание, метаданные, RSS
├── uploader.py         # VideoHost API: загрузка, плейлисты
├── handlers.py         # Команды, FSM-состояния, callback-кнопки
├── keyboards.py        # Inline- и reply-клавиатуры
├── scheduler.py        # Фоновый цикл проверки подписок по RSS
└── states.py           # Enum состояний FSM
```

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие + меню |
| `/subscribe` | Подписка на YouTube-канал (FSM: ссылка → качество → плейлист → подтверждение) |
| `/dl` | Разовая загрузка видео по ссылке |
| `/dl_playlist` | Скачать весь YouTube-плейлист в отдельный плейлист VideoHost |
| `/backfill` | Скачать архив за период (7/30/90/180/365 дней или всё время) |
| `/list` | Список подписок (с краткой сводкой фильтров) |
| `/unsub` | Отписаться от канала (inline-кнопки) |
| `/quality` | Изменить качество подписки |
| `/filters` | Настроить белый/чёрный список слов в названиях видео |
| `/manage` | Единое inline-меню управления подпиской (отписка, фильтры, архив, качество, время жизни) |
| `/playlists` | Список плейлистов VideoHost |
| `/status` | Статус текущей загрузки |
| `/cancel` | Отменить текущую операцию |
| `/help` | Помощь |

### Фильтры подписок (белый/чёрный список)

Команда `/filters` позволяет настроить для каждой подписки два списка слов:

- **Белый список** — видео скачиваются **только** если в названии есть хотя бы
  одно из слов списка. Пустой = ограничение отключено (все видео проходят).
- **Чёрный список** — видео **не** скачиваются, если в названии есть любое из
  слов списка. Пустой = блокировка отключена.

Оба списка можно комбинировать: белый сужает множество кандидатов, чёрный
удаляет из него конкретные элементы. Слова разделяются запятыми, регистр
не важен, сопоставление — подстрока (Python `in`).

**Примеры:**
- Белый: `tutorial,обзор,распаковка` → только обучающие видео/обзоры/распаковки
- Чёрный: `shorts,short,премьера` → исключить Shorts и премьеры
- Белый + чёрный: `tutorial` + `shorts` → только tutorials, но не Shorts-варианты

Фильтры применяются в scheduler (новые видео) и в `/backfill` (архив).
В `/dl` (разовая загрузка) фильтры **не** применяются — пользователь явно
указал конкретное видео.

В журнале бота фильтрация видна как:
```
Filter skip: <title> (<yt_id>) — white='...' black='...'
```

### Управление подписками (/manage)

Команда `/manage` открывает единое inline-меню для всех действий с подпиской:

1. `/manage` → выбор подписки из списка
2. Inline-меню с 5 действиями:
   - 🗑 **Отписаться** — удалить подписку
   - 🔍 **Фильтры** — белый/чёрный список слов
   - 📦 **Архив** — backfill за период (7/30/90/180/365 дней или всё время)
   - 🎚 **Качество** — изменить качество (480p/720p/1080p/4K)
   - ⏱ **Время жизни** — управлять `lifetimeDays` плейлиста VideoHost

«Время жизни просмотренных» — особенно важная опция: она связана с auto-cleanup в VideoHost. При `lifetimeDays > 0` видео, помеченные как «просмотренные», автоматически удаляются из VideoHost через N дней (избранные сохраняются). Бот вызывает `PUT /api/bot/playlists/[id]` с новым `lifetimeDays`.

### Постоянная клавиатура

Все основные команды доступны через reply-клавиатуру (emoji-кнопки):

| Кнопка | Команда |
|--------|---------|
| 🔔 Подписка | `/subscribe` |
| ⬇ Скачать видео | `/dl` |
| 📂 YouTube плейлист | `/dl_playlist` |
| 📦 Архив за период | `/backfill` |
| 📋 Мои подписки | `/list` |
| 🎚 Плейлисты | `/playlists` |
| 📊 Статус | `/status` |
| ⏹ Отменить | `/cancel` |
| ❓ Помощь | `/help` |

Кнопка «⚙️ Управление» (→ `/manage`) добавлена в клавиатуру в v1.7.0.

## Установка

### Зависимости

- Python 3.12+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`pip install yt-dlp`)
- [ffmpeg](https://ffmpeg.org/) (для слияния видео/аудио)

### Установка

```bash
git clone https://github.com/sakurka-cmd/yt2tg-bot.git
cd yt2tg-bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Настройка

1. Создайте Telegram-бота через [@BotFather](https://t.me/BotFather)
2. В VideoHost админке создайте токен для бота (вкладка «Токены ботов»)
3. Создайте плейлист в VideoHost, куда бот будет загружать видео
4. Скопируйте `.env.example` в `.env` и заполните:

```bash
cp .env.example .env
nano .env
```

### Переменные окружения (.env)

| Переменная | Обязательна | Описание |
|------------|-------------|----------|
| `TG_BOT_TOKEN` | Да | Токен Telegram-бота от @BotFather |
| `VIDEOHOST_URL` | Нет | URL VideoHost (по умолчанию `http://127.0.0.1:3002`) |
| `VIDEOHOST_TOKEN` | Да | Токен бота из админки VideoHost |
| `ADMIN_IDS` | Нет | Список Telegram ID администраторов через запятую (доступ для всех если пусто) |
| `CHECK_INTERVAL` | Нет | Интервал проверки подписок в секундах (по умолчанию 3600 = 1 час) |
| `DATABASE_URL` | Нет | Путь к SQLite (по умолчанию `data/yt2tg_bot.db`) |
| `TMP_DIR` | Нет | Временная директория для загрузок (по умолчанию `/tmp/yt2tg`) |
| `MAX_FILE_SIZE` | Нет | Макс. размер файла в байтах (0 = без лимита) |
| `LOG_LEVEL` | Нет | Уровень логирования (по умолчанию `INFO`) |

### Запуск

```bash
# Прямой запуск (для тестирования)
source venv/bin/activate
python -m bot.main

# Или через systemd (см. ниже)
```

### Systemd (опционально)

Пример — установка под root (замените пути, если ставите под другим пользователем):

```bash
# Файл сервиса в репозитории уже настроен на /root/yt2tg-bot.
# Если ставите под другим пользователем, отредактируйте User/WorkingDirectory/ExecStart.
sudo cp yt2tg-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now yt2tg-bot

# Логи
sudo journalctl -u yt2tg-bot -f
```

## Как работает

### Подписка на канал

1. `/subscribe` → отправляете ссылку на YouTube-канал
2. Бот парсит канал, показывает название
3. Выбираете качество (480p / 720p / 1080p / 4K)
4. Бот показывает список плейлистов VideoHost — выбираете целевой
5. Подтверждаете → бот создаёт подписку

После этого планировщик раз в час проверяет RSS-ленту канала. Новые видео (за последние 7 дней) скачиваются через yt-dlp и загружаются в VideoHost через Bot API. Каждое видео проверяется на дубликаты по YouTube ID.

### Разовая загрузка

1. `/dl` → отправляете ссылку на YouTube-видео
2. Бот показывает название и предлагает выбрать качество
3. Опционально — выбор плейлиста
4. Файл скачивается и загружается на сервер

### VideoHost Bot API

Бот использует следующие эндпоинты VideoHost:

| Метод | Эндпоинт | Описание |
|-------|---------|----------|
| `POST` | `/api/bot/upload` | Загрузка видео (multipart: `file`, `title`, `playlistId`, `publishedAt`, `thumbnailUrl`, `youtubeId`) |
| `GET` | `/api/bot/playlists` | Список всех плейлистов |
| `GET` | `/api/bot/playlists?playlistId=...` | Список видео в плейлисте (`id`, `title`, `streamUrl`, `order`) |
| `POST` | `/api/bot/playlists` | Создание плейлиста (для `/dl_playlist` — плейлист `ytpls_<title>`) |
| `PUT` | `/api/bot/playlists/[id]` | Обновление плейлиста (`name`, `description`, `lifetimeDays`) — используется `/manage` для времени жизни |
| `POST` | `/api/bot/playlists/[id]/sort` | Сортировка видео в плейлисте по `publishedAt` |
| `GET` | `/api/bot/videos/[id]` | Проверка существования видео в VideoHost (для дедупликации) |

## Безопасность

- Доступ к боту можно ограничить через `ADMIN_IDS` — только указанные Telegram ID могут пользоваться
- Токен VideoHost передаётся через заголовок `Authorization: Bearer <token>` или `X-Bot-Token`
- FSM-состояния хранятся в SQLite и переживают перезапуск бота

## Сравнение с yt2vk-bot

| Функция | yt2vk-bot | yt2tg-bot |
|---------|-----------|-----------|
| Платформа | VK Communities | Telegram |
| Хранение | VK Video API | VideoHost (self-hosted) |
| Скачивание | yt-dlp | yt-dlp |
| Качество | 480p–4K | 480p–4K |
| Подписки | RSS | RSS |
| Дедупликация | SQLite | SQLite |
| FSM | vkbottle | pyTelegramBotAPI |
| Планировщик | Встроенный | Встроенный |

## Лицензия

MIT

## Changelog

### v1.7.1 — 2026-06-30

**Fixed:**
- `get_channel_feed()` now retries up to 3 times with 10s backoff on transient empty RSS responses. YouTube occasionally returns an empty feed (cache refresh / brief outage); previously the scheduler logged a warning and gave up, missing any new videos until the next hourly check. For rarely-publishing channels this could age videos past the 7-day skip window and lose them forever.

### v1.7.0 — 2026-06-29 (Batch 9 — UTube)

**Added:**
- `/manage` command — unified inline menu for all subscription actions (unsub, filters, backfill, quality, lifetime).
- «⏱ Время жизни» action in `/manage` — manage `lifetimeDays` of the playlist via `PUT /api/bot/playlists/[id]`.
- «⚙️ Управление» button in the reply keyboard.

### v1.6.0 — 2026-06-29

**Added:**
- `/dl_playlist` command — download entire YouTube playlist into separate VideoHost playlist (named `ytpls_<title>`).
- Period selection for `/dl_playlist` (7/30/90/180/365 days or all time).
- Beautiful reply keyboard with emoji icons: 🔔 Подписка, ⬇ Скачать видео, 📂 YouTube плейлист, 📦 Архив за период, 📋 Мои подписки, 🎚 Плейлисты, 📊 Статус, ⏹ Отменить, ❓ Помощь.
- Subscribe via video link — bot auto-detects channel from video URL metadata.
- Enhanced `/status` — shows active task, backfill status, and videos uploaded in last 24 hours.
- `db.list_recent_videos(hours=24)` for status reporting.

**Fixed:**
- Scheduler using handle instead of UC channel_id for RSS — all subscriptions now work.
- `list_channel_videos` not imported — caused backfill failure.
- `backfill_period_keyboard` not imported.
- Menu button handler — `BUTTON_ALIASES` lookup was broken.
- Backfill, cancel, and _process_backfill restored (were lost in rebuilds).
