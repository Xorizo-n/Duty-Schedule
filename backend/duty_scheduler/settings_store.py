"""Runtime-настройки: то, что раньше жило только в переменных окружения.

Watchtower замораживает environment контейнера на момент последнего
`docker compose up -d` (см. AGENTS.md §12), поэтому правка `.env` без ручного
захода на сервер ничего не меняла. Здесь те же значения лежат в JSON-файле,
который приложение читает и пишет само, — правка через `/settings` применяется
на лету и переживает пересоздание контейнера.

Переменные окружения остаются дефолтами: файл настроек их перекрывает, но
только для тех ключей, которые в нём реально заданы.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading


SETTINGS_VERSION = 1
PBKDF2_ITERATIONS = 240_000
SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 6
MIN_UPDATE_INTERVAL = 10

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
TRUE_VALUES = {"1", "true", "yes", "on", "да"}
FALSE_VALUES = {"0", "false", "no", "off", "нет"}

# Значение, которым фронтенд получает уже сохранённый секрет: настоящий токен
# наружу не отдаётся, а прислать его обратно неизменным нужно уметь.
SECRET_PLACEHOLDER = "********"


class SettingsError(ValueError):
    """Ошибка валидации, которую не стыдно показать пользователю."""


@dataclass(frozen=True)
class SettingField:
    key: str
    env: str
    label: str
    kind: str  # text | secret | int | bool | select
    group: str
    hint: str = ""
    choices: tuple[str, ...] = ()
    optional: bool = False


SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField(
        key="google_sheet_url",
        env="GOOGLE_SHEET_URL",
        label="Ссылка на таблицу",
        kind="text",
        group="Google-таблица",
        hint="Полный URL книги, к которой открыт доступ сервисному аккаунту",
    ),
    SettingField(
        key="duty_sheet_gid",
        env="DUTY_SHEET_GID",
        label="gid листа",
        kind="int",
        group="Google-таблица",
        hint="Число из ссылки на лист. Пусто — искать лист по имени",
        optional=True,
    ),
    SettingField(
        key="duty_sheet_name",
        env="DUTY_SHEET_NAME",
        label="Имя листа",
        kind="text",
        group="Google-таблица",
        hint="Запасной поиск, если листа с таким gid нет",
    ),
    SettingField(
        key="vk_bot_token",
        env="VK_BOT_TOKEN",
        label="Токен сообщества",
        kind="secret",
        group="VK-бот",
        hint="Для кнопок нужны права «сообщения» и «управление сообществом»",
        optional=True,
    ),
    SettingField(
        key="vk_peer_id",
        env="VK_PEER_ID",
        label="ID беседы",
        kind="text",
        group="VK-бот",
        hint="Куда шлём уведомления и где отвечаем на кнопки. Пусто — VK выключен",
        optional=True,
    ),
    SettingField(
        key="vk_commands_enabled",
        env="VK_COMMANDS_ENABLED",
        label="Отвечать на кнопки",
        kind="bool",
        group="VK-бот",
        hint="Выключите, если бот должен только рассылать напоминания",
    ),
    SettingField(
        key="vk_group_id",
        env="VK_GROUP_ID",
        label="ID сообщества",
        kind="int",
        group="VK-бот",
        hint="Обычно определяется по токену сам — заполняйте, только если не определился",
        optional=True,
    ),
    SettingField(
        key="vk_api_version",
        env="VK_API_VERSION",
        label="Версия VK API",
        kind="text",
        group="VK-бот",
    ),
    SettingField(
        key="server_timezone",
        env="SERVER_TIMEZONE",
        label="Часовой пояс",
        kind="text",
        group="Время и обновление",
        hint="Имя из базы IANA, например Asia/Yekaterinburg",
    ),
    SettingField(
        key="google_update_interval",
        env="GOOGLE_UPDATE_INTERVAL",
        label="Опрос таблицы, с",
        kind="int",
        group="Время и обновление",
    ),
    SettingField(
        key="ntp_update_interval",
        env="NTP_UPDATE_INTERVAL",
        label="Синхронизация NTP, с",
        kind="int",
        group="Время и обновление",
    ),
    SettingField(
        key="console_log_level",
        env="CONSOLE_LOG_LEVEL",
        label="Уровень лога в консоль",
        kind="select",
        group="Логи",
        choices=LOG_LEVELS,
    ),
    SettingField(
        key="file_log_level",
        env="FILE_LOG_LEVEL",
        label="Уровень лога в файл",
        kind="select",
        group="Логи",
        choices=LOG_LEVELS,
    ),
)

FIELDS_BY_KEY = {setting.key: setting for setting in SETTING_FIELDS}


def default_settings_path(project_root: Path) -> Path:
    """Путь к файлу настроек: SETTINGS_FILE или settings.json в корне."""
    raw_path = (os.getenv("SETTINGS_FILE") or "").strip()
    if not raw_path:
        return project_root / "settings.json"
    path = Path(raw_path)
    return path if path.is_absolute() else project_root / path


def coerce_value(setting: SettingField, raw_value):
    """Приводит присланное значение к типу поля или бросает SettingsError."""
    if setting.kind == "bool":
        if isinstance(raw_value, bool):
            return raw_value
        normalized = str(raw_value).strip().casefold()
        if normalized in TRUE_VALUES:
            return True
        if normalized in FALSE_VALUES:
            return False
        raise SettingsError(f"«{setting.label}»: ожидается да/нет")

    if setting.kind == "int":
        if raw_value is None or str(raw_value).strip() == "":
            if setting.optional:
                return None
            raise SettingsError(f"«{setting.label}»: значение обязательно")
        try:
            value = int(str(raw_value).strip())
        except ValueError:
            raise SettingsError(f"«{setting.label}»: ожидается число")
        if setting.key.endswith("_interval") and value < MIN_UPDATE_INTERVAL:
            raise SettingsError(
                f"«{setting.label}»: не меньше {MIN_UPDATE_INTERVAL} секунд"
            )
        return value

    value = "" if raw_value is None else str(raw_value).strip()

    if setting.kind == "select":
        normalized = value.upper()
        if normalized not in setting.choices:
            raise SettingsError(f"«{setting.label}»: допустимы {', '.join(setting.choices)}")
        return normalized

    if not value and not setting.optional:
        raise SettingsError(f"«{setting.label}»: значение обязательно")

    if setting.key == "server_timezone" and value:
        import pytz

        try:
            pytz.timezone(value)
        except pytz.UnknownTimeZoneError:
            raise SettingsError(f"«{setting.label}»: неизвестный часовой пояс {value!r}")

    return value


class SettingsStore:
    """JSON-файл с паролем, ключом сессий и переопределениями конфига."""

    def __init__(self, path: Path, logger: logging.Logger | None = None) -> None:
        self.path = Path(path)
        self.logger = logger or logging.getLogger(__name__)
        self.lock = threading.RLock()
        self._document: dict | None = None

    # ------------------------------------------------------------------
    # Файл
    # ------------------------------------------------------------------

    def _empty_document(self) -> dict:
        return {"version": SETTINGS_VERSION, "auth": None, "secret_key": None, "values": {}}

    def _load(self) -> dict:
        with self.lock:
            if self._document is not None:
                return self._document

            document = self._empty_document()
            if self.path.exists():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        document.update(loaded)
                        if not isinstance(document.get("values"), dict):
                            document["values"] = {}
                    else:
                        self.logger.error(f"Файл настроек {self.path} должен содержать JSON-объект")
                except Exception as exc:
                    self.logger.error(f"Не удалось прочитать настройки из {self.path}: {exc}")

            self._document = document
            return document

    def _save(self, document: dict) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Пишем во временный файл рядом: оборванная запись не оставит
            # приложение без пароля и без настроек.
            temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            temp_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
            self._document = document

    # ------------------------------------------------------------------
    # Значения
    # ------------------------------------------------------------------

    def overrides(self) -> dict:
        """Только те ключи, которые реально заданы через /settings."""
        values = self._load().get("values", {})
        return {key: value for key, value in values.items() if key in FIELDS_BY_KEY}

    def save_values(self, values: dict) -> dict:
        """Валидирует и сохраняет присланный набор. Возвращает новый набор."""
        if not isinstance(values, dict):
            raise SettingsError("Ожидается объект со значениями настроек")

        with self.lock:
            document = dict(self._load())
            stored = dict(document.get("values", {}))

            for key, raw_value in values.items():
                setting = FIELDS_BY_KEY.get(key)
                if setting is None:
                    continue
                # Плейсхолдер значит «секрет не меняли» — не затираем сохранённый.
                if setting.kind == "secret" and str(raw_value) == SECRET_PLACEHOLDER:
                    continue
                stored[key] = coerce_value(setting, raw_value)

            document["values"] = stored
            document["version"] = SETTINGS_VERSION
            self._save(document)
            return dict(stored)

    # ------------------------------------------------------------------
    # Пароль и сессии
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_password(password: str, salt: bytes, iterations: int) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()

    def has_password(self) -> bool:
        auth = self._load().get("auth")
        return bool(auth and auth.get("hash") and auth.get("salt"))

    def set_password(self, password: str) -> None:
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
            raise SettingsError(f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")

        salt = secrets.token_bytes(SALT_BYTES)
        with self.lock:
            document = dict(self._load())
            document["auth"] = {
                "salt": salt.hex(),
                "hash": self._hash_password(password, salt, PBKDF2_ITERATIONS),
                "iterations": PBKDF2_ITERATIONS,
            }
            self._save(document)

    def verify_password(self, password: str) -> bool:
        auth = self._load().get("auth") or {}
        if not auth.get("hash") or not auth.get("salt"):
            return False
        if not isinstance(password, str):
            return False

        candidate = self._hash_password(
            password,
            bytes.fromhex(auth["salt"]),
            int(auth.get("iterations", PBKDF2_ITERATIONS)),
        )
        return hmac.compare_digest(candidate, auth["hash"])

    def secret_key(self) -> str:
        """Ключ подписи сессий. Сохраняется, чтобы логин переживал рестарт."""
        with self.lock:
            document = dict(self._load())
            existing = document.get("secret_key")
            if existing:
                return existing

            generated = secrets.token_hex(32)
            document["secret_key"] = generated
            try:
                self._save(document)
            except Exception as exc:
                # Файл может быть недоступен на запись — тогда живём одним
                # запуском: сессии просто не переживут рестарт.
                self.logger.warning(f"Не удалось сохранить ключ сессий в {self.path}: {exc}")
            return generated
