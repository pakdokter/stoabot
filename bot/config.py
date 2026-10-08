from pydantic_settings import BaseSettings
from pydantic import field_validator
import os


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str
    telegram_admin_ids: str = ""

    # Database
    database_url: str
    database_url_sync: str = ""

    # Redis
    # Postgres induk bersama: schema/search_path, mis. "stoa,shared". Kosong = schema public (lama)
    db_search_path: str = ""

    # Rekening bank yang boleh jadi sumber dana kantong (pisahkan koma). Isi PERSIS sama dengan nama
    # sheet rekening di reconbot, mis. "BRI-567(Biz),Jago". Kosong = tombol bank tidak ditampilkan.
    bank_sumber: str = ""

    redis_url: str = "redis://localhost:6379/0"

    # OCR
    ocr_provider: str = "tesseract"
    google_application_credentials: str = ""

    # Business
    business_name: str = "UMKM"
    business_address: str = ""
    business_phone: str = ""

    # App
    debug: bool = False
    log_level: str = "INFO"
    timezone: str = "Asia/Makassar"

    @property
    def bank_labels(self) -> list[str]:
        return [x.strip() for x in self.bank_sumber.split(",") if x.strip()]

    @property
    def admin_ids(self) -> list[int]:
        if not self.telegram_admin_ids:
            return []
        return [int(x.strip()) for x in self.telegram_admin_ids.split(",") if x.strip()]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
