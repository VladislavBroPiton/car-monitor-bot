import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
OWNER_ID: int = int(os.environ["OWNER_ID"])
DATABASE_URL: str = os.environ["DATABASE_URL"]

AUTORU_SESSION_ID: str = os.getenv("AUTORU_SESSION_ID", "")
AUTORU_CSRF_TOKEN: str = os.getenv("AUTORU_CSRF_TOKEN", "")

WEBHOOK_HOST: str = os.environ["WEBHOOK_HOST"]
WEBHOOK_PATH: str = "/webhook"
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "supersecret")

APP_HOST: str = "0.0.0.0"
APP_PORT: int = int(os.getenv("PORT", "8000"))
