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
import threading

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

# Файл пишут двое: страница настроек и бот, дописывающий участников беседы.
VK_USERS_LOCK = threading.Lock()


def _squash_spaces(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _read_vk_users_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SettingsError(f"Не удалось прочитать {path.name}: {exc}")
    if not isinstance(raw, dict):
        raise SettingsError(f"{path.name} должен содержать JSON-объект")
    return raw


def read_vk_users(path: Path) -> list[dict]:
    """Маппинг в виде списка строк формы: `[{name, id, label, auto}]`.

    Оба формата значения из файла («имя: id» и «имя: {id, label, auto}»)
    приводятся к одному виду; при записи формат восстанавливается — см.
    `validate_vk_users`. auto — человек добавлен ботом из беседы и его имя
    ещё никто не проверял.
    """
    users = []
    for name, value in _read_vk_users_file(path).items():
        if isinstance(value, dict):
            users.append(
                {
                    "name": name,
                    "id": value.get("id"),
                    "label": value.get("label") or "",
                    "auto": bool(value.get("auto")),
                }
            )
        else:
            users.append({"name": name, "id": value, "label": "", "auto": False})
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
        value: dict[str, object] = {"id": vk_id}
        if label:
            value["label"] = label
        if item.get("auto") is True:
            value["auto"] = True
        mapping[name] = value if len(value) > 1 else vk_id

    return mapping


def _write_vk_users_file(path: Path, mapping: dict) -> None:
    atomic_write_text(path, json.dumps(mapping, ensure_ascii=False, indent=2) + "\n")


def write_vk_users(path: Path, mapping: dict) -> None:
    with VK_USERS_LOCK:
        _write_vk_users_file(path, mapping)


def add_vk_chat_members(path: Path, members: list[dict]) -> list[str]:
    """Дописывает в vk_users.json участников беседы, которых там ещё нет.

    members — `[{"id": int, "name": "Фамилия Имя"}]` из VK. Кто уже есть по id
    (под любым именем), не трогается: имя в файле мог поправить администратор.
    Новые записи получают пометку auto. Занятое имя (тёзка) — «Имя (id…)»:
    ключи в файле уникальны без учёта регистра. Возвращает добавленные имена.
    """
    with VK_USERS_LOCK:
        mapping = _read_vk_users_file(path)
        known_ids = set()
        for value in mapping.values():
            vk_id = value.get("id") if isinstance(value, dict) else value
            if str(vk_id).strip().isdigit():
                known_ids.add(int(vk_id))
        taken = {name.casefold() for name in mapping}

        added = []
        for member in members:
            vk_id = int(member["id"])
            if vk_id <= 0 or vk_id in known_ids:
                continue
            name = _squash_spaces(member.get("name")) or f"id{vk_id}"
            if name.casefold() in taken:
                name = f"{name} (id{vk_id})"
            mapping[name] = {"id": vk_id, "auto": True}
            known_ids.add(vk_id)
            taken.add(name.casefold())
            added.append(name)

        if added:
            _write_vk_users_file(path, mapping)
        return added


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
