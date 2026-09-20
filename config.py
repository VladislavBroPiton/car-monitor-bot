import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
OWNER_ID: int = int(os.environ["OWNER_ID"])
DATABASE_URL: str = os.environ["DATABASE_URL"]

# ScraperAPI — запасной путь к Copart, если с IP дата-центра
# вместо JSON приходит страница защиты. Необязателен.
SCRAPER_API_KEY: str = os.getenv("SCRAPER_API_KEY", "")

# Запасной курс доллара: берётся, только когда ЦБ недоступен (см. rates.py).
# Лоты и фильтры в долларах, курс нужен для пересчёта итоговой суммы в рубли.
USD_RUB_RATE: float = float(os.getenv("USD_RUB_RATE", "90"))

# Через сколько дней после торгов лот считается отторгованным и убирается
# из списка — если пользователь включил автоочистку в настройках.
SOLD_CLEAN_DAYS: int = int(os.getenv("SOLD_CLEAN_DAYS", "3"))

# Сколько лотов максимум слать в чат за один обход одного фильтра.
# Первый прогон нового фильтра иначе высыпает сотни карточек подряд.
MAX_NOTIFY_PER_RUN: int = int(os.getenv("MAX_NOTIFY_PER_RUN", "15"))

# ── Расчёт итоговой стоимости лота Copart ─────────────────────────────────────
# Значения по умолчанию ориентировочные — реальные тарифы задаются через env.
COPART_AUCTION_FEE_PCT: float = float(os.getenv("COPART_AUCTION_FEE_PCT", "10"))
COPART_BROKER_FEE:      float = float(os.getenv("COPART_BROKER_FEE", "700"))
COPART_INLAND_USD:      float = float(os.getenv("COPART_INLAND_USD", "600"))
COPART_OCEAN_USD:       float = float(os.getenv("COPART_OCEAN_USD", "1600"))
COPART_CUSTOMS_PCT:     float = float(os.getenv("COPART_CUSTOMS_PCT", "48"))

WEBHOOK_HOST: str = os.environ["WEBHOOK_HOST"]
WEBHOOK_PATH: str = "/webhook"
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "supersecret")

APP_HOST: str = "0.0.0.0"
APP_PORT: int = int(os.getenv("PORT", "8000"))
