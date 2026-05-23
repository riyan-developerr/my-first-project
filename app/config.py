from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_name: str
    app_base_url: str
    database_url: str
    storage_dir: Path
    admin_username: str
    admin_password: str
    agent_api_key: str
    whatsapp_verify_token: str
    whatsapp_access_token: str
    whatsapp_phone_number_id: str
    whatsapp_api_version: str
    whatsapp_app_secret: str
    payment_instructions: str
    currency: str
    max_upload_mb: int
    retention_days: int


def get_settings() -> Settings:
    _load_dotenv()
    return Settings(
        app_name=os.getenv("APP_NAME", "PrintPilot"),
        app_base_url=os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./data/printpilot.db"),
        storage_dir=Path(os.getenv("STORAGE_DIR", "./data/uploads")),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", "change-this-password"),
        agent_api_key=os.getenv("AGENT_API_KEY", "change-this-agent-key"),
        whatsapp_verify_token=os.getenv("WHATSAPP_VERIFY_TOKEN", "change-this-webhook-token"),
        whatsapp_access_token=os.getenv("WHATSAPP_ACCESS_TOKEN", ""),
        whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""),
        whatsapp_api_version=os.getenv("WHATSAPP_API_VERSION", "v24.0"),
        whatsapp_app_secret=os.getenv("WHATSAPP_APP_SECRET", ""),
        payment_instructions=os.getenv(
            "PAYMENT_INSTRUCTIONS",
            "Please send payment to your shop account and reply with a screenshot or transaction ID.",
        ),
        currency=os.getenv("CURRENCY", "PKR"),
        max_upload_mb=_get_int("MAX_UPLOAD_MB", 100),
        retention_days=_get_int("RETENTION_DAYS", 7),
    )


settings = get_settings()
