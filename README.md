# Car Monitor Bot

Telegram-бот для мониторинга новых объявлений о продаже авто на Auto.ru и Дром.ру.

## Стек

- **aiogram 3** — Telegram Bot
- **FastAPI + uvicorn** — веб-сервер (webhook + /run для крона)
- **asyncpg** — работа с PostgreSQL
- **aiohttp + BeautifulSoup4** — парсинг Дром
- **Neon PostgreSQL** — хранение фильтров и seen_listings
- **Render** — хостинг (без Docker)
- **cron-job.org** — триггер каждые 30 минут
- **GitHub** — деплой через git push

## Структура

```
car-monitor-bot/
├── bot/
│   ├── main.py        # FastAPI + aiogram webhook
│   └── handlers.py    # /start, /filters, /status, FSM
├── parsers/
│   ├── base.py        # Listing, SearchFilter, BaseParser
│   ├── autoru.py      # Auto.ru внутренний AJAX API
│   └── drom.py        # Дром HTML парсер
├── db/
│   ├── schema.sql     # DDL таблиц
│   └── repository.py  # все запросы к БД
├── scheduler.py       # POST /run endpoint
├── notifier.py        # форматирование + отправка в TG
├── config.py          # env переменные
├── requirements.txt
├── render.yaml
└── .env.example
```

## Деплой

### 1. Neon PostgreSQL

Создай БД на [neon.tech](https://neon.tech), выполни `db/schema.sql` в SQL Editor.

### 2. Переменные окружения

Скопируй `.env.example` в `.env` и заполни:

```env
BOT_TOKEN=...          # от @BotFather
OWNER_ID=...           # твой Telegram user_id (узнай у @userinfobot)
DATABASE_URL=...       # строка подключения Neon
WEBHOOK_HOST=...       # https://your-app.onrender.com
WEBHOOK_SECRET=...     # любая случайная строка
AUTORU_SESSION_ID=     # cookie с Auto.ru (опционально)
AUTORU_CSRF_TOKEN=     # csrf токен Auto.ru (опционально)
```

### 3. Render

1. Создай новый **Web Service** на [render.com](https://render.com)
2. Подключи GitHub репо
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn bot.main:app --host 0.0.0.0 --port $PORT`
5. Добавь все env vars из `.env.example`
6. Задеплой

### 4. cron-job.org

Создай задачу:
- **URL**: `https://your-app.onrender.com/run`
- **Method**: POST
- **Header**: `X-Secret: <твой WEBHOOK_SECRET>`
- **Schedule**: каждые 30 минут

### 5. Бот

Открой бота в Telegram, нажми /start, затем /filters для создания первого фильтра.

## Auto.ru cookies

Auto.ru использует Яндекс SmartCaptcha. Без cookies парсер работает в базовом режиме.
Для полноценной работы:
1. Открой auto.ru в браузере, залогинься
2. DevTools → Application → Cookies → auto.ru
3. Скопируй `autoru_sid` в `AUTORU_SESSION_ID`
4. Скопируй `csrf_token` (или из заголовков запроса) в `AUTORU_CSRF_TOKEN`
5. Обновляй по мере необходимости в настройках Render

## Команды бота

| Команда | Описание |
|---------|----------|
| /start | Приветствие |
| /filters | Управление фильтрами поиска |
| /status | Статистика (фильтры, seen_listings) |
| /help | Справка |
