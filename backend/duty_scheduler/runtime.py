"""Пересборка конфига на живом приложении.

Отдельный модуль, а не функция в `__init__`: его импортирует `settings_api`,
который сам импортируется из `__init__`, — прямой импорт был бы циклическим.
"""

from __future__ import annotations

import threading

from flask import Flask

from .config import AppConfig, load_config
from .logging_utils import apply_log_levels


def apply_runtime_config(app: Flask) -> AppConfig:
    """Собирает конфиг из окружения и настроек и раздаёт его сервисам.

    Ничего не перезапускает: и обновитель расписания, и VK-нотифаер читают
    `self.config` на каждой итерации своего цикла, поэтому новые значения
    подхватываются сами.
    """
    settings_store = app.extensions["settings_store"]
    config = load_config(settings_store.overrides())

    app.extensions["config"] = config
    app.config["APP_VERSION"] = config.app_version
    apply_log_levels(app.extensions["logger"], config)
    app.extensions["schedule_service"].apply_config(config)
    app.extensions["vk_notifier"].apply_config(config)
    return config


def refresh_schedule_async(app: Flask) -> None:
    """Внеочередное обновление таблицы после смены настроек.

    В отдельном потоке: HTTP-ответ настроек не должен ждать поход в Google.
    """
    schedule_service = app.extensions["schedule_service"]
    threading.Thread(target=schedule_service.update_google_sheets, daemon=True).start()
