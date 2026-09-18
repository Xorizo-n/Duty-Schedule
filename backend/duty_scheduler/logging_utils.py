import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .config import AppConfig


CONSOLE_HANDLER_NAME = "duty-console"
FILE_HANDLER_NAME = "duty-file"


def _levels(config: AppConfig) -> tuple[int, int]:
    console_level = getattr(logging, config.console_log_level, logging.INFO)
    file_level = getattr(logging, config.file_log_level, logging.WARNING)
    return console_level, file_level


def apply_log_levels(logger: logging.Logger, config: AppConfig) -> None:
    """Меняет уровни логирования на живом логгере — без пересоздания хендлеров.

    Хендлеры ищутся по имени: переоткрывать файл лога ради смены уровня незачем,
    а на ротацию это не влияет.
    """
    console_level, file_level = _levels(config)
    logger.setLevel(min(console_level, file_level))

    for handler in logger.handlers:
        if handler.name == CONSOLE_HANDLER_NAME:
            handler.setLevel(console_level)
        elif handler.name == FILE_HANDLER_NAME:
            handler.setLevel(file_level)


def setup_logging(config: AppConfig) -> logging.Logger:
    logger = logging.getLogger()
    logger.handlers.clear()

    console_level, file_level = _levels(config)
    logger.setLevel(min(console_level, file_level))

    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.name = CONSOLE_HANDLER_NAME
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    os.makedirs(config.log_dir, exist_ok=True)
    log_file = os.path.join(config.log_dir, "app.log")
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.name = FILE_HANDLER_NAME
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
