from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import date, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import AppConfig
from .schedule_service import PATRONYMIC_PATTERN, SATURDAY, SUNDAY, ScheduleService


MORNING_NOTIFICATION_HOUR = 10
EVENING_NOTIFICATION_HOUR = 19
SATURDAY_NOTIFICATION_TYPES = ("saturday_today", "saturday_tomorrow")

VK_API_URL = "https://api.vk.com/method/"
LONGPOLL_WAIT_SECONDS = 25

# Подписи кнопок клавиатуры и текстовые синонимы тех же команд.
COMMAND_BUTTONS = (
    ("today", "Сегодня"),
    ("tomorrow", "Завтра"),
    ("week", "Неделя"),
)
COMMAND_ALIASES = {
    "сегодня": "today",
    "today": "today",
    "завтра": "tomorrow",
    "tomorrow": "tomorrow",
    "неделя": "week",
    "неделю": "week",
    "на неделю": "week",
    "week": "week",
    "начать": "help",
    "старт": "help",
    "start": "help",
    "меню": "help",
    "помощь": "help",
    "help": "help",
}
# "[club1|@club1] сегодня" -> "сегодня": упоминание бота не часть команды.
VK_MENTION_PATTERN = re.compile(r"\[[^\]]*\|[^\]]*\]")


class VkNotifier:
    def __init__(self, config: AppConfig, logger: logging.Logger, schedule_service: ScheduleService) -> None:
        self.config = config
        self.logger = logger
        self.schedule_service = schedule_service
        self.start_lock = threading.Lock()
        self.started = False
        self.notifications_lock = threading.Lock()
        self.last_notifications: dict[str, str] = {}
        self.longpoll_lock = threading.Lock()
        self.longpoll_state: dict | None = None

    def start(self) -> None:
        with self.start_lock:
            if self.started:
                return

            threading.Thread(target=self._notification_loop, daemon=True).start()
            self.logger.info("Проверка уведомлений VK запущена")
            threading.Thread(target=self._commands_loop, daemon=True).start()
            self.logger.info("Обработчик команд VK запущен")
            self.started = True

    def apply_config(self, config: AppConfig) -> None:
        """Подхватывает новый конфиг из настроек без перезапуска процесса."""
        self.config = config
        # Токен или группа могли смениться — сессия long poll больше не наша.
        with self.longpoll_lock:
            self.longpoll_state = None

    def _notification_loop(self) -> None:
        while True:
            try:
                self.check_upcoming_duties()
                time.sleep(60)
            except Exception as exc:
                self.logger.error(f"Ошибка notification_checker: {exc}")

    def load_vk_user_mapping(self) -> dict:
        mapping_path = self.config.project_root / self.config.vk_users_file
        if not mapping_path.exists():
            self.logger.warning(f"Файл соответствий VK не найден: {mapping_path}")
            return {}

        try:
            with open(mapping_path, "r", encoding="utf-8") as file:
                mapping = json.load(file)
            if not isinstance(mapping, dict):
                self.logger.error(f"Файл {mapping_path} должен содержать JSON-объект")
                return {}
            return mapping
        except Exception as exc:
            self.logger.error(f"Не удалось загрузить соответствия VK из {mapping_path}: {exc}")
            return {}

    @staticmethod
    def _normalize_name(name: str) -> str:
        return re.sub(r"\s+", " ", str(name)).strip().casefold()

    @classmethod
    def _short_name(cls, name: str) -> str:
        # "Козлов Данила Дмитриевич" -> "козлов данила": в vk_users.json ключи без отчества.
        return " ".join(cls._normalize_name(name).split()[:2])

    @classmethod
    def build_user_lookup(cls, user_mapping: dict) -> dict:
        lookup: dict[str, object] = {}
        for key, value in user_mapping.items():
            for candidate in (cls._normalize_name(key), cls._short_name(key)):
                if candidate:
                    lookup.setdefault(candidate, value)
        return lookup

    def find_user_info(self, duty_name: str, user_mapping: dict):
        lookup = self.build_user_lookup(user_mapping)
        for candidate in (self._normalize_name(duty_name), self._short_name(duty_name)):
            if candidate in lookup:
                return lookup[candidate]
        return None

    def get_vk_mention(self, duty_name: str, user_mapping: dict | None = None) -> str:
        duty_name = self.schedule_service.clean_name(duty_name)
        if not duty_name:
            return ""

        # Ищем по полному ФИО (в vk_users.json ключи бывают и полные, и короткие),
        # а показываем всегда «Фамилия Имя» — отчество в сообщении лишнее.
        display_name = self.schedule_service.shorten_name(duty_name)

        if user_mapping is None:
            user_mapping = self.load_vk_user_mapping()

        user_info = self.find_user_info(duty_name, user_mapping)
        if user_info is None:
            self.logger.warning(f"Для '{duty_name}' не найден VK id, используем обычное имя")
            return display_name

        if isinstance(user_info, int) or (isinstance(user_info, str) and str(user_info).isdigit()):
            vk_id = int(user_info)
            label = display_name
        elif isinstance(user_info, dict):
            vk_id = user_info.get("id")
            label = user_info.get("label", display_name)
        else:
            self.logger.warning(f"Некорректный формат VK соответствия для '{duty_name}'")
            return display_name

        if vk_id is None or not str(vk_id).lstrip("-").isdigit():
            self.logger.warning(f"Некорректный VK id для '{duty_name}': {vk_id}")
            return display_name

        return f"[id{int(vk_id)}|{label}]"

    def split_duty_names(self, duty_name: str) -> list[str]:
        normalized_name = self.schedule_service.clean_name(duty_name)
        if not normalized_name:
            return []

        if "," in normalized_name:
            return [part.strip() for part in normalized_name.split(",") if part.strip()]

        words = normalized_name.split()

        # ФИО с отчествами: "Иванов Иван Иванович Петров Петр Петрович" -> двое.
        groups: list[str] = []
        current: list[str] = []
        for word in words:
            current.append(word)
            if len(current) >= 2 and PATRONYMIC_PATTERN.search(word):
                groups.append(" ".join(current))
                current = []
        if groups and not current:
            return groups

        if len(words) > 2 and len(words) % 2 == 0:
            return [" ".join(words[index:index + 2]) for index in range(0, len(words), 2)]

        return [normalized_name]

    def format_vk_mentions(self, duty_name: str, user_mapping: dict | None = None) -> str:
        duty_names = self.split_duty_names(duty_name)
        if not duty_names:
            return ""

        if user_mapping is None:
            user_mapping = self.load_vk_user_mapping()

        mentions = [self.get_vk_mention(name, user_mapping) for name in duty_names]
        if len(mentions) == 1:
            return mentions[0]
        return ", ".join(mentions[:-1]) + f" и {mentions[-1]}"

    def format_vk_notification(self, notification_type: str, duty_date: date, duty_name: str) -> str:
        user_mapping = self.load_vk_user_mapping()
        duty_names = self.split_duty_names(duty_name)
        duty_label = self.format_vk_mentions(duty_name, user_mapping)
        verb = "дежурят" if len(duty_names) > 1 else "дежурит"
        date_label = duty_date.strftime("%d.%m")
        weekday_label = self.schedule_service.get_weekday_name(duty_date)

        if notification_type in SATURDAY_NOTIFICATION_TYPES:
            # Слово «субботу» уже несёт день недели, дублировать его не нужно.
            prefix = f"В эту субботу ({date_label}) {verb}"
        elif notification_type == "evening_today":
            prefix = f"Сегодня ({date_label}, {weekday_label}) вечером {verb}"
        else:
            prefix = f"Завтра ({date_label}, {weekday_label}) утром {verb}"

        return f"{prefix}: {duty_label}."

    def call_api(self, method: str, timeout: int = 10, **params):
        """Вызов метода VK API. Возвращает поле response либо None при ошибке.

        Параметры уходят телом POST, а не в query string: так токен не оседает
        в логах прокси, а длина сообщения ничем не ограничена.
        """
        if not self.config.vk_bot_token:
            self.logger.info("VK отключён: не задан VK_BOT_TOKEN")
            return None

        request_params = {
            "access_token": self.config.vk_bot_token,
            "v": self.config.vk_api_version,
        }
        request_params.update({key: value for key, value in params.items() if value is not None})

        try:
            request = Request(
                f"{VK_API_URL}{method}",
                data=urlencode(request_params).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            self.logger.error(f"Ошибка запроса к VK ({method}): {exc}")
            return None

        if payload.get("error"):
            self.logger.error(f"VK API ошибка ({method}): {payload['error']}")
            return None
        return payload.get("response")

    def send_vk_message(self, message: str, peer_id: str | int | None = None, keyboard: str | None = None) -> bool:
        peer_id = peer_id if peer_id is not None else self.config.vk_peer_id
        if not self.config.vk_bot_token or not peer_id:
            self.logger.info("VK уведомления отключены: не заданы VK_BOT_TOKEN/VK_PEER_ID")
            return False

        random_id_source = f"{time.time()}:{message}"
        random_id = int(hashlib.md5(random_id_source.encode("utf-8")).hexdigest()[:8], 16)
        response = self.call_api(
            "messages.send",
            peer_id=peer_id,
            message=message,
            random_id=random_id,
            keyboard=keyboard,
        )
        if response is None:
            return False

        self.logger.info(f"VK сообщение отправлено успешно: {response}")
        return True

    # ------------------------------------------------------------------
    # Кнопки и ответы на команды
    # ------------------------------------------------------------------

    def build_keyboard(self) -> str | None:
        """Инлайн-клавиатура «Сегодня / Завтра / Неделя» к сообщению бота."""
        if not self.config.vk_commands_enabled:
            return None

        buttons = [
            {
                "action": {
                    "type": "text",
                    "label": label,
                    "payload": json.dumps({"command": command}, ensure_ascii=False),
                },
                "color": "secondary" if command == "week" else "primary",
            }
            for command, label in COMMAND_BUTTONS
        ]
        return json.dumps({"inline": True, "buttons": [buttons]}, ensure_ascii=False)

    @staticmethod
    def resolve_command(payload: str | None, text: str | None) -> str | None:
        """Достаёт команду из payload кнопки, иначе из текста сообщения."""
        if payload:
            try:
                parsed = json.loads(payload)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict) and parsed.get("command") in set(COMMAND_ALIASES.values()):
                return parsed["command"]

        normalized = VK_MENTION_PATTERN.sub(" ", text or "")
        normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
        normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
        return COMMAND_ALIASES.get(normalized)

    def format_duty_names(self, duty_name: str) -> str:
        """Дежурные из ячейки таблицы в виде «Фамилия Имя, Фамилия Имя»."""
        names = [
            self.schedule_service.shorten_name(name)
            for name in self.split_duty_names(duty_name)
        ]
        return ", ".join(name for name in names if name)

    def _format_day_answer(self, prefix: str, duty_date: date) -> str:
        weekday_label = self.schedule_service.get_weekday_name(duty_date)
        header = f"{prefix} ({duty_date.strftime('%d.%m')}, {weekday_label})"

        if duty_date.weekday() == SUNDAY:
            return f"{header}: воскресенье, дежурных нет."

        duty_entry = self.schedule_service.get_schedule_entry_by_date(duty_date) or {}
        if duty_date.weekday() == SATURDAY:
            # По субботам смена одна, обе фамилии лежат в evening — см. парсер.
            names = self.format_duty_names(duty_entry.get("evening", ""))
            return f"{header}: {names}." if names else f"{header}: дежурные не назначены."

        morning = self.format_duty_names(duty_entry.get("morning", ""))
        evening = self.format_duty_names(duty_entry.get("evening", ""))
        if not morning and not evening:
            return f"{header}: дежурные не назначены."

        lines = [f"{header}:"]
        if morning:
            lines.append(f"Утро: {morning}")
        if evening:
            lines.append(f"Вечер: {evening}")
        return "\n".join(lines)

    def _format_week_answer(self) -> str:
        weeks = self.schedule_service.get_display_weeks()
        if not weeks or not weeks[0]:
            return "Расписание ещё не загружено."

        week = weeks[0]
        period = f"{week[0]['date'].strftime('%d.%m')} — {week[-1]['date'].strftime('%d.%m')}"
        lines = [f"Дежурные на неделю ({period}):"]

        for duty in week:
            duty_date = duty["date"]
            label = f"{duty['weekday']} {duty_date.strftime('%d.%m')}"
            if duty_date.weekday() == SATURDAY:
                names = self.format_duty_names(duty.get("evening", ""))
                lines.append(f"{label} — {names or 'не назначены'}")
                continue

            parts = []
            morning = self.format_duty_names(duty.get("morning", ""))
            evening = self.format_duty_names(duty.get("evening", ""))
            if morning:
                parts.append(f"утро: {morning}")
            if evening:
                parts.append(f"вечер: {evening}")
            lines.append(f"{label} — {'; '.join(parts) if parts else 'не назначены'}")

        return "\n".join(lines)

    def build_command_answer(self, command: str) -> str:
        current_date = self.schedule_service.get_current_datetime().date()

        if command == "week":
            return self._format_week_answer()
        if command == "tomorrow":
            return self._format_day_answer("Завтра", current_date + timedelta(days=1))
        if command == "today":
            return self._format_day_answer("Сегодня", current_date)
        return (
            "Показываю дежурных по кнопкам ниже.\n"
            "Можно и текстом: «сегодня», «завтра», «неделя»."
        )

    def handle_command_message(self, message: dict) -> bool:
        """Отвечает на одно входящее сообщение. True — ответ отправлен."""
        peer_id = message.get("peer_id")
        if peer_id is None or str(peer_id) != str(self.config.vk_peer_id or ""):
            # Отвечаем только в настроенной беседе: график — не публичные данные.
            return False

        # Собственные сообщения группы приходят тем же событием.
        if message.get("out") or int(message.get("from_id") or 0) < 0:
            return False

        command = self.resolve_command(message.get("payload"), message.get("text"))
        if not command:
            return False

        self.logger.info(f"Команда VK '{command}' от {message.get('from_id')}")
        return self.send_vk_message(
            self.build_command_answer(command),
            peer_id=peer_id,
            keyboard=self.build_keyboard(),
        )

    # ------------------------------------------------------------------
    # Bots Long Poll
    # ------------------------------------------------------------------

    def _detect_group_id(self) -> int | None:
        if self.config.vk_group_id:
            return self.config.vk_group_id

        # С групповым токеном groups.getById без параметров отдаёт саму группу.
        response = self.call_api("groups.getById")
        groups = response.get("groups") if isinstance(response, dict) else response
        if isinstance(groups, list) and groups and isinstance(groups[0], dict):
            group_id = groups[0].get("id")
            if group_id:
                return int(group_id)

        self.logger.error("Не удалось определить id группы VK, задайте VK_GROUP_ID")
        return None

    def _init_longpoll(self) -> dict | None:
        group_id = self._detect_group_id()
        if not group_id:
            return None

        # Идемпотентно включаем доставку message_new — без неё long poll молчит.
        self.call_api(
            "groups.setLongPollSettings",
            group_id=group_id,
            enabled=1,
            api_version=self.config.vk_api_version,
            message_new=1,
        )

        server = self.call_api("groups.getLongPollServer", group_id=group_id)
        if not isinstance(server, dict) or not server.get("server"):
            self.logger.error(
                "Не удалось получить long poll сервер VK: "
                "токену нужны права «управление сообществом»"
            )
            return None

        self.logger.info(f"Long poll VK подключён к группе {group_id}")
        return {"server": server["server"], "key": server["key"], "ts": server["ts"]}

    def _poll_updates(self, state: dict) -> list[dict]:
        query = urlencode(
            {
                "act": "a_check",
                "key": state["key"],
                "ts": state["ts"],
                "wait": LONGPOLL_WAIT_SECONDS,
            }
        )
        with urlopen(f"{state['server']}?{query}", timeout=LONGPOLL_WAIT_SECONDS + 15) as response:
            payload = json.loads(response.read().decode("utf-8"))

        failed = payload.get("failed")
        if failed == 1:
            # История устарела — VK сам присылает новый ts.
            state["ts"] = payload.get("ts", state["ts"])
            return []
        if failed:
            # 2 и 3 — ключ или ts протухли: пересоздаём сессию целиком.
            raise RuntimeError(f"long poll failed={failed}")

        state["ts"] = payload.get("ts", state["ts"])
        return payload.get("updates") or []

    def _commands_loop(self) -> None:
        while True:
            if not self.config.vk_commands_enabled or not self.config.vk_bot_token:
                time.sleep(30)
                continue

            try:
                with self.longpoll_lock:
                    state = self.longpoll_state
                if state is None:
                    state = self._init_longpoll()
                    if state is None:
                        time.sleep(60)
                        continue
                    with self.longpoll_lock:
                        self.longpoll_state = state

                for update in self._poll_updates(state):
                    if update.get("type") != "message_new":
                        continue
                    update_object = update.get("object") or {}
                    message = update_object.get("message") or update_object
                    if isinstance(message, dict):
                        self.handle_command_message(message)
            except Exception as exc:
                self.logger.error(f"Ошибка обработчика команд VK: {exc}")
                with self.longpoll_lock:
                    self.longpoll_state = None
                time.sleep(10)

    def _already_sent(self, key: str) -> bool:
        with self.notifications_lock:
            return key in self.last_notifications

    def _mark_sent(self, key: str, value: str) -> None:
        with self.notifications_lock:
            self.last_notifications[key] = value

    def _send_notification(
        self,
        notification_type: str,
        duty_date: date,
        field_name: str,
        sent_at: str,
    ) -> None:
        notification_key = f"{notification_type}:{duty_date.isoformat()}"
        if self._already_sent(notification_key):
            return

        duty_entry = self.schedule_service.get_schedule_entry_by_date(duty_date)
        duty_name = (duty_entry or {}).get(field_name, "").strip()
        if not duty_name:
            self.logger.info(f"На {duty_date} нет дежурства для уведомления ({notification_type})")
            return

        message = self.format_vk_notification(notification_type, duty_date, duty_name)
        if self.send_vk_message(message, keyboard=self.build_keyboard()):
            self._mark_sent(notification_key, sent_at)
            self.logger.info(f"Отправлено уведомление {notification_type} на {duty_date}")

    def check_upcoming_duties(self) -> None:
        current_dt = self.schedule_service.get_current_datetime()
        current_date = current_dt.date()

        if current_dt.minute != 0:
            return

        sent_at = current_dt.isoformat()

        if current_dt.hour == MORNING_NOTIFICATION_HOUR:
            if current_date.weekday() == SATURDAY:
                # Смена уже идёт (с 8:00 до 16:00) — напоминаем о ней в тот же день.
                self._send_notification("saturday_today", current_date, "evening", sent_at)
            else:
                self._send_notification("evening_today", current_date, "evening", sent_at)
            return

        if current_dt.hour == EVENING_NOTIFICATION_HOUR:
            next_date = current_date + timedelta(days=1)
            if next_date.weekday() == SATURDAY:
                self._send_notification("saturday_tomorrow", next_date, "evening", sent_at)
            else:
                self._send_notification("morning_tomorrow", next_date, "morning", sent_at)
