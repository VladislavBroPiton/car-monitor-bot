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
| `ODM`  | `odometer_reading_received:[0 TO 150000]` | пробег от/до (в милях) |
| `SDAT` | `auction_date_utc:[NOW TO NOW+7DAY]` | аукцион с/по |
| `TITL` | `title_group_code:TITLEGROUP_C` | документ |
| `FETI` | `lot_condition_code:CERT-D` | только на ходу |
| `FETI` | `buy_it_now_code:B1` | только «купить сразу» |
| `PRID` | `-damage_type_code:(DAMAGECODE_BN OR …)` | исключить повреждения |
| `LOC`  | `yard_name:FL*` | площадки (штаты) |

Внутри одной группы выражения объединяются по ИЛИ, разные группы — по И,
отрицание пишется через `-`. Группа `FETI` одна на «на ходу» и «купить сразу»,
поэтому при обоих включённых фильтрах в запрос уходит «на ходу»,
а Buy It Now отбирается уже на нашей стороне.

Особенности:

- **Цена** — `la`, оценочная стоимость в **долларах** (у канадских лотов — CAD).
  Текущие ставки в поиске всегда `0` — они видны только авторизованным.
  Границы цены в фильтре задаются в рублях и пересчитываются по `USD_RUB_RATE`.
- **Пробег** — `orr`, в **милях**. Границы пробега в фильтре задаются в километрах
  и переводятся в мили автоматически.
- **Город** — площадка хранения (`yn`), например `FL - JACKSONVILLE NORTH`.
- **Дата аукциона** — `ad`; у лотов со статусом Future её нет, поле остаётся пустым.
- Названия моделей у Copart свои. Если точное совпадение по `lot_model_desc`
  ничего не дало, парсер ищет по марке и отсеивает по заголовку лота.
- Марок LADA / SKODA / RENAULT / GEELY / CHERY на аукционе нет — запрос не отправляется.

### Два способа искать на Copart

**Отдельный фильтр аукциона** (`filters.kind = 'copart'`) — основной путь.
Создаётся кнопкой «➕🟡 Фильтр» в главном меню, свой мастер из 12 шагов.
Значения задаются в единицах самого аукциона:

| Поле | Единица |
|------|---------|
| Цена от/до | **доллары**, без пересчёта |
| Пробег до | **мили** |
| Марка, модель | латиница |
| Города | не применяются — вместо них штаты площадок |

Такой фильтр не обращается к Auto.ru, Дрому и Авито: `scheduler.py`
маршрутизирует его только в `CopartParser`.

**Аукцион как дополнительный источник обычного фильтра** — `copart` в `sources`.
Оставлено для совместимости: цена там в рублях и пересчитывается по
`USD_RUB_RATE`, пробег в километрах переводится в мили. По умолчанию
источник выключен, включается кнопкой «🟡 Copart» или «🌍 Всё вместе».

### Что показывается по лоту

Фото (`tims`), номер лота, оценочная стоимость и цена «купить сразу», оценка
стоимости ремонта (`rc`), тип документа, отметка «на ходу», наличие ключей,
характер повреждения, дата торгов по Москве, площадка, VIN и характеристики
(двигатель, привод, топливо, цвет).

Картинка приходит в виде превью `_thb.jpg`; заменой суффикса доступны
`_ful.jpg` (~78 КБ) и `_hrs.jpg` (~270 КБ). Используется `_ful` —
константа `IMAGE_SIZE` в `parsers/copart.py`.

Отметка `NOT ACTUAL` в поле `ord` означает, что показаниям одометра верить
нельзя — в карточке это выводится явно.

### Напоминания о торгах

`notify_upcoming_auctions` вызывается из `scheduler.py` на каждом обходе
и присылает уведомление за сутки и за час до начала торгов. Стадия хранится
в `seen_listings.auction_notify_stage` (0 → 1 → 2), поэтому повторов нет.
В тихие часы напоминания не отправляются.

### Повторные выставления

Copart перевыставляет непроданные лоты под новым номером, но VIN остаётся тот же.
`count_relists` считает прошлые появления по VIN, и в уведомлении появляется
пометка «выставляется повторно». Это сигнал: машина не уходит с торгов —
либо цена завышена, либо есть проблема, не видная в описании.

### Расчёт «под ключ»

`costs.py` прикидывает итоговую стоимость: лот + аукционный сбор + брокер +
доставка до порта + фрахт + пошлина. Тарифы задаются переменными окружения
(`COPART_*`, см. `.env.example`) — значения по умолчанию ориентировочные.

Точность ограничена: Copart не отдаёт цену продажи, поэтому за базу берётся
оценочная стоимость либо цена «купить сразу». Аукционный сбор у Copart
прогрессивный и зависит от способа оплаты — здесь он взят единым процентом.

### Статистика по оценкам

Кнопка «📊 Оценки» в разделе Copart. Разрезы: модель, год, повреждение,
тип документа, штат. Показывает вилку и среднее по накопленным лотам.
Это статистика **оценочных стоимостей**, а не цен продажи — последние
через публичный API недоступны.

### Марки и модели берутся у аукциона

Списки не захардкожены: `fetch_makes()` и `fetch_models(make)` достают их
из `facetFields`, которые приходят в каждом ответе поиска. Группа `MODL`
сужается под выбранную марку, поэтому в мастере видны реальные названия
Copart с числом лотов рядом — 400 марок, 314 моделей у Toyota.
Кэш в памяти на 6 часов.

### Предпросмотр фильтра

`CopartParser.preview()` — один запрос, показывает сколько лотов подходит
и примеры, не сохраняя фильтр. Доступен на последнем шаге мастера
и кнопкой «🔎 Проверить сейчас» в карточке готового фильтра.

Различает три ситуации: ничего не найдено (фильтр узкий), найдено больше
`FETCH_LIMIT` (фильтр широкий, часть лотов не увидим) и «лоты есть,
но не проходят по цене» — самая частая, потому что границы в долларах.

### Ограничение уведомлений

`MAX_NOTIFY_PER_RUN` (по умолчанию 15) — потолок сообщений в чат за один
обход одного фильтра. Без него первый прогон нового фильтра высыпал бы
до 300 карточек с фотографиями подряд. Остальные сохраняются молча,
следом приходит сводка со ссылкой на Mini App.

Сохранение и отправка разнесены: сначала всё новое пишется в БД, потом
отправляется. Иначе обрыв на середине оставил бы часть лотов «невиденными»,
и они прилетели бы повторно.

### Ограничение выдачи

За один обход берётся не больше `MAX_PAGES × PAGE_SIZE` = 300 лотов на фильтр.
Если найдено больше, в лог уходит `WARNING` с фактическим числом — молча
выдача не обрезается.

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

ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS currency       TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS image_url      TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS title_group    TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS has_keys       TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS run_and_drive  BOOLEAN;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS buy_now_price  INTEGER;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS repair_cost    INTEGER;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS odometer_brand TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS vin            TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS specs          TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS auction_notify_stage SMALLINT DEFAULT 0;

ALTER TABLE filters ADD COLUMN IF NOT EXISTS kind           TEXT DEFAULT 'ru';
UPDATE filters SET kind = 'ru' WHERE kind IS NULL;
ALTER TABLE filters ADD COLUMN IF NOT EXISTS title_groups   TEXT[];
ALTER TABLE filters ADD COLUMN IF NOT EXISTS damage_exclude TEXT[];
ALTER TABLE filters ADD COLUMN IF NOT EXISTS yards          TEXT[];
ALTER TABLE filters ADD COLUMN IF NOT EXISTS run_and_drive  BOOLEAN;
ALTER TABLE filters ADD COLUMN IF NOT EXISTS buy_now_only   BOOLEAN;
```

## Тесты

```bash
python tests/check_py311.py    # совместимость с версией Python на Render
python -m pytest tests -q      # либо: python tests/test_copart.py
node tests/test_miniapp.mjs    # логика Mini App
```

**`check_py311.py` запускать перед каждым деплоем.** Render работает на
Python 3.11; если локально стоит 3.12+, `ast.parse` пропустит конструкции,
которых на проде нет, и сервис упадёт при импорте. Так уже случалось
с вложенными одинаковыми кавычками в f-строке (PEP 701).

Офлайн, сеть не нужна. Python-тесты проверяют разбор на зафиксированной выдаче
API в `tests/fixtures/copart_search.json`; JS-тесты грузят скрипт страницы
в заглушённое окружение и проверяют чистые функции — форматирование валюты,
отсчёт до торгов, тяжесть повреждения.

Ловят молчаливую поломку: когда бот продолжает работать, но шлёт карточки
без цены и фото, а Mini App подписывает доллары рублями.

## Курс валют и здоровье источников

Курс доллара тянется с `cbr.ru` (зеркало `cbr-xml-daily.ru` — запасное),
кэш 6 часов, при недоступности берётся `USD_RUB_RATE` из окружения.

`source_health` считает, сколько обходов подряд источник вернул пусто.
После трёх подряд бот пишет владельцу — иначе смена формата у площадки
заметна только по тишине в чате. Повторный алерт приходит лишь после того,
как источник оживёт и замолчит снова.

## Почему нет IAAI

Второй крупный аукцион добавить не вышло. Страница поиска отдаёт 2 МБ
HTML-оболочки без данных (результаты подгружаются XHR-ом), JSON-эндпоинт
`Search/GetSearchResults` отвечает `302`, а `api.iaai.com` недоступен.
В разметке — обфусцированный сенсор Akamai Bot Manager. Получение данных
потребовало бы обхода антибот-защиты, поэтому источник не реализован.

## Команды бота

| Команда | Описание |
|---------|----------|
| /start | Приветствие |
| /filters | Управление фильтрами поиска |
| /status | Статистика (фильтры, seen_listings) |
| /help | Справка |
