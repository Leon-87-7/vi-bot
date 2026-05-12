from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    webhook_url: str
    telegram_webhook_secret: str = ""
    telegram_sticker_gemini_fail: str
    telegram_sticker_drive_fail: str
    transcript_url: str = "http://host.docker.internal:5050"
    brave_api_key: str = ""
    google_service_account_json: str = "/app/credentials/service_account.json"
    google_drive_folder_short: str
    google_drive_folder_long: str
    google_sheets_id_short: str
    google_sheets_id_long: str
    db_path: str = "/app/data/jobs.db"
    port: int = 8000
    num_workers: int = 1

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
