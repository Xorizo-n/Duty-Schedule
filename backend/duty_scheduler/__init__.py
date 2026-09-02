import threading

from flask import Flask

from .api import api_bp
from .config import AppConfig, load_config
from .views import views_bp
from .logging_utils import setup_logging
from .schedule_service import ScheduleService
from .vk_bot import VkNotifier


_worker_lock = threading.Lock()
_workers_started = False


def create_app() -> Flask:
    config = load_config()
    logger = setup_logging(config)

    app = Flask(
        __name__,
        template_folder=str(config.frontend_dir / "templates"),
        static_folder=str(config.frontend_dir / "static"),
    )
    app.config["APP_VERSION"] = config.app_version

    schedule_service = ScheduleService(config, logger)
    vk_notifier = VkNotifier(config, logger, schedule_service)

    app.extensions["config"] = config
    app.extensions["logger"] = logger
    app.extensions["schedule_service"] = schedule_service
    app.extensions["vk_notifier"] = vk_notifier

    app.register_blueprint(api_bp)
    app.register_blueprint(views_bp)
    return app


def start_background_workers(app: Flask) -> None:
    global _workers_started

    with _worker_lock:
        if _workers_started:
            return

        schedule_service: ScheduleService = app.extensions["schedule_service"]
        vk_notifier: VkNotifier = app.extensions["vk_notifier"]
        schedule_service.start()
        vk_notifier.start()
        _workers_started = True


def get_config(app: Flask) -> AppConfig:
    return app.extensions["config"]
