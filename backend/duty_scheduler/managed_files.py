"""Файлы, которыми управляет страница настроек: vk_users.json и credentials.json.

Оба раньше жили только на хосте и монтировались в контейнер `:ro`, поэтому
добавить человека в маппинг VK или подложить ключ Google без захода на сервер
было нельзя. Здесь приложение читает и пишет их само; в контейнере они лежат
на томе `duty_settings` рядом с `settings.json`.

Содержимое `credentials.json` наружу не отдаётся никогда — только признак
наличия и e-mail сервисного аккаунта, чтобы было видно, кому открывать доступ
к таблице.
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import re

from .settings_store import SettingsError


MAX_CREDENTIALS_BYTES = 64 * 1024
CREDENTIALS_REQUIRED_KEYS = ("client_email", "private_key", "token_uri")


def resolve_path(project_root: Path, configured: str) -> Path:
    """Путь из конфига: абсолютный как есть, относительный — от корня проекта."""
    path = Path(configured)
    return path if path.is_absolute() else project_root / path


def describe_path(path: Path) -> dict:
    """Есть ли файл и получится ли его записать.

    Для несуществующего файла проверяется ближайший существующий каталог:
    именно его права решают, создастся ли файл. Так страница честно
    показывает, что старый `:ro`-монтированный файл править нельзя.
    """
    exists = path.is_file()
    if exists:
        writable = os.access(path, os.W_OK)
    else:
        probe = path.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        writable = os.access(probe, os.W_OK)
    return {"path": str(path), "exists": exists, "writable": writable}


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    """Пишет через временный файл рядом: оборванная запись не испортит рабочий."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(temp_path, mode)
    os.replace(temp_path, path)


# ----------------------------------------------------------------------
# vk_users.json
# ----------------------------------------------------------------------

def _squash_spaces(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def read_vk_users(path: Path) -> list[dict]:
    """Маппинг в виде списка строк формы: `[{name, id, label}]`.

    Оба формата значения из файла («имя: id» и «имя: {id, label}») приводятся
    к одному виду; при записи формат восстанавливается — см. `validate_vk_users`.
    """
    if not path.is_file():
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SettingsError(f"Не удалось прочитать {path.name}: {exc}")
    if not isinstance(raw, dict):
        raise SettingsError(f"{path.name} должен содержать JSON-объект")

    users = []
    for name, value in raw.items():
        if isinstance(value, dict):
            users.append({"name": name, "id": value.get("id"), "label": value.get("label") or ""})
        else:
            users.append({"name": name, "id": value, "label": ""})
    return users


def validate_vk_users(raw_users) -> dict:
    """Строки формы → содержимое файла в формате, который читает `VkNotifier`.

    Подпись хранится только когда задана, чтобы файл оставался таким же
    коротким, каким его правили руками.
    """
    if not isinstance(raw_users, list):
        raise SettingsError("Ожидается список участников")

    mapping: dict[str, object] = {}
    seen: set[str] = set()
    for index, item in enumerate(raw_users, start=1):
        if not isinstance(item, dict):
            raise SettingsError(f"Строка {index}: ожидается объект с полями name и id")

        name = _squash_spaces(item.get("name"))
        if not name:
            raise SettingsError(f"Строка {index}: укажите имя")
        key = name.casefold()
        if key in seen:
            raise SettingsError(f"«{name}» указан дважды")
        seen.add(key)

        raw_id = _squash_spaces(item.get("id"))
        if not raw_id.isdigit() or int(raw_id) <= 0:
            raise SettingsError(f"«{name}»: VK id должен быть положительным числом")
        vk_id = int(raw_id)

        label = _squash_spaces(item.get("label"))
        mapping[name] = {"id": vk_id, "label": label} if label else vk_id

    return mapping


def write_vk_users(path: Path, mapping: dict) -> None:
    atomic_write_text(path, json.dumps(mapping, ensure_ascii=False, indent=2) + "\n")


# ----------------------------------------------------------------------
# credentials.json
# ----------------------------------------------------------------------

def describe_credentials(path: Path) -> dict:
    """Статус ключа без его содержимого: есть ли файл и чей это аккаунт."""
    info = describe_path(path)
    info["client_email"] = None
    info["error"] = None
    if info["exists"]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            info["client_email"] = data.get("client_email") if isinstance(data, dict) else None
        except (OSError, ValueError):
            info["error"] = "файл не читается как JSON"
    return info


def validate_credentials(content: bytes) -> str:
    """Проверяет, что прислали ключ сервисного аккаунта, и нормализует его."""
    if not content:
        raise SettingsError("Файл ключа пуст")
    if len(content) > MAX_CREDENTIALS_BYTES:
        raise SettingsError("Файл слишком большой для ключа сервисного аккаунта")

    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SettingsError("Ключ должен быть JSON-файлом сервисного аккаунта Google")

    if not isinstance(data, dict) or data.get("type") != "service_account":
        raise SettingsError("Это не ключ сервисного аккаунта: ожидается \"type\": \"service_account\"")

    missing = [key for key in CREDENTIALS_REQUIRED_KEYS if not data.get(key)]
    if missing:
        raise SettingsError(f"В ключе не хватает полей: {', '.join(missing)}")

    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_credentials(path: Path, text: str) -> None:
    # Приватный ключ — читать его должен только сам процесс.
    atomic_write_text(path, text, mode=0o600)
