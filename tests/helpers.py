from __future__ import annotations

from pathlib import Path

from duty_scheduler.config import AppConfig


def make_config(base_dir: Path) -> AppConfig:
    return AppConfig(
        base_dir=base_dir,
        google_sheet_url="https://example.com/sheet",
        credentials_file="credentials.json",
        server_timezone="Asia/Yekaterinburg",
        vk_bot_token="token",
        vk_peer_id="123",
        vk_api_version="5.199",
        vk_users_file="vk_users.json",
        console_log_level="INFO",
        file_log_level="WARNING",
        log_dir=str(base_dir / "logs"),
        google_update_interval=60,
        ntp_update_interval=60,
        app_version="2.1.0",
    )
