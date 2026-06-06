from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    google_sheet_url: str
    credentials_file: str
    server_timezone: str
    vk_bot_token: str | None
    vk_peer_id: str | None
    vk_api_version: str
    vk_users_file: str
    console_log_level: str
    file_log_level: str
    log_dir: str
    google_update_interval: int
    ntp_update_interval: int
    app_version: str


def load_config() -> AppConfig:
    base_dir = Path(__file__).resolve().parent.parent
    google_sheet_url = os.getenv("GOOGLE_SHEET_URL")
    if not google_sheet_url:
        raise ValueError("GOOGLE_SHEET_URL не установлен в переменных окружения")

    return AppConfig(
        base_dir=base_dir,
        google_sheet_url=google_sheet_url,
        credentials_file=os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"),
        server_timezone=os.getenv("SERVER_TIMEZONE", "Asia/Yekaterinburg"),
        vk_bot_token=os.getenv("VK_BOT_TOKEN"),
        vk_peer_id=os.getenv("VK_PEER_ID"),
        vk_api_version=os.getenv("VK_API_VERSION", "5.199"),
        vk_users_file=os.getenv("VK_USERS_FILE", "vk_users.json"),
        console_log_level=os.getenv("CONSOLE_LOG_LEVEL", "INFO").upper(),
        file_log_level=os.getenv("FILE_LOG_LEVEL", "WARNING").upper(),
        log_dir=os.getenv("LOG_DIR", "/app/logs"),
        google_update_interval=int(os.getenv("GOOGLE_UPDATE_INTERVAL", "60")),
        ntp_update_interval=int(os.getenv("NTP_UPDATE_INTERVAL", "60")),
        app_version="2.1.0",
    )
