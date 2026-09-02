from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    frontend_dir: Path
    google_sheet_url: str
    credentials_file: str
    duty_sheet_gid: int | None
    duty_sheet_name: str
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


DEFAULT_DUTY_SHEET_GID = 1262048925
DEFAULT_DUTY_SHEET_NAME = "Новое Дежуство"


def _load_duty_sheet_gid() -> int | None:
    raw_gid = os.getenv("DUTY_SHEET_GID")
    if raw_gid is None:
        return DEFAULT_DUTY_SHEET_GID
    raw_gid = raw_gid.strip()
    if not raw_gid:
        return None
    try:
        return int(raw_gid)
    except ValueError:
        raise ValueError(f"DUTY_SHEET_GID должен быть числом, получено: {raw_gid!r}")


def load_config() -> AppConfig:
    # config.py -> duty_scheduler -> backend -> корень проекта
    project_root = Path(__file__).resolve().parents[2]
    google_sheet_url = os.getenv("GOOGLE_SHEET_URL")
    if not google_sheet_url:
        raise ValueError("GOOGLE_SHEET_URL не установлен в переменных окружения")

    return AppConfig(
        project_root=project_root,
        frontend_dir=Path(os.getenv("FRONTEND_DIR") or project_root / "frontend"),
        google_sheet_url=google_sheet_url,
        credentials_file=os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"),
        duty_sheet_gid=_load_duty_sheet_gid(),
        duty_sheet_name=os.getenv("DUTY_SHEET_NAME", DEFAULT_DUTY_SHEET_NAME),
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
        app_version="2.2.2",
    )
