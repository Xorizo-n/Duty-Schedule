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
    vk_commands_enabled: bool
    vk_group_id: int | None
    console_log_level: str
    file_log_level: str
    log_dir: str
    google_update_interval: int
    ntp_update_interval: int
    app_version: str


# config.py -> duty_scheduler -> backend -> корень проекта
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_DUTY_SHEET_GID = 1262048925
DEFAULT_DUTY_SHEET_NAME = "Новое Дежуство"
TRUE_VALUES = {"1", "true", "yes", "on", "да"}


def _load_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    return raw_value.strip().casefold() in TRUE_VALUES


def _load_optional_int(name: str) -> int | None:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return None
    try:
        return int(raw_value.strip())
    except ValueError:
        raise ValueError(f"{name} должен быть числом, получено: {raw_value!r}")


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


def load_config(overrides: dict | None = None) -> AppConfig:
    """Конфиг из окружения, поверх которого ложатся настройки из `/settings`.

    Окружение остаётся источником дефолтов, но перекрывается для каждого ключа,
    который реально задан в файле настроек, — см. `settings_store`.

    Пустой `GOOGLE_SHEET_URL` больше не роняет запуск: без него приложение
    должно подняться хотя бы для того, чтобы ссылку можно было ввести
    в настройках. Отсутствие ссылки превращается в ошибку обновления данных.
    """
    overrides = overrides or {}
    project_root = PROJECT_ROOT

    values = {
        "google_sheet_url": os.getenv("GOOGLE_SHEET_URL", ""),
        "duty_sheet_gid": _load_duty_sheet_gid(),
        "duty_sheet_name": os.getenv("DUTY_SHEET_NAME", DEFAULT_DUTY_SHEET_NAME),
        "server_timezone": os.getenv("SERVER_TIMEZONE", "Asia/Yekaterinburg"),
        "vk_bot_token": os.getenv("VK_BOT_TOKEN"),
        "vk_peer_id": os.getenv("VK_PEER_ID"),
        "vk_api_version": os.getenv("VK_API_VERSION", "5.199"),
        "vk_commands_enabled": _load_bool("VK_COMMANDS_ENABLED", True),
        "vk_group_id": _load_optional_int("VK_GROUP_ID"),
        "console_log_level": os.getenv("CONSOLE_LOG_LEVEL", "INFO").upper(),
        "file_log_level": os.getenv("FILE_LOG_LEVEL", "WARNING").upper(),
        "google_update_interval": int(os.getenv("GOOGLE_UPDATE_INTERVAL", "60")),
        "ntp_update_interval": int(os.getenv("NTP_UPDATE_INTERVAL", "60")),
    }
    values.update({key: value for key, value in overrides.items() if key in values})

    return AppConfig(
        project_root=project_root,
        frontend_dir=Path(os.getenv("FRONTEND_DIR") or project_root / "frontend"),
        credentials_file=os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"),
        vk_users_file=os.getenv("VK_USERS_FILE", "vk_users.json"),
        log_dir=os.getenv("LOG_DIR", "/app/logs"),
        app_version="2.3.0",
        **values,
    )
