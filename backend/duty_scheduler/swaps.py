"""Подмены на дежурство: человек из VK выходит вместо того, кто стоит в графике.

Подмена сначала сохраняется на сервере (`swaps.json` рядом с `settings.json`,
в контейнере — на томе `duty_settings`), затем пишется в Google-таблицу. Пока
таблица её не подтвердила, подмена накладывается поверх каждой синхронизации:
иначе очередное чтение листа вернуло бы в график прежнего человека.

Сверка при синхронизации (`reconcile`), по каждой активной подмене:

* в ячейке уже новый человек — запись дошла, подмена закрыта (`confirmed`);
* в ячейке всё ещё прежний — запись не дошла: подмена действует на табло и в
  боте, запись повторяется не чаще раза в RETRY_SECONDS. Если же запись уже
  проходила (`written`) и с тех пор прошло больше WRITTEN_GRACE_SECONDS,
  значит, ячейку вернули руками: таблица главнее, подмена снимается
  (`reverted`);
* в ячейке кто-то третий — таблицу поправили после подмены, подмена снимается
  (`overridden`);
* дата прошла — подмена уходит в историю (`expired`).

Подмена опознаётся по дате, смене и прежнему человеку, а не по номеру строки:
вставка строки в таблицу её не ломает. Номер строки и колонки берутся из
свежего разбора листа прямо перед записью, а сама запись — «сравнить и
записать»: ячейка перечитывается, и если там уже не прежний человек, ничего
не пишется.
"""

from __future__ import annotations

from datetime import date, datetime, time as day_time, timedelta
import json
import logging
from pathlib import Path
import re
import threading
import time
import uuid

from .config import AppConfig
from .managed_files import atomic_write_text
from .schedule_service import SUNDAY, ScheduleService


RETRY_SECONDS = 300
WRITTEN_GRACE_SECONDS = 600
HISTORY_LIMIT = 200
# Сколько календарных дней вперёд предлагать кнопками (две недели, как на табло).
# Текстом можно ввести любую дату из таблицы.
OFFERED_DAYS = 14

SHIFT_ORDER = {"morning": 0, "evening": 1, "saturday": 2}
# Когда смена заканчивается: сегодняшнюю закончившуюся подменять незачем.
SHIFT_ENDS = {
    "morning": day_time(10, 0),
    "evening": day_time(20, 0),
    "saturday": day_time(16, 0),
}

RESULT_WRITTEN = "written"
RESULT_PENDING = "pending"


class SwapError(Exception):
    """Подмену сделать нельзя; текст исключения — ответ человеку."""


def swaps_path_for(settings_path: Path) -> Path:
    """swaps.json лежит рядом с settings.json — в контейнере это том duty_settings."""
    return settings_path.with_name("swaps.json")


class SwapService:
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        schedule_service: ScheduleService,
        path: Path,
    ) -> None:
        self.config = config
        self.logger = logger
        self.schedule_service = schedule_service
        self.path = path
        # RLock: create_swap держит его на время записи и зовёт методы, которые берут его же.
        self.lock = threading.RLock()

    def apply_config(self, config: AppConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Хранилище
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if not self.path.exists():
            return {"active": [], "history": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.error(f"Не удалось прочитать подмены из {self.path}: {exc}")
            return {"active": [], "history": []}
        if not isinstance(data, dict):
            return {"active": [], "history": []}
        return {
            "active": [swap for swap in data.get("active") or [] if isinstance(swap, dict)],
            "history": [swap for swap in data.get("history") or [] if isinstance(swap, dict)],
        }

    def _save(self, data: dict) -> None:
        data["history"] = data["history"][-HISTORY_LIMIT:]
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2))

    def active_swaps(self) -> list[dict]:
        with self.lock:
            return [dict(swap) for swap in self._load()["active"]]

    def history(self) -> list[dict]:
        with self.lock:
            return [dict(swap) for swap in self._load()["history"]]

    def _close(self, data: dict, swap: dict, outcome: str) -> None:
        data["active"] = [item for item in data["active"] if item.get("id") != swap.get("id")]
        closed = dict(swap)
        closed["outcome"] = outcome
        closed["closed_at"] = self._now().isoformat()
        data["history"].append(closed)

    # ------------------------------------------------------------------
    # Люди и ячейки
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.schedule_service.get_current_datetime()

    def same_person(self, left: str, right: str) -> bool:
        key = self.schedule_service.person_key
        return bool(left) and key(left) == key(right)

    def find_slot(self, record: dict | None, shift: str, name: str) -> dict | None:
        for slot in (record or {}).get("slots") or []:
            if slot.get("shift") == shift and self.same_person(slot.get("name", ""), name):
                return slot
        return None

    def full_name_for(self, name: str) -> str:
        """Полное ФИО из таблицы для «Фамилия Имя» из vk_users.json.

        В ячейку пишем так же, как там записаны остальные, — с отчеством. Если
        человек в таблице ещё не встречался, пишем как есть.
        """
        for record in self.schedule_service.get_schedule_snapshot():
            for slot in record.get("slots") or []:
                if self.same_person(slot.get("name", ""), name):
                    return slot["name"]
        return name

    def replace_in_cell(self, raw_value: str, old_name: str, new_name: str) -> str:
        """Меняет в ячейке одного человека на другого, не трогая остальное.

        Сначала ищется полное ФИО, потом «Фамилия Имя» с отчеством или без —
        так сохраняются соседи по ячейке и пометки вроде «(с 9:00)». Если не
        нашлось ни то, ни другое, ячейка собирается заново из разобранных имён.
        """
        def words(parts: list[str]) -> str:
            # Имя целиком, а не кусок слова: «Иванов Иван» не должен задеть «Иванов Иванович».
            return r"(?<![^\s,])" + r"\s+".join(re.escape(part) for part in parts)

        end = r"(?![^\s,])"
        # Отчество после «Фамилия Имя» — только слово с окончанием отчества,
        # иначе третьим словом можно захватить соседа по ячейке.
        patronymic = r"(?:\s+[^\s,]+?(?:ович|евич|ьевич|овна|евна|ична|инична))?"
        old_words = old_name.split()
        # «Фамилия Имя» без отчества — отчество в ячейке забираем вместе с ним.
        full = words(old_words) + (patronymic if len(old_words) < 3 else "") + end
        for pattern in (full, words(old_words[:2]) + patronymic + end):
            replaced, count = re.subn(pattern, new_name, raw_value, count=1, flags=re.IGNORECASE)
            if count:
                return replaced

        names = self.schedule_service.split_names(raw_value)
        return ", ".join(new_name if self.same_person(name, old_name) else name for name in names)

    # ------------------------------------------------------------------
    # Кого и когда можно подменить
    # ------------------------------------------------------------------

    def _shift_is_over(self, duty_date: date, shift: str, now: datetime) -> bool:
        if duty_date != now.date():
            return duty_date < now.date()
        return now.time() >= SHIFT_ENDS[shift]

    def candidates(self, duty_date: date, caller_name: str, now: datetime | None = None) -> list[dict]:
        """Кого вызывающий может подменить в этот день, в порядке смен."""
        now = now or self._now()
        if duty_date.weekday() == SUNDAY or duty_date < now.date():
            return []

        record = self.schedule_service.get_schedule_entry_by_date(duty_date)
        slots = (record or {}).get("slots") or []
        # На смену, где вызывающий уже стоит, второй раз его не записываем.
        own_shifts = {slot["shift"] for slot in slots if self.same_person(slot["name"], caller_name)}

        result = [
            dict(slot)
            for slot in slots
            if slot["shift"] not in own_shifts and not self._shift_is_over(duty_date, slot["shift"], now)
        ]
        result.sort(key=lambda slot: SHIFT_ORDER.get(slot["shift"], 9))
        return result

    def offered_dates(self, caller_name: str, now: datetime | None = None) -> list[date]:
        """Ближайшие дни, где есть кого подменить: для кнопок выбора даты."""
        now = now or self._now()
        days = (now.date() + timedelta(days=offset) for offset in range(OFFERED_DAYS))
        return [day for day in days if self.candidates(day, caller_name, now)]

    # ------------------------------------------------------------------
    # Наложение на расписание
    # ------------------------------------------------------------------

    def overlay(self, schedule: list[dict], swaps: list[dict] | None = None) -> list[dict]:
        """Расписание с подменами, которых в нём ещё нет. Вход не меняется."""
        swaps = self.active_swaps() if swaps is None else swaps
        if not swaps:
            return schedule

        by_date: dict[str, list[dict]] = {}
        for swap in swaps:
            by_date.setdefault(swap["date"], []).append(swap)

        result = []
        for record in schedule:
            record_swaps = by_date.get(record["date"].isoformat())
            if not record_swaps:
                result.append(record)
                continue

            patched = dict(record)
            patched["slots"] = [dict(slot) for slot in record.get("slots") or []]
            touched = False
            for swap in record_swaps:
                slot = self.find_slot(patched, swap["shift"], swap["old_name"])
                if slot is not None:
                    slot["name"] = swap["new_name"]
                    touched = True

            if touched:
                # Строки для табло и бота собираем заново из ячеек-слотов.
                for field in ("morning", "evening"):
                    shifts = ("saturday", "evening") if field == "evening" else ("morning",)
                    names = [slot["name"] for slot in patched["slots"] if slot["shift"] in shifts]
                    if names:
                        patched[field] = ", ".join(names)
            result.append(patched)
        return result

    def apply_to_cache(self) -> None:
        """Сразу показывает подмены на табло и в боте, не дожидаясь чтения таблицы."""
        # Подмены читаем до лока кэша: reconcile берёт локи в порядке
        # «подмены → кэш», и обратный порядок здесь дал бы взаимную блокировку.
        swaps = self.active_swaps()
        self.schedule_service.replace_schedule(lambda schedule: self.overlay(schedule, swaps))

    # ------------------------------------------------------------------
    # Запись в таблицу
    # ------------------------------------------------------------------

    def _write_cell(self, worksheet, swap: dict, slot: dict) -> str:
        """Сравнить и записать. Возвращает written / conflict."""
        row, col = int(slot["row"]), int(slot["col"])
        current = worksheet.cell(row, col).value or ""
        names = self.schedule_service.split_names(current)
        if not any(self.same_person(name, swap["old_name"]) for name in names):
            if any(self.same_person(name, swap["new_name"]) for name in names):
                return RESULT_WRITTEN
            return "conflict"

        worksheet.update_cell(row, col, self.replace_in_cell(current, swap["old_name"], swap["new_name"]))
        self.logger.info(
            f"Подмена {swap['date']} ({swap['shift']}): '{swap['old_name']}' -> "
            f"'{swap['new_name']}' записана в ячейку R{row}C{col}"
        )
        return RESULT_WRITTEN

    def _mark_failed(self, swap: dict, error: str) -> None:
        swap["attempts"] = int(swap.get("attempts") or 0) + 1
        swap["last_error"] = error
        swap["next_attempt_at"] = time.time() + RETRY_SECONDS

    def create_swap(
        self,
        duty_date: date,
        shift: str,
        old_name: str,
        new_name: str,
        vk_id: int,
    ) -> tuple[dict, str]:
        """Сохраняет подмену и пробует записать её в таблицу.

        Возвращает (подмена, written | pending). pending — сохранена на сервере
        и уже действует, но в таблицу записать не вышло; запись повторится при
        синхронизации. SwapError — подмену делать нельзя (расписание успело
        измениться или такая подмена уже есть).
        """
        with self.lock:
            data = self._load()
            same_shift = [
                existing
                for existing in data["active"]
                if existing["date"] == duty_date.isoformat() and existing["shift"] == shift
            ]
            if any(self.same_person(existing["old_name"], old_name) for existing in same_shift):
                raise SwapError("Этого человека в этот день уже подменили — посмотрите расписание заново.")

            # Подменяют того, кто сам кого-то подменил. Если его подмена ещё не
            # дошла до таблицы, в ячейке по-прежнему исходный человек — его и
            # меняем. Прежняя подмена в любом случае больше не нужна.
            chained = next(
                (existing for existing in same_shift if self.same_person(existing["new_name"], old_name)),
                None,
            )
            if chained is not None:
                if chained.get("status") != RESULT_WRITTEN:
                    old_name = chained["old_name"]
                self._close(data, chained, "replaced")

            swap = {
                "id": uuid.uuid4().hex,
                "date": duty_date.isoformat(),
                "shift": shift,
                "old_name": old_name,
                "new_name": new_name,
                "vk_id": vk_id,
                "created_at": self._now().isoformat(),
                "status": RESULT_PENDING,
                "attempts": 0,
            }

            result = RESULT_PENDING
            try:
                worksheet = self.schedule_service.open_worksheet()
                fresh = self.schedule_service.parse_duty_sheet(worksheet)
                record = next((item for item in fresh if item["date"] == duty_date), None)
                slot = self.find_slot(record, shift, old_name)
                if slot is None:
                    raise SwapError(
                        "В таблице на этот день уже другие дежурные — посмотрите расписание заново."
                    )
                if self._write_cell(worksheet, swap, slot) == "conflict":
                    raise SwapError(
                        "Ячейку в таблице только что изменили — посмотрите расписание заново."
                    )
                swap["status"] = RESULT_WRITTEN
                swap["written_at"] = time.time()
                result = RESULT_WRITTEN
            except SwapError:
                raise
            except Exception as exc:
                self.logger.error(f"Подмена {swap['date']} не записана в таблицу, повторю позже: {exc}")
                self._mark_failed(swap, str(exc))

            data["active"].append(swap)
            self._save(data)

        self.apply_to_cache()
        return swap, result

    # ------------------------------------------------------------------
    # Сверка при синхронизации
    # ------------------------------------------------------------------

    def reconcile(self, schedule: list[dict], worksheet) -> list[dict]:
        """Сверяет подмены со свежим листом, дописывает недошедшие, накладывает остальные."""
        with self.lock:
            data = self._load()
            if not data["active"]:
                return schedule

            today = self._now().date()
            records = {record["date"]: record for record in schedule}
            changed = False

            for swap in list(data["active"]):
                duty_date = date.fromisoformat(swap["date"])
                if duty_date < today:
                    self._close(data, swap, "expired")
                    changed = True
                    continue

                record = records.get(duty_date)
                old_slot = self.find_slot(record, swap["shift"], swap["old_name"])
                new_slot = self.find_slot(record, swap["shift"], swap["new_name"])

                if new_slot is not None and old_slot is None:
                    self._close(data, swap, "confirmed")
                    changed = True
                    continue

                if old_slot is None:
                    self.logger.warning(
                        f"Подмена {swap['date']} снята: в таблице на месте "
                        f"'{swap['old_name']}' теперь другой человек"
                    )
                    self._close(data, swap, "overridden")
                    changed = True
                    continue

                if swap.get("status") == RESULT_WRITTEN:
                    if time.time() - float(swap.get("written_at") or 0) > WRITTEN_GRACE_SECONDS:
                        self.logger.warning(
                            f"Подмена {swap['date']} снята: в таблицу вернули '{swap['old_name']}'"
                        )
                        self._close(data, swap, "reverted")
                        changed = True
                    continue

                if time.time() < float(swap.get("next_attempt_at") or 0):
                    continue

                try:
                    if self._write_cell(worksheet, swap, old_slot) == "conflict":
                        # Ячейку поправили между чтением листа и записью — разберёмся
                        # на следующей синхронизации по свежим данным.
                        self._mark_failed(swap, "ячейка изменилась перед записью")
                    else:
                        swap["status"] = RESULT_WRITTEN
                        swap["written_at"] = time.time()
                        swap.pop("last_error", None)
                except Exception as exc:
                    self.logger.error(f"Подмена {swap['date']} снова не записана в таблицу: {exc}")
                    self._mark_failed(swap, str(exc))
                changed = True

            if changed:
                self._save(data)
            return self.overlay(schedule, data["active"])
