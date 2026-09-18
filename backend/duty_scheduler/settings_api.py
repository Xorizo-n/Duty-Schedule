"""HTTP-ручки страницы настроек.

Аутентификация нарочно простая: приложение живёт в доверенной локальной сети,
и единственное, от чего защищаемся, — случайная правка с чужого табло. Пароль
задаётся при первом обращении и дальше спрашивается при каждом входе.
"""

from __future__ import annotations

import threading
import time

from flask import Blueprint, current_app, jsonify, request, session

from .managed_files import (
    describe_credentials,
    describe_path,
    read_vk_users,
    resolve_path,
    validate_credentials,
    validate_vk_users,
    write_credentials,
    write_vk_users,
)
from .runtime import apply_runtime_config, refresh_schedule_async
from .settings_store import (
    SECRET_PLACEHOLDER,
    SETTING_FIELDS,
    SettingsError,
    SettingsStore,
)


settings_api_bp = Blueprint("settings_api", __name__)

SESSION_KEY = "settings_authenticated"
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300
# Пауза на неудачной попытке: перебор по сети становится бессмысленным
# задолго до того, как упрётся в лимит попыток.
FAILED_ATTEMPT_DELAY = 0.5


class LoginGuard:
    """Счётчик неудачных попыток входа с временной блокировкой по адресу."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.failures: dict[str, tuple[int, float]] = {}

    def lockout_seconds(self, key: str) -> int:
        with self.lock:
            attempts, blocked_until = self.failures.get(key, (0, 0.0))
        if attempts < LOGIN_MAX_ATTEMPTS:
            return 0
        return max(0, int(blocked_until - time.time()))

    def register_failure(self, key: str) -> None:
        with self.lock:
            attempts, _ = self.failures.get(key, (0, 0.0))
            attempts += 1
            self.failures[key] = (attempts, time.time() + LOGIN_LOCKOUT_SECONDS)

    def reset(self, key: str) -> None:
        with self.lock:
            self.failures.pop(key, None)


def get_store() -> SettingsStore:
    return current_app.extensions["settings_store"]


def get_guard() -> LoginGuard:
    guard = current_app.extensions.get("settings_login_guard")
    if guard is None:
        guard = LoginGuard()
        current_app.extensions["settings_login_guard"] = guard
    return guard


def is_authenticated() -> bool:
    return bool(session.get(SESSION_KEY))


def json_body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def describe_settings() -> list[dict]:
    """Поля, сгруппированные для формы, со значениями из действующего конфига.

    Значение берётся из конфига, а не из файла настроек: так в форме видно то,
    что реально работает, включая дефолты из окружения. Источник значения
    подсказывает, переживёт ли оно пересоздание контейнера.
    """
    config = current_app.extensions["config"]
    overrides = get_store().overrides()

    groups: dict[str, dict] = {}
    for setting in SETTING_FIELDS:
        value = getattr(config, setting.key, None)
        if setting.kind == "secret":
            # Настоящий токен наружу не отдаём — только признак, что он задан.
            value = SECRET_PLACEHOLDER if value else ""
        elif setting.kind == "bool":
            value = bool(value)
        elif value is None:
            value = ""

        group = groups.setdefault(setting.group, {"title": setting.group, "fields": []})
        group["fields"].append(
            {
                "key": setting.key,
                "label": setting.label,
                "kind": setting.kind,
                "hint": setting.hint,
                "choices": list(setting.choices),
                "optional": setting.optional,
                "env": setting.env,
                "value": value,
                "source": "settings" if setting.key in overrides else "env",
            }
        )

    return list(groups.values())


@settings_api_bp.route("/api/settings/session")
def settings_session():
    return jsonify(
        {
            "password_set": get_store().has_password(),
            "authenticated": is_authenticated(),
        }
    )


@settings_api_bp.route("/api/settings/password", methods=["POST"])
def settings_password():
    store = get_store()
    body = json_body()
    password = body.get("password") or ""

    if store.has_password():
        # Смена пароля — только зная текущий или уже войдя в настройки.
        if not is_authenticated() and not store.verify_password(body.get("current_password") or ""):
            return jsonify({"success": False, "error": "Текущий пароль неверен"}), 403

    try:
        store.set_password(password)
    except SettingsError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    session[SESSION_KEY] = True
    get_guard().reset(request.remote_addr or "")
    return jsonify({"success": True, "password_set": True, "authenticated": True})


@settings_api_bp.route("/api/settings/login", methods=["POST"])
def settings_login():
    store = get_store()
    if not store.has_password():
        return jsonify({"success": False, "error": "Пароль ещё не задан"}), 409

    guard = get_guard()
    client_key = request.remote_addr or ""
    lockout = guard.lockout_seconds(client_key)
    if lockout:
        return (
            jsonify({"success": False, "error": f"Слишком много попыток, подождите {lockout} с"}),
            429,
        )

    if not store.verify_password(json_body().get("password") or ""):
        guard.register_failure(client_key)
        time.sleep(FAILED_ATTEMPT_DELAY)
        current_app.extensions["logger"].warning(f"Неверный пароль настроек с {client_key}")
        return jsonify({"success": False, "error": "Неверный пароль"}), 403

    guard.reset(client_key)
    session[SESSION_KEY] = True
    return jsonify({"success": True, "authenticated": True})


@settings_api_bp.route("/api/settings/logout", methods=["POST"])
def settings_logout():
    session.pop(SESSION_KEY, None)
    return jsonify({"success": True, "authenticated": False})


@settings_api_bp.route("/api/settings")
def settings_read():
    if not is_authenticated():
        return jsonify({"success": False, "error": "Требуется вход"}), 401
    return jsonify({"success": True, "groups": describe_settings()})


@settings_api_bp.route("/api/settings", methods=["POST"])
def settings_write():
    if not is_authenticated():
        return jsonify({"success": False, "error": "Требуется вход"}), 401

    try:
        get_store().save_values(json_body().get("values") or {})
    except SettingsError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    apply_runtime_config(current_app._get_current_object())
    refresh_schedule_async(current_app._get_current_object())
    current_app.extensions["logger"].info("Настройки обновлены через /settings")

    return jsonify({"success": True, "groups": describe_settings()})


# ----------------------------------------------------------------------
# Файлы: участники VK и ключ Google
# ----------------------------------------------------------------------

def vk_users_path():
    config = current_app.extensions["config"]
    return resolve_path(config.project_root, config.vk_users_file)


def credentials_path():
    config = current_app.extensions["config"]
    return resolve_path(config.project_root, config.credentials_file)


def describe_vk_users() -> dict:
    path = vk_users_path()
    return {"success": True, "file": describe_path(path), "users": read_vk_users(path)}


@settings_api_bp.route("/api/settings/vk-users")
def vk_users_read():
    if not is_authenticated():
        return jsonify({"success": False, "error": "Требуется вход"}), 401
    try:
        return jsonify(describe_vk_users())
    except SettingsError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@settings_api_bp.route("/api/settings/vk-users", methods=["POST"])
def vk_users_write():
    """Заменяет список целиком: форма присылает всё, что в ней видно."""
    if not is_authenticated():
        return jsonify({"success": False, "error": "Требуется вход"}), 401

    path = vk_users_path()
    try:
        mapping = validate_vk_users(json_body().get("users"))
        write_vk_users(path, mapping)
    except SettingsError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except OSError as exc:
        return jsonify({"success": False, "error": f"Не удалось записать {path}: {exc}"}), 500

    # Файл читается заново на каждое сообщение — перезапуск не нужен.
    current_app.extensions["logger"].info(
        f"Список участников VK обновлён через /settings: {len(mapping)} чел."
    )
    return jsonify(describe_vk_users())


@settings_api_bp.route("/api/settings/credentials")
def credentials_read():
    if not is_authenticated():
        return jsonify({"success": False, "error": "Требуется вход"}), 401
    return jsonify({"success": True, "file": describe_credentials(credentials_path())})


@settings_api_bp.route("/api/settings/credentials", methods=["POST"])
def credentials_write():
    """Загрузка ключа: multipart-поле `file` или JSON `{content}`.

    Существующий ключ перезаписывается только с явным `replace`, чтобы
    случайный клик не подменил рабочий доступ к таблице.
    """
    if not is_authenticated():
        return jsonify({"success": False, "error": "Требуется вход"}), 401

    path = credentials_path()
    upload = request.files.get("file")
    if upload is not None:
        content = upload.read()
        replace = request.form.get("replace", "").strip().casefold() in {"1", "true", "yes", "on"}
    else:
        body = json_body()
        content = str(body.get("content") or "").encode("utf-8")
        replace = bool(body.get("replace"))

    if path.is_file() and not replace:
        return jsonify({"success": False, "error": "Ключ уже загружен. Чтобы заменить, подтвердите замену"}), 409

    try:
        text = validate_credentials(content)
        write_credentials(path, text)
    except SettingsError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except OSError as exc:
        return jsonify({"success": False, "error": f"Не удалось записать {path}: {exc}"}), 500

    # Клиент Google создаётся на каждое обновление, так что новый ключ
    # подхватится первым же походом в таблицу — запускаем его сразу.
    refresh_schedule_async(current_app._get_current_object())
    current_app.extensions["logger"].info("Ключ сервисного аккаунта Google загружен через /settings")
    return jsonify({"success": True, "file": describe_credentials(path)})
