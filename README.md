# Car Monitor Bot

Telegram-бот для мониторинга новых объявлений о продаже авто на Auto.ru, Дром.ру,
Авито и лотов аукциона Copart.

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
│   ├── avito.py       # Авито через ScraperAPI
│   ├── copart.py      # Copart публичный JSON API
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
SCRAPER_API_KEY=       # ScraperAPI для Авито/Дрома (опционально)
USD_RUB_RATE=90        # курс для пересчёта цен Copart (по умолчанию 90)
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

## Copart

Аукцион битых авто из США и Канады. Работает через публичный JSON API самого сайта —
ни ScraperAPI, ни прокси, ни авторизация не нужны.

**Endpoint** (найден в DevTools → Network → Fetch/XHR на `copart.com/lotSearchResults`):

```
POST https://www.copart.com/public/lots/search-results
Content-Type: application/json

{"query":["*"],"filter":{"MAKE":["lot_make_desc:\"CHEVROLET\""]},
 "sort":["auction_date_type asc"],"page":0,"size":100,"start":0,
 "freeFormSearch":false,"hideImages":true,"backPage":"search"}
```

Группы фильтров берутся из `facetFields` того же ответа:

| Группа | Выражение | Поле фильтра бота |
|--------|-----------|-------------------|
| `MAKE` | `lot_make_desc:"CHEVROLET"` | марка |
| `MODL` | `lot_model_desc:"CRUZE"` | модель |
| `YEAR` | `lot_year:[2015 TO 2020]` | год от/до |
| `ODM`  | `odometer_reading_received:[0 TO 150000]` | пробег от/до |
| `SDAT` | `auction_date_utc:[NOW TO NOW+7DAY]` | аукцион с/по |

Особенности:

- **Цена** — `la`, оценочная стоимость в **долларах** (у канадских лотов — CAD).
  Текущие ставки в поиске всегда `0` — они видны только авторизованным.
  Границы цены в фильтре задаются в рублях и пересчитываются по `USD_RUB_RATE`.
- **Пробег** — `orr`, в **милях**.
- **Город** — площадка хранения (`yn`), например `FL - JACKSONVILLE NORTH`.
- **Дата аукциона** — `ad`; у лотов со статусом Future её нет, поле остаётся пустым.
- Названия моделей у Copart свои. Если точное совпадение по `lot_model_desc`
  ничего не дало, парсер ищет по марке и отсеивает по заголовку лота.
- Марок LADA / SKODA / RENAULT / GEELY / CHERY на аукционе нет — запрос не отправляется.

Источник `copart` не входит в `sources` по умолчанию — его нужно выбрать явно
в фильтре (кнопка «🟡 Copart» или «🌍 Всё вместе»).

### Миграция БД

Новые колонки применяются автоматически при старте (`db/repository.py`),
либо вручную в SQL-редакторе Neon:

```sql
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS damage_description TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS auction_date       TIMESTAMPTZ;
ALTER TABLE favorites     ADD COLUMN IF NOT EXISTS damage_description TEXT;
ALTER TABLE favorites     ADD COLUMN IF NOT EXISTS auction_date       TIMESTAMPTZ;
ALTER TABLE filters       ADD COLUMN IF NOT EXISTS auction_date_from  DATE;
ALTER TABLE filters       ADD COLUMN IF NOT EXISTS auction_date_to    DATE;
```

## Команды бота

| Команда | Описание |
|---------|----------|
| /start | Приветствие |
| /filters | Управление фильтрами поиска |
| /status | Статистика (фильтры, seen_listings) |
| /help | Справка |
