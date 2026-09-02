from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
import logging
import re
import threading
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from .config import AppConfig
from .schedule_service import PATRONYMIC_PATTERN, SATURDAY, ScheduleService


MORNING_NOTIFICATION_HOUR = 10
EVENING_NOTIFICATION_HOUR = 19
SATURDAY_NOTIFICATION_TYPES = ("saturday_today", "saturday_tomorrow")


class VkNotifier:
    def __init__(self, config: AppConfig, logger: logging.Logger, schedule_service: ScheduleService) -> None:
        self.config = config
        self.logger = logger
        self.schedule_service = schedule_service
        self.start_lock = threading.Lock()
        self.started = False
        self.notifications_lock = threading.Lock()
        self.last_notifications: dict[str, str] = {}

    def start(self) -> None:
        with self.start_lock:
            if self.started:
                return

            thread = threading.Thread(target=self._notification_loop, daemon=True)
            thread.start()
            self.logger.info("Проверка уведомлений VK запущена")
            self.started = True

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

        if user_mapping is None:
            user_mapping = self.load_vk_user_mapping()

        user_info = self.find_user_info(duty_name, user_mapping)
        if user_info is None:
            self.logger.warning(f"Для '{duty_name}' не найден VK id, используем обычное имя")
            return duty_name

        if isinstance(user_info, int) or (isinstance(user_info, str) and str(user_info).isdigit()):
            vk_id = int(user_info)
            label = duty_name
        elif isinstance(user_info, dict):
            vk_id = user_info.get("id")
            label = user_info.get("label", duty_name)
        else:
            self.logger.warning(f"Некорректный формат VK соответствия для '{duty_name}'")
            return duty_name

        if vk_id is None or not str(vk_id).lstrip("-").isdigit():
            self.logger.warning(f"Некорректный VK id для '{duty_name}': {vk_id}")
            return duty_name

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

    def send_vk_message(self, message: str) -> bool:
        if not self.config.vk_bot_token or not self.config.vk_peer_id:
            self.logger.info("VK уведомления отключены: не заданы VK_BOT_TOKEN/VK_PEER_ID")
            return False

        random_id_source = f"{time.time()}:{message}"
        random_id = int(hashlib.md5(random_id_source.encode("utf-8")).hexdigest()[:8], 16)
        params = {
            "access_token": self.config.vk_bot_token,
            "v": self.config.vk_api_version,
            "peer_id": self.config.vk_peer_id,
            "message": message,
            "random_id": random_id,
        }

        try:
            request_url = f"https://api.vk.com/method/messages.send?{urlencode(params)}"
            with urlopen(request_url, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))

            if payload.get("error"):
                self.logger.error(f"VK API ошибка: {payload['error']}")
                return False

            self.logger.info(f"VK уведомление отправлено успешно: {payload.get('response')}")
            return True
        except Exception as exc:
            self.logger.error(f"Ошибка отправки VK уведомления: {exc}")
            return False

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
        if self.send_vk_message(message):
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
