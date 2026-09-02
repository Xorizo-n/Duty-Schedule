from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
import re
import socket
import threading
import time

import gspread
from google.oauth2.service_account import Credentials
import ntplib
import pytz

from .config import AppConfig


SATURDAY = 5


class ScheduleService:
    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.server_tz = pytz.timezone(config.server_timezone)
        self.cache_lock = threading.Lock()
        self.start_lock = threading.Lock()
        self.started = False
        self.data_cache = {
            "schedule": None,
            "last_update": 0,
            "error": None,
            "ntp_time": None,
            "ntp_last_sync": 0,
        }

    def start(self) -> None:
        with self.start_lock:
            if self.started:
                return

            updater_thread = threading.Thread(target=self._background_updater, daemon=True)
            updater_thread.start()
            self.logger.info("Фоновый обновитель backend запущен")
            self.started = True

    def _background_updater(self) -> None:
        self.logger.info("Старт фонового обновителя расписания")
        self.update_ntp_time()
        self.update_google_sheets()

        last_google_update = time.time()
        last_ntp_update = time.time()

        while True:
            try:
                current_time = time.time()

                if current_time - last_google_update >= self.config.google_update_interval:
                    self.update_google_sheets()
                    last_google_update = current_time

                if current_time - last_ntp_update >= self.config.ntp_update_interval:
                    self.update_ntp_time()
                    last_ntp_update = current_time

                time.sleep(10)
            except Exception as exc:
                self.logger.error(f"Ошибка фонового обновителя: {exc}")
                time.sleep(10)

    def get_ntp_time(self) -> datetime:
        ntp_servers = [
            "time.google.com",
            "time.windows.com",
            "pool.ntp.org",
            "time.apple.com",
            "ntp1.stratum2.ru",
            "ntp2.stratum2.ru",
        ]

        for ntp_server in ntp_servers:
            try:
                self.logger.info(f"Попытка синхронизации с {ntp_server}...")
                client = ntplib.NTPClient()
                response = client.request(ntp_server, version=3, timeout=5)
                ntp_time = datetime.fromtimestamp(response.tx_time, tz=timezone.utc)
                ntp_time = ntp_time.astimezone(self.server_tz)

                self.logger.info(f"Время синхронизировано с {ntp_server}: {ntp_time.strftime('%H:%M:%S')}")
                self.logger.info(
                    f"Задержка: {response.delay:.3f} сек, Расхождение: {response.offset:.3f} сек"
                )
                return ntp_time
            except (ntplib.NTPException, socket.timeout, socket.gaierror, ConnectionRefusedError) as exc:
                self.logger.warning(f"Не удалось получить время с {ntp_server}: {exc}")
            except Exception as exc:
                self.logger.error(f"Ошибка при запросе к {ntp_server}: {exc}")

        self.logger.error("Не удалось синхронизироваться ни с одним NTP сервером")
        return datetime.now(self.server_tz)

    def update_ntp_time(self) -> None:
        try:
            ntp_time = self.get_ntp_time()
            with self.cache_lock:
                self.data_cache["ntp_time"] = ntp_time
                self.data_cache["ntp_last_sync"] = time.time()
            self.logger.info(f"NTP время обновлено: {ntp_time.strftime('%H:%M:%S')}")
        except Exception as exc:
            self.logger.error(f"Ошибка обновления NTP времени: {exc}")
            with self.cache_lock:
                self.data_cache["ntp_time"] = datetime.now(self.server_tz)
                self.data_cache["ntp_last_sync"] = time.time()

    def get_google_sheets_client(self):
        try:
            # Минимально необходимый доступ: только чтение таблиц.
            # Полный scope drive дал бы сервисному аккаунту запись во весь Drive.
            scope = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
            credentials_path = self.config.base_dir / self.config.credentials_file
            if not credentials_path.exists():
                self.logger.error(f"Файл учетных данных не найден: {credentials_path}")
                return None

            creds = Credentials.from_service_account_file(str(credentials_path), scopes=scope)
            return gspread.authorize(creds)
        except Exception as exc:
            self.logger.error(f"Ошибка при инициализации клиента Google Sheets: {exc}")
            return None

    @staticmethod
    def clean_name(name: str) -> str:
        if not name:
            return ""

        name = re.sub(r"\([^)]*\)", "", name)
        # Диапазон времени целиком: "с 8:00 до 16:00", "с 8:00-16:00", "с 17:00".
        name = re.sub(
            r"с\s*\d{1,2}:\d{2}(\s*(?:до|-|–|—)\s*\d{1,2}:\d{2})?",
            "",
            name,
            flags=re.IGNORECASE,
        )
        name = name.replace("<br>", ", ")
        name = re.sub(r"[\r\n]+", ", ", name)
        name = re.sub(r"\s+", " ", name).strip()
        name = re.sub(r"\s*,\s*", ", ", name)
        return name.strip(" ,")

    @staticmethod
    def is_date_cell(cell_value: str) -> bool:
        if not cell_value:
            return False

        cell_value = str(cell_value).strip()
        date_pattern_full = r"^\d{1,2}\.\d{1,2}\.\d{4}$"
        date_pattern_short = r"^\d{1,2}\.\d{1,2}$"
        return bool(re.match(date_pattern_full, cell_value) or re.match(date_pattern_short, cell_value))

    @staticmethod
    def parse_date_cell(date_str: str, reference_date: date | None = None) -> date | None:
        date_str = str(date_str).strip()

        if re.match(r"^\d{1,2}\.\d{1,2}\.\d{4}$", date_str):
            try:
                return datetime.strptime(date_str, "%d.%m.%Y").date()
            except ValueError:
                return None

        if not re.match(r"^\d{1,2}\.\d{1,2}$", date_str):
            return None

        # Год в таблице не указан: выбираем тот, при котором дата ближе всего
        # к текущей. Иначе неделя на стыке декабря и января разъезжается на год.
        reference = reference_date or date.today()
        day, month = (int(part) for part in date_str.split("."))
        candidates = []
        for year in (reference.year - 1, reference.year, reference.year + 1):
            try:
                candidates.append(date(year, month, day))
            except ValueError:
                continue

        if not candidates:
            return None
        return min(candidates, key=lambda candidate: abs((candidate - reference).days))

    @staticmethod
    def get_weekday_name(date_obj: date) -> str:
        weekdays = {
            0: "ПН",
            1: "ВТ",
            2: "СР",
            3: "ЧТ",
            4: "ПТ",
            5: "СБ",
            6: "ВС",
        }
        return weekdays[date_obj.weekday()]

    @staticmethod
    def _cell(row: list[str] | None, col_idx: int) -> str:
        if row is None or col_idx >= len(row):
            return ""
        return row[col_idx]

    @classmethod
    def _duty_row(cls, all_values: list[list[str]], row_idx: int, date_rows: set[int]) -> list[str] | None:
        # Строка дежурных существует, только если она не является следующей строкой дат.
        if row_idx >= len(all_values) or row_idx in date_rows:
            return None
        return all_values[row_idx]

    def parse_duty_sheet(self, worksheet) -> list[dict]:
        """Разбирает лист блочной структуры: строка дат, под ней 'утро' и 'вечер'.

        Колонки со временем и справочные колонки отсеиваются сами собой: в строке
        дат у них пусто, поэтому такая колонка просто не попадает в разбор.
        """
        all_values = worksheet.get_all_values()
        reference_date = self.get_current_datetime().date()

        date_row_indexes = [
            row_idx
            for row_idx, row in enumerate(all_values)
            if any(self.is_date_cell(cell) for cell in row)
        ]
        date_rows = set(date_row_indexes)

        schedule: dict[date, dict] = {}
        for row_idx in date_row_indexes:
            morning_row = self._duty_row(all_values, row_idx + 1, date_rows)
            evening_row = self._duty_row(all_values, row_idx + 2, date_rows)

            for col_idx, cell_value in enumerate(all_values[row_idx]):
                if not self.is_date_cell(cell_value):
                    continue

                date_value = self.parse_date_cell(cell_value, reference_date)
                if not date_value:
                    continue

                morning = self.clean_name(self._cell(morning_row, col_idx))
                evening = self.clean_name(self._cell(evening_row, col_idx))

                if date_value.weekday() >= SATURDAY:
                    # По субботам смена одна (с 8:00 до 16:00), но людей может быть
                    # двое — в таблице они разнесены по строкам 'утро' и 'вечер'.
                    names = [name for name in (morning, evening) if name]
                    morning, evening = "", ", ".join(names)

                record = schedule.setdefault(
                    date_value,
                    {
                        "date": date_value,
                        "morning": "",
                        "evening": "",
                        "date_str": cell_value.strip(),
                        "weekday": self.get_weekday_name(date_value),
                    },
                )
                record["morning"] = record["morning"] or morning
                record["evening"] = record["evening"] or evening

        parsed = [schedule[date_key] for date_key in sorted(schedule)]
        self.logger.info(f"Разобран лист '{worksheet.title}': {len(parsed)} дат")
        return parsed

    def open_duty_worksheet(self, sheet):
        gid = self.config.duty_sheet_gid
        if gid is not None:
            try:
                return sheet.get_worksheet_by_id(gid)
            except Exception as exc:
                self.logger.warning(f"Лист дежурств с gid={gid} не найден ({exc}), ищу по имени")

        target_title = self.config.duty_sheet_name.strip().casefold()
        for worksheet in sheet.worksheets():
            if worksheet.title.strip().casefold() == target_title:
                return worksheet

        raise ValueError(
            f"Лист дежурств не найден: gid={gid}, имя={self.config.duty_sheet_name!r}"
        )

    def update_google_sheets(self) -> None:
        try:
            self.logger.info("Обновление данных из Google Sheets...")
            client = self.get_google_sheets_client()
            if not client:
                with self.cache_lock:
                    self.data_cache["error"] = "Не удалось инициализировать клиент Google Sheets"
                return

            sheet = client.open_by_url(self.config.google_sheet_url)
            worksheet = self.open_duty_worksheet(sheet)
            schedule = self.parse_duty_sheet(worksheet)

            if not schedule:
                raise ValueError(f"В листе '{worksheet.title}' не найдено ни одной даты")

            with self.cache_lock:
                self.data_cache["schedule"] = schedule
                self.data_cache["last_update"] = time.time()
                self.data_cache["error"] = None

            self.logger.info(f"Данные успешно обновлены. Всего записей: {len(schedule)}")
        except Exception as exc:
            error_message = f"Ошибка при обновлении данных: {exc}"
            with self.cache_lock:
                self.data_cache["error"] = error_message
            self.logger.error(error_message)

    def get_current_datetime(self) -> datetime:
        with self.cache_lock:
            ntp_time = self.data_cache.get("ntp_time")
            ntp_last_sync = self.data_cache.get("ntp_last_sync", 0)

        if ntp_time:
            elapsed_seconds = max(0, time.time() - ntp_last_sync)
            return (ntp_time + timedelta(seconds=elapsed_seconds)).astimezone(self.server_tz)
        return datetime.now(self.server_tz)

    def get_schedule_snapshot(self) -> list[dict]:
        with self.cache_lock:
            schedule = self.data_cache.get("schedule") or []
        return [duty.copy() for duty in schedule]

    def get_schedule_entry_by_date(self, target_date: date) -> dict | None:
        for duty in self.get_schedule_snapshot():
            if duty.get("date") == target_date:
                return duty
        return None

    def get_today_duty(self, schedule_data: list[dict]) -> dict | None:
        today = self.get_current_datetime().date()
        for duty in schedule_data:
            if duty["date"] == today:
                return duty
        return None

    def get_two_work_weeks(self, schedule_data: list[dict]) -> list[list[dict]]:
        today = self.get_current_datetime().date()
        current_week_start = today - timedelta(days=today.weekday())
        if today.weekday() == 6:
            current_week_start = today + timedelta(days=1)

        all_work_days = []
        for week_offset in range(2):
            week_start = current_week_start + timedelta(weeks=week_offset)
            for day_offset in range(6):
                all_work_days.append(week_start + timedelta(days=day_offset))

        schedule_dict = {duty["date"]: duty for duty in schedule_data}
        display_weeks = []
        current_week_data = []

        for work_date in all_work_days:
            duty = schedule_dict.get(work_date)
            display_duty = duty.copy() if duty else {
                "date": work_date,
                "morning": "",
                "evening": "",
                "date_str": work_date.strftime("%d.%m.%Y"),
                "weekday": self.get_weekday_name(work_date),
            }

            current_week_data.append(display_duty)
            if len(current_week_data) == 6:
                display_weeks.append(current_week_data)
                current_week_data = []

        if current_week_data:
            display_weeks.append(current_week_data)

        return display_weeks

    def build_api_payload(self) -> dict:
        with self.cache_lock:
            schedule = self.data_cache.get("schedule") or []
            error = self.data_cache.get("error")
            last_update = self.data_cache.get("last_update", 0)

        today_duty = self.get_today_duty(schedule)
        weeks = self.get_two_work_weeks(schedule)

        weeks_json = []
        for week in weeks:
            week_json = []
            for duty in week:
                week_json.append(
                    {
                        "date": duty["date"].strftime("%Y-%m-%d"),
                        "morning": duty.get("morning", ""),
                        "evening": duty.get("evening", ""),
                        "date_str": duty["date"].strftime("%d.%m"),
                        "weekday": duty["weekday"],
                    }
                )
            weeks_json.append(week_json)

        current_dt = self.get_current_datetime()
        return {
            "today": current_dt.date().strftime("%Y-%m-%d"),
            "today_duty": {
                "morning": today_duty.get("morning", "") if today_duty else "",
                "evening": today_duty.get("evening", "") if today_duty else "",
                "date": today_duty["date"].strftime("%Y-%m-%d") if today_duty else "",
            } if today_duty else None,
            "weeks": weeks_json,
            "error": error,
            "server_time": current_dt.isoformat(),
            "last_updated": last_update,
        }

    def build_health_payload(self) -> dict:
        with self.cache_lock:
            return {
                "status": "healthy",
                "ntp_synced": self.data_cache.get("ntp_time") is not None,
                "data_loaded": self.data_cache.get("schedule") is not None,
                "last_data_update": self.data_cache.get("last_update", 0),
                "last_ntp_sync": self.data_cache.get("ntp_last_sync", 0),
                "timestamp": time.time(),
            }
