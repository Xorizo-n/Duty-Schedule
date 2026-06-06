import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .config import AppConfig


def setup_logging(config: AppConfig) -> logging.Logger:
    logger = logging.getLogger()
    logger.handlers.clear()

    console_level = getattr(logging, config.console_log_level, logging.INFO)
    file_level = getattr(logging, config.file_log_level, logging.WARNING)
    logger.setLevel(min(console_level, file_level))

    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
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
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
