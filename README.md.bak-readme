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
| `/list` | Список подписок |
| `/unsub` | Отписаться от канала (inline-кнопки) |
| `/quality` | Изменить качество подписки |
| `/playlists` | Список плейлистов VideoHost |
| `/status` | Статус текущей загрузки |
| `/help` | Помощь |

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
python -m bot

# Или через systemd (см. yt2tg-bot.service)
systemctl --user start yt2tg-bot
systemctl --user enable yt2tg-bot
```

### Systemd (опционально)

```bash
cp yt2tg-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start yt2tg-bot
systemctl --user enable yt2tg-bot

# Логи
journalctl --user -u yt2tg-bot -f
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
| `POST` | `/api/bot/upload` | Загрузка видео (multipart: `file`, `title`, `playlistId`) |
| `GET` | `/api/bot/playlists` | Список плейлистов (query: `?playlistId=...` для видео в плейлисте) |

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