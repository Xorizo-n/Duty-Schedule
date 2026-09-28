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
from .managed_files import add_vk_chat_members, resolve_path
from .schedule_service import SATURDAY, SUNDAY, ScheduleService
from .settings_store import SettingsError
from .swaps import RESULT_WRITTEN, SwapError, SwapService


MORNING_NOTIFICATION_HOUR = 10
EVENING_NOTIFICATION_HOUR = 19
SATURDAY_NOTIFICATION_TYPES = ("saturday_today", "saturday_tomorrow")

VK_API_URL = "https://api.vk.com/method/"
# peer_id бесед начинается с 2000000000, всё меньше — личные диалоги.
VK_CHAT_PEER_OFFSET = 2_000_000_000
LONGPOLL_WAIT_SECONDS = 25
# Как часто сверять участников беседы со списком vk_users.json. Приход новых
# людей ловится сразу по служебному сообщению — это страховка на пропуски.
CHAT_MEMBERS_SYNC_SECONDS = 3600
CHAT_JOIN_ACTIONS = frozenset({"chat_invite_user", "chat_invite_user_by_link"})

# Подписи кнопок клавиатуры и текстовые синонимы тех же команд.
COMMAND_BUTTONS = (
    ("today", "Сегодня"),
    ("tomorrow", "Завтра"),
    ("week", "Неделя"),
)
# Один цвет на все кнопки: команды равноправны, выделять какую-то незачем.
COMMAND_BUTTON_COLOR = "primary"
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
COMMAND_NAMES = frozenset(COMMAND_ALIASES.values())

# Подмена на дежурство — только в личке. Команды кнопок диалога подмены.
SWAP_COMMANDS = frozenset(
    {"swap", "swap_date", "swap_person", "swap_confirm", "swap_back", "swap_cancel"}
)
SWAP_ALIASES = frozenset({"подмена", "подменить", "замена", "заменить"})
SWAP_CANCEL_ALIASES = frozenset({"отмена", "отменить", "стоп", "cancel"})
SWAP_CONFIRM_ALIASES = frozenset({"подтвердить", "подтверждаю", "да"})
# Сколько ждать следующего шага диалога подмены, прежде чем его забыть.
SWAP_SESSION_SECONDS = 600
VK_BUTTON_LABEL_LIMIT = 40
SWAP_SHIFT_TITLES = {"morning": "утро", "evening": "вечер", "saturday": ""}
# "[club1|@club1] сегодня" или "@duty_bot сегодня" -> "сегодня": упоминание не часть команды.
VK_MENTION_PATTERN = re.compile(r"\[[^\]]*\|[^\]]*\]|@[\w.]+")
# Разметка упоминания сообщества: "[club123|..." — id группы во второй группе.
VK_GROUP_MARKUP_PATTERN = re.compile(r"\[(?:club|public)(\d+)\|", re.IGNORECASE)
# Упоминание, набранное вручную и не превращённое VK в разметку: "@duty_bot".
VK_PLAIN_MENTION_PATTERN = re.compile(r"@([\w.]+)")


class VkNotifier:
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        schedule_service: ScheduleService,
        swap_service: SwapService | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.schedule_service = schedule_service
        self.swap_service = swap_service
        # Незавершённые диалоги подмены: VK id -> шаг и выбранное. Только в памяти:
        # после рестарта диалог начинается заново, это не страшно.
        self.swap_sessions_lock = threading.Lock()
        self.swap_sessions: dict[int, dict] = {}
        self.start_lock = threading.Lock()
        self.started = False
        self.notifications_lock = threading.Lock()
        self.last_notifications: dict[str, str] = {}
        self.longpoll_lock = threading.Lock()
        self.longpoll_state: dict | None = None
        # Кто мы в VK — заполняется при подключении long poll, нужно для
        # распознавания упоминаний бота в беседе.
        self.group_id: int | None = None
        self.group_screen_name: str = ""
        self.last_members_sync = 0.0

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
        # Беседа могла смениться — сверим её участников при переподключении.
        self.last_members_sync = 0.0

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
        # Эвристика общая с разбором листа и подменами — живёт в ScheduleService.
        return self.schedule_service.split_names(duty_name)

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

    @staticmethod
    def _button(label: str, payload: dict) -> dict:
        # Все кнопки бота собираются здесь — поэтому и оформление у них одно.
        return {
            "action": {
                "type": "text",
                "label": label[:VK_BUTTON_LABEL_LIMIT],
                "payload": json.dumps(payload, ensure_ascii=False),
            },
            "color": COMMAND_BUTTON_COLOR,
        }

    @staticmethod
    def _keyboard(rows: list[list[dict]], inline: bool) -> str:
        keyboard: dict = {"inline": inline, "buttons": rows}
        if not inline:
            # Не прятать клавиатуру после нажатия — она нужна постоянно.
            keyboard["one_time"] = False
        return json.dumps(keyboard, ensure_ascii=False)

    def build_keyboard(self, inline: bool = True) -> str | None:
        """Клавиатура «Сегодня / Завтра / Неделя».

        inline=True — кнопки под сообщением бота (беседа). inline=False —
        постоянная клавиатура под полем ввода (личка): VK держит её в диалоге,
        пока её не заменят, поэтому кнопки не повторяются в каждом ответе.
        В личке под основными кнопками — «Подмена».
        """
        if not self.config.vk_commands_enabled:
            return None

        rows = [[self._button(label, {"command": command}) for command, label in COMMAND_BUTTONS]]
        if not inline and self.swap_service is not None:
            rows.append([self._button("Подмена", {"command": "swap"})])
        return self._keyboard(rows, inline)

    @staticmethod
    def parse_payload(payload: str | None) -> dict:
        if not payload:
            return {}
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @classmethod
    def command_from_payload(cls, payload: str | None) -> str | None:
        """Команда из payload нажатой кнопки нашей клавиатуры."""
        command = cls.parse_payload(payload).get("command")
        return command if command in COMMAND_NAMES or command in SWAP_COMMANDS else None

    @staticmethod
    def normalize_text(text: str | None) -> str:
        """Текст без упоминаний и знаков препинания, в нижнем регистре."""
        normalized = VK_MENTION_PATTERN.sub(" ", text or "")
        normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
        return re.sub(r"\s+", " ", normalized).strip().casefold()

    @classmethod
    def resolve_command(cls, payload: str | None, text: str | None) -> str | None:
        """Достаёт команду из payload кнопки, иначе из текста сообщения."""
        button_command = cls.command_from_payload(payload)
        if button_command in COMMAND_NAMES:
            return button_command
        return COMMAND_ALIASES.get(cls.normalize_text(text))

    def format_duty_names(self, duty_name: str) -> str:
        """Дежурные из ячейки таблицы в виде «Фамилия Имя, Фамилия Имя»."""
        names = [
            self.schedule_service.shorten_name(name)
            for name in self.split_duty_names(duty_name)
        ]
        return ", ".join(name for name in names if name)

    def _format_day_answer(self, prefix: str, duty_date: date, header: str | None = None) -> str:
        weekday_label = self.schedule_service.get_weekday_name(duty_date)
        header = header or f"{prefix} ({duty_date.strftime('%d.%m')}, {weekday_label})"

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

    def build_command_answer(self, command: str, private: bool = False) -> str:
        current_date = self.schedule_service.get_current_datetime().date()

        if command == "week":
            return self._format_week_answer()
        if command == "tomorrow":
            return self._format_day_answer("Завтра", current_date + timedelta(days=1))
        if command == "today":
            return self._format_day_answer("Сегодня", current_date)
        answer = (
            "Показываю дежурных по кнопкам ниже.\n"
            "Можно и текстом: «сегодня», «завтра», «неделя»."
        )
        if private and self.swap_service is not None:
            answer += "\n«Подмена» — выйти на дежурство вместо другого."
        return answer

    def allowed_user_ids(self, user_mapping: dict | None = None) -> set[int]:
        """VK id участников из vk_users.json — им бот отвечает в личке."""
        if user_mapping is None:
            user_mapping = self.load_vk_user_mapping()

        user_ids: set[int] = set()
        for value in user_mapping.values():
            vk_id = value.get("id") if isinstance(value, dict) else value
            if isinstance(vk_id, bool):
                continue
            if isinstance(vk_id, int) or (isinstance(vk_id, str) and vk_id.strip().isdigit()):
                if int(vk_id) > 0:
                    user_ids.add(int(vk_id))
        return user_ids

    def is_bot_mentioned(self, text: str | None) -> bool:
        """Упомянут ли в тексте именно наш бот, а не кто-то другой."""
        if not self.group_id or not text:
            return False

        for match in VK_GROUP_MARKUP_PATTERN.finditer(text):
            if int(match.group(1)) == self.group_id:
                return True

        own_names = {f"club{self.group_id}", f"public{self.group_id}"}
        if self.group_screen_name:
            own_names.add(self.group_screen_name.casefold())
        return any(
            match.group(1).casefold() in own_names
            for match in VK_PLAIN_MENTION_PATTERN.finditer(text)
        )

    def handle_command_message(self, message: dict) -> bool:
        """Отвечает на одно входящее сообщение. True — ответ отправлен."""
        peer_id = message.get("peer_id")
        if peer_id is None:
            return False

        # Собственные сообщения группы приходят тем же событием.
        try:
            from_id = int(message.get("from_id") or 0)
        except (TypeError, ValueError):
            return False
        if message.get("out") or from_id <= 0:
            return False

        is_configured_chat = str(peer_id) == str(self.config.vk_peer_id or "")
        # В личке peer_id совпадает с id собеседника; у бесед он от 2000000000.
        is_private = str(peer_id) == str(from_id) and from_id < VK_CHAT_PEER_OFFSET

        if not is_configured_chat:
            if not is_private:
                # Из бесед отвечаем только в настроенной.
                return False
            # График — не публичные данные: в личке отвечаем только участникам
            # из vk_users.json, тому же списку, что используется для упоминаний.
            if from_id not in self.allowed_user_ids():
                self.logger.info(f"Личное сообщение VK от {from_id} проигнорировано: его нет в списке участников")
                return False

        payload, text = message.get("payload"), message.get("text")
        if not is_private and not self.command_from_payload(payload) and not self.is_bot_mentioned(text):
            # В беседе люди говорят между собой: отзываемся только на нажатие
            # кнопки или на сообщение, где бот упомянут. «Сегодня» без
            # упоминания — это просто реплика, а не команда.
            return False

        if self.swap_service is not None:
            if is_private:
                swap_answer = self.handle_swap_message(from_id, payload, text)
                if swap_answer is not None:
                    answer, keyboard = swap_answer
                    return self.send_vk_message(answer, peer_id=peer_id, keyboard=keyboard)
            elif self.is_swap_request(payload, text):
                return self.send_vk_message(
                    "Подмена работает в личных сообщениях — напишите боту «Подмена».",
                    peer_id=peer_id,
                    keyboard=self.build_keyboard(),
                )

        # Сообщение адресовано боту (личка, кнопка или упоминание), но команду
        # не узнали — подсказываем, что он умеет.
        command = self.resolve_command(payload, text) or "help"

        self.logger.info(f"Команда VK '{command}' от {message.get('from_id')}")
        return self.send_vk_message(
            self.build_command_answer(command, private=is_private),
            peer_id=peer_id,
            # В личке — постоянная клавиатура диалога вместо кнопок под ответом.
            keyboard=self.build_keyboard(inline=not is_private),
        )

    # ------------------------------------------------------------------
    # Подмена на дежурство (только личка)
    # ------------------------------------------------------------------

    def is_swap_request(self, payload: str | None, text: str | None) -> bool:
        command = self.command_from_payload(payload)
        return command in SWAP_COMMANDS or self.normalize_text(text) in SWAP_ALIASES

    def caller_name(self, vk_id: int) -> str | None:
        """Как вызывающий записан в таблице: полное ФИО по ключу из vk_users.json."""
        for name, value in self.load_vk_user_mapping().items():
            user_id = value.get("id") if isinstance(value, dict) else value
            if str(user_id).strip() == str(vk_id):
                return self.swap_service.full_name_for(name)
        return None

    def _get_swap_session(self, vk_id: int) -> dict | None:
        with self.swap_sessions_lock:
            session = self.swap_sessions.get(vk_id)
            if session and session["expires"] < time.time():
                del self.swap_sessions[vk_id]
                return None
            return session

    def _save_swap_session(self, vk_id: int, session: dict) -> None:
        session["expires"] = time.time() + SWAP_SESSION_SECONDS
        with self.swap_sessions_lock:
            self.swap_sessions[vk_id] = session

    def _drop_swap_session(self, vk_id: int) -> None:
        with self.swap_sessions_lock:
            self.swap_sessions.pop(vk_id, None)

    def _day_label(self, duty_date: date) -> str:
        return f"{duty_date.strftime('%d.%m')} ({self.schedule_service.get_weekday_name(duty_date)})"

    def _shift_label(self, duty_date: date, shift: str) -> str:
        title = SWAP_SHIFT_TITLES.get(shift, "")
        return f"{self._day_label(duty_date)}, {title}" if title else self._day_label(duty_date)

    def person_button_labels(self, slots: list[dict]) -> list[str]:
        """Подписи кнопок «Козлов Е.»; при совпадении — полнее, чтобы не путать."""
        def initials(name: str) -> str:
            words = self.schedule_service.shorten_name(name).split()
            return f"{words[0]} {words[1][0]}." if len(words) >= 2 else " ".join(words)

        labels = [initials(slot["name"]) for slot in slots]
        if len(set(labels)) < len(labels):
            labels = [self.schedule_service.shorten_name(slot["name"]) for slot in slots]
        if len(set(labels)) < len(labels):
            # Один человек на обеих сменах дня — различаем по смене.
            labels = [
                f"{label} ({SWAP_SHIFT_TITLES[slot['shift']]})" if SWAP_SHIFT_TITLES.get(slot["shift"]) else label
                for label, slot in zip(labels, slots)
            ]
        return labels

    def _swap_dates_keyboard(self, dates: list[date]) -> str:
        buttons = [
            self._button(
                f"{self.schedule_service.get_weekday_name(day)} {day.strftime('%d.%m')}",
                {"command": "swap_date", "date": day.isoformat()},
            )
            for day in dates
        ]
        rows = [buttons[index:index + 3] for index in range(0, len(buttons), 3)]
        rows.append([self._button("Отмена", {"command": "swap_cancel"})])
        return self._keyboard(rows, inline=False)

    def _swap_start(self, vk_id: int, caller: str) -> tuple[str, str]:
        dates = self.swap_service.offered_dates(caller)
        self._save_swap_session(vk_id, {"step": "date", "caller": caller})
        if not dates:
            return (
                "В ближайшие две недели подменять некого. "
                "Можно написать дату текстом, например 03.10.",
                self._swap_dates_keyboard([]),
            )
        return (
            "Подмена: на какой день? Выберите кнопкой или напишите дату, например 03.10.",
            self._swap_dates_keyboard(dates),
        )

    def _swap_pick_date(self, vk_id: int, session: dict, raw_date: str | None) -> tuple[str, str]:
        caller = session["caller"]
        # Пока дата не принята, остаёмся на шаге даты: следующий текст — снова дата.
        session = {"step": "date", "caller": caller}
        self._save_swap_session(vk_id, session)
        now = self.schedule_service.get_current_datetime()
        retry_keyboard = self._swap_dates_keyboard(self.swap_service.offered_dates(caller, now))

        duty_date = None
        if raw_date:
            try:
                duty_date = date.fromisoformat(raw_date)
            except ValueError:
                duty_date = self.schedule_service.parse_date_cell(raw_date.strip(), now.date())
        if duty_date is None:
            return "Не понял дату. Напишите в формате ДД.ММ, например 03.10.", retry_keyboard
        if duty_date < now.date():
            return f"{self._day_label(duty_date)} уже прошло — выберите другой день.", retry_keyboard
        if duty_date.weekday() == SUNDAY:
            return "В воскресенье дежурств нет — выберите другой день.", retry_keyboard

        record = self.schedule_service.get_schedule_entry_by_date(duty_date)
        if not record or not record.get("slots"):
            return f"На {self._day_label(duty_date)} в таблице нет дежурных.", retry_keyboard

        candidates = self.swap_service.candidates(duty_date, caller, now)
        day_text = self._format_day_answer("", duty_date, header=self._day_label(duty_date))
        if not candidates:
            return (
                f"{day_text}\n\nПодменять тут некого: это ваши смены или они уже закончились.",
                retry_keyboard,
            )

        session.update({"step": "person", "date": duty_date.isoformat(), "candidates": candidates})
        self._save_swap_session(vk_id, session)

        labels = self.person_button_labels(candidates)
        buttons = [
            self._button(label, {"command": "swap_person", "date": duty_date.isoformat(), "index": index})
            for index, label in enumerate(labels)
        ]
        rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        rows.append([
            self._button("Другая дата", {"command": "swap_back"}),
            self._button("Отмена", {"command": "swap_cancel"}),
        ])
        return f"{day_text}\n\nКого подменяете?", self._keyboard(rows, inline=False)

    def _swap_pick_person(self, vk_id: int, session: dict, index) -> tuple[str, str] | None:
        candidates = session.get("candidates") or []
        if not isinstance(index, int) or not 0 <= index < len(candidates):
            return None

        slot = candidates[index]
        session.update({"step": "confirm", "slot": slot})
        self._save_swap_session(vk_id, session)

        duty_date = date.fromisoformat(session["date"])
        text = (
            f"Подмена: {self._shift_label(duty_date, slot['shift'])}.\n"
            f"Вместо: {self.schedule_service.shorten_name(slot['name'])}\n"
            f"Дежурит: {self.schedule_service.shorten_name(session['caller'])} (вы)\n"
            "Подтвердить? Изменение сразу запишется в таблицу, в беседе увидят сообщение."
        )
        keyboard = self._keyboard(
            [[
                self._button("Подтвердить", {"command": "swap_confirm"}),
                self._button("Отмена", {"command": "swap_cancel"}),
            ]],
            inline=False,
        )
        return text, keyboard

    def _swap_confirm(self, vk_id: int, session: dict) -> tuple[str, str]:
        self._drop_swap_session(vk_id)
        slot = session["slot"]
        duty_date = date.fromisoformat(session["date"])
        caller = session["caller"]
        main_keyboard = self.build_keyboard(inline=False)

        try:
            swap, result = self.swap_service.create_swap(duty_date, slot["shift"], slot["name"], caller, vk_id)
        except SwapError as exc:
            return str(exc), main_keyboard

        self.logger.info(
            f"Подмена от {vk_id}: {duty_date} ({slot['shift']}) '{slot['name']}' -> '{caller}', {result}"
        )
        where = self._shift_label(duty_date, slot["shift"])
        old_short = self.schedule_service.shorten_name(slot["name"])
        if result == RESULT_WRITTEN:
            answer = f"Готово: {where} дежурите вы вместо {old_short}. В таблицу записано."
            # Перечитываем таблицу: так сервер сразу убедится, что запись дошла.
            threading.Thread(target=self.schedule_service.update_google_sheets, daemon=True).start()
        else:
            answer = (
                f"Подмена сохранена: {where} дежурите вы вместо {old_short}. "
                "Табло и бот её уже показывают, но в таблицу записать пока не получилось — "
                "бот повторит сам."
            )

        if self.config.vk_peer_id and self.config.vk_swap_announce:
            mapping = self.load_vk_user_mapping()
            self.send_vk_message(
                f"Подмена: {where} вместо {self.get_vk_mention(slot['name'], mapping)} "
                f"дежурит {self.get_vk_mention(caller, mapping)}.",
                keyboard=self.build_keyboard(),
            )
        return answer, main_keyboard

    def handle_swap_message(self, vk_id: int, payload: str | None, text: str | None) -> tuple[str, str] | None:
        """Шаг диалога подмены. None — сообщение не про подмену, отвечаем как обычно."""
        data = self.parse_payload(payload)
        command = data.get("command") if data.get("command") in SWAP_COMMANDS else None
        normalized = self.normalize_text(text)
        session = self._get_swap_session(vk_id)

        if command is None:
            if normalized in SWAP_ALIASES:
                command = "swap"
            elif session is None:
                return None
            elif self.resolve_command(payload, text):
                # «Сегодня», «Неделя» посреди подмены — человек передумал.
                self._drop_swap_session(vk_id)
                return None
            elif normalized in SWAP_CANCEL_ALIASES:
                command = "swap_cancel"
            elif session["step"] == "confirm" and normalized in SWAP_CONFIRM_ALIASES:
                command = "swap_confirm"
            elif session["step"] == "date":
                command, data = "swap_date", {"date": (text or "").strip()}
            else:
                return "Выберите вариант кнопкой ниже или нажмите «Отмена».", None

        self.logger.info(f"Подмена VK '{command}' от {vk_id}")
        main_keyboard = self.build_keyboard(inline=False)

        if command == "swap_cancel":
            self._drop_swap_session(vk_id)
            return "Подмена отменена.", main_keyboard

        if session is None or command == "swap":
            caller = self.caller_name(vk_id)
            if not caller:
                return "Не нашёл вас в списке участников — подмена недоступна.", main_keyboard
            if command == "swap_date":
                # Кнопка даты из старого диалога (истёк или бот перезапускался):
                # продолжаем сразу с этой даты.
                session = {"step": "date", "caller": caller}
            elif command in ("swap", "swap_back"):
                return self._swap_start(vk_id, caller)
            else:
                return "Диалог подмены устарел — нажмите «Подмена» ещё раз.", main_keyboard

        if command == "swap_back":
            return self._swap_start(vk_id, session["caller"])

        if command == "swap_date":
            return self._swap_pick_date(vk_id, session, data.get("date"))

        if command == "swap_person":
            answer = None
            if session.get("step") in ("person", "confirm") and data.get("date") == session.get("date"):
                answer = self._swap_pick_person(vk_id, session, data.get("index"))
            return answer or ("Этот выбор устарел — выберите ещё раз.", None)

        if command == "swap_confirm":
            if session.get("step") != "confirm":
                return "Сначала выберите день и человека.", None
            return self._swap_confirm(vk_id, session)

        return None

    # ------------------------------------------------------------------
    # Bots Long Poll
    # ------------------------------------------------------------------

    def _detect_group(self) -> tuple[int, str] | None:
        """id и короткое имя сообщества: id — для long poll, оба — для упоминаний."""
        # С групповым токеном groups.getById без параметров отдаёт саму группу.
        params = {"group_id": self.config.vk_group_id} if self.config.vk_group_id else {}
        response = self.call_api("groups.getById", **params)
        groups = response.get("groups") if isinstance(response, dict) else response
        if isinstance(groups, list) and groups and isinstance(groups[0], dict):
            group_id = groups[0].get("id")
            if group_id:
                return int(group_id), str(groups[0].get("screen_name") or "")

        if self.config.vk_group_id:
            # Упоминание разметкой [club<id>|...] узнаем и без короткого имени.
            return self.config.vk_group_id, ""

        self.logger.error("Не удалось определить id группы VK, задайте VK_GROUP_ID")
        return None

    def _init_longpoll(self) -> dict | None:
        group = self._detect_group()
        if not group:
            return None
        group_id, self.group_screen_name = group
        self.group_id = group_id

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

    # ------------------------------------------------------------------
    # Участники беседы → vk_users.json
    # ------------------------------------------------------------------

    def fetch_chat_members(self) -> list[dict] | None:
        """Люди из беседы VK_PEER_ID: `[{id, name}]`, где name — «Фамилия Имя» со страницы.

        None — VK не отдал список: чаще всего бот не администратор беседы.
        """
        response = self.call_api("messages.getConversationMembers", peer_id=self.config.vk_peer_id)
        if not isinstance(response, dict):
            self.logger.error(
                "Не удалось получить участников беседы VK: сообщество должно быть "
                "администратором беседы"
            )
            return None

        profiles = {
            profile["id"]: profile
            for profile in response.get("profiles") or []
            if isinstance(profile, dict) and isinstance(profile.get("id"), int)
        }
        members = []
        for item in response.get("items") or []:
            member_id = item.get("member_id") if isinstance(item, dict) else None
            # Отрицательные id — сообщества и боты, в том числе мы сами.
            if not isinstance(member_id, int) or member_id <= 0:
                continue
            profile = profiles.get(member_id) or {}
            if profile.get("deactivated"):
                # Удалённые и заблокированные страницы.
                continue
            name = f"{profile.get('last_name', '')} {profile.get('first_name', '')}".strip()
            members.append({"id": member_id, "name": name})
        return members

    def sync_chat_members(self) -> list[str]:
        """Дописывает в vk_users.json участников беседы, которых там нет.

        Так у всех в беседе появляется доступ в личку, а упоминания в
        напоминаниях работают, если имя на странице VK совпало с таблицей. Не
        совпало — администратор правит имя на /settings (там такие помечены).
        Из списка никто не удаляется. Возвращает добавленные имена.
        """
        self.last_members_sync = time.time()
        peer_id = str(self.config.vk_peer_id or "").strip()
        # Участники бывают только у беседы; личный диалог в VK_PEER_ID пропускаем.
        if not self.config.vk_bot_token or not peer_id.isdigit() or int(peer_id) < VK_CHAT_PEER_OFFSET:
            return []

        members = self.fetch_chat_members()
        if members is None:
            return []

        path = resolve_path(self.config.project_root, self.config.vk_users_file)
        try:
            added = add_vk_chat_members(path, members)
        except (SettingsError, OSError) as exc:
            self.logger.error(f"Не удалось дописать участников беседы в {path}: {exc}")
            return []

        if added:
            self.logger.info(f"Из беседы в список участников VK добавлены: {', '.join(added)}")
        return added

    def handle_chat_action(self, message: dict) -> bool:
        """Служебное сообщение беседы. True — это приход человека, список сверен."""
        action = message.get("action") if isinstance(message.get("action"), dict) else {}
        if action.get("type") not in CHAT_JOIN_ACTIONS:
            return False
        if str(message.get("peer_id")) != str(self.config.vk_peer_id or ""):
            return False
        self.sync_chat_members()
        return True

    def _commands_loop(self) -> None:
        while True:
            # Беседа не обязательна: без неё бот всё равно отвечает в личке.
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

                if time.time() - self.last_members_sync >= CHAT_MEMBERS_SYNC_SECONDS:
                    # Первый раз — сразу после подключения, дальше раз в час.
                    self.sync_chat_members()

                for update in self._poll_updates(state):
                    if update.get("type") != "message_new":
                        continue
                    update_object = update.get("object") or {}
                    message = update_object.get("message") or update_object
                    if isinstance(message, dict) and not self.handle_chat_action(message):
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
