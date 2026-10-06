"""
Bootstrap configuration from environment variables.
These are only *seed* values: after first start, everything is managed in the
dashboard and stored in SQLite (persisted on the /app/data volume).
"""
import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8004"))
    DB_PATH: str = os.getenv("DB_PATH", "/app/data/codecraft.db")

    # Seeds (only used when the DB is created for the first time)
    API_KEY: str = os.getenv("API_KEY", "sk-codecraft-change-me")
    DASHBOARD_PASSWORD: str = os.getenv("DASHBOARD_PASSWORD", "admin")

    # Optional: seed first account from env (same constants as the original script)
    CF_CLEARANCE: str = os.getenv("CF_CLEARANCE", "")
    REMEMBER_WEB_NAME: str = os.getenv("REMEMBER_WEB_NAME", "")
    REMEMBER_WEB_VALUE: str = os.getenv("REMEMBER_WEB_VALUE", "")


settings = Settings()
