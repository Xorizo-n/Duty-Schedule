from __future__ import annotations

from datetime import date, datetime
import logging
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from duty_scheduler.schedule_service import ScheduleService
from duty_scheduler.swaps import (
    RESULT_PENDING,
    RESULT_WRITTEN,
    WRITTEN_GRACE_SECONDS,
    SwapError,
    SwapService,
)

from tests.helpers import FakeWorksheet, duty_sheet_fixture, make_config


CALLER = "Булатов Иван Олегович"


class SwapServiceTestCase(unittest.TestCase):
    """Фикстура: ПН 31.08 – СБ 12.09. «Сейчас» — среда 02.09, 12:00: утро уже прошло."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.config = make_config(project_root=root)
        logger = logging.getLogger("swaps-test")

        self.service = ScheduleService(self.config, logger)
        self.now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=self.service.server_tz)
        self.start_patch(patch.object(self.service, "get_current_datetime", side_effect=lambda: self.now))

        self.sheet = FakeWorksheet(duty_sheet_fixture())
        self.start_patch(patch.object(self.service, "open_worksheet", side_effect=lambda: self.sheet))

        self.swaps = SwapService(self.config, logger, self.service, root / "swaps.json")
        self.service.swap_service = self.swaps
        self.service.data_cache["schedule"] = self.service.parse_duty_sheet(self.sheet)

    def start_patch(self, patcher) -> None:
        patcher.start()
        self.addCleanup(patcher.stop)

    def sync(self) -> list[dict]:
        """То, что делает update_google_sheets: свежий разбор + сверка с подменами."""
        return self.swaps.reconcile(self.service.parse_duty_sheet(self.sheet), self.sheet)

    def cell(self, row: int, col: int) -> str:
        return self.sheet.cell(row, col).value

    def cached(self, day: date) -> dict:
        return self.service.get_schedule_entry_by_date(day)

    # --- замена имени в ячейке ------------------------------------------

    def test_replace_in_cell_swaps_full_name(self) -> None:
        self.assertEqual(
            self.swaps.replace_in_cell("Пичугин Максим Константинович", "Пичугин Максим Константинович", CALLER),
            CALLER,
        )

    def test_replace_in_cell_keeps_neighbour_and_notes(self) -> None:
        raw = "Козлов Егор Евгеньевич (с 9:00), Козлов Данила Дмитриевич"

        self.assertEqual(
            self.swaps.replace_in_cell(raw, "Козлов Данила", CALLER),
            f"Козлов Егор Евгеньевич (с 9:00), {CALLER}",
        )

    def test_replace_in_cell_does_not_touch_a_longer_name(self) -> None:
        raw = "Козлов Егорий Петрович, Козлов Егор Евгеньевич"

        self.assertEqual(
            self.swaps.replace_in_cell(raw, "Козлов Егор", CALLER),
            f"Козлов Егорий Петрович, {CALLER}",
        )

    # --- кого можно подменить -------------------------------------------

    def test_candidates_skip_finished_shift_of_today(self) -> None:
        candidates = self.swaps.candidates(date(2026, 9, 2), CALLER)

        self.assertEqual([slot["name"] for slot in candidates], ["Пичугин Максим Константинович"])

    def test_candidates_skip_shift_where_caller_already_is(self) -> None:
        # 05.09 (СБ) на смене Козлов Егор и Козлов Данила — Данила не может взять место Егора.
        candidates = self.swaps.candidates(date(2026, 9, 5), "Козлов Данила Дмитриевич")

        self.assertEqual(candidates, [])

    def test_candidates_on_sunday_and_in_the_past_are_empty(self) -> None:
        self.assertEqual(self.swaps.candidates(date(2026, 9, 6), CALLER), [])
        self.assertEqual(self.swaps.candidates(date(2026, 9, 1), CALLER), [])

    def test_offered_dates_start_today_and_skip_sunday(self) -> None:
        dates = self.swaps.offered_dates(CALLER)

        self.assertEqual(dates[0], date(2026, 9, 2))
        self.assertNotIn(date(2026, 9, 6), dates)
        self.assertEqual(dates[-1], date(2026, 9, 12))

    def test_full_name_is_taken_from_the_table(self) -> None:
        self.assertEqual(self.swaps.full_name_for("Булатов Иван"), CALLER)
        self.assertEqual(self.swaps.full_name_for("Новиков Пётр"), "Новиков Пётр")

    # --- создание подмены -----------------------------------------------

    def test_create_swap_writes_cell_and_shows_it_at_once(self) -> None:
        swap, result = self.swaps.create_swap(
            date(2026, 9, 3), "evening", "Афонин Кирилл Борисович", CALLER, 211787018
        )

        self.assertEqual(result, RESULT_WRITTEN)
        self.assertEqual(self.cell(4, 5), CALLER)
        self.assertEqual(self.cached(date(2026, 9, 3))["evening"], CALLER)
        self.assertEqual(self.swaps.active_swaps()[0]["id"], swap["id"])

    def test_create_swap_on_saturday_replaces_only_chosen_person(self) -> None:
        self.swaps.create_swap(date(2026, 9, 5), "saturday", "Козлов Данила Дмитриевич", CALLER, 1)

        self.assertEqual(self.cell(3, 8), "Козлов Егор Евгеньевич")
        self.assertEqual(self.cell(4, 8), CALLER)
        self.assertEqual(self.cached(date(2026, 9, 5))["evening"], f"Козлов Егор Евгеньевич, {CALLER}")

    def test_create_swap_refuses_when_table_changed_meanwhile(self) -> None:
        self.sheet.update_cell(4, 5, "Юрчик")

        with self.assertRaises(SwapError):
            self.swaps.create_swap(date(2026, 9, 3), "evening", "Афонин Кирилл Борисович", CALLER, 1)

        self.assertEqual(self.swaps.active_swaps(), [])

    def test_create_swap_keeps_pending_swap_when_google_is_unavailable(self) -> None:
        with patch.object(self.service, "open_worksheet", side_effect=RuntimeError("503")):
            swap, result = self.swaps.create_swap(
                date(2026, 9, 3), "evening", "Афонин Кирилл Борисович", CALLER, 1
            )

        self.assertEqual(result, RESULT_PENDING)
        self.assertEqual(swap["last_error"], "503")
        self.assertEqual(self.cell(4, 5), "Афонин Кирилл Борисович")
        # Табло и бот уже видят подмену, хотя таблица ещё старая.
        self.assertEqual(self.cached(date(2026, 9, 3))["evening"], CALLER)

    def test_same_person_cannot_be_replaced_twice(self) -> None:
        with patch.object(self.service, "open_worksheet", side_effect=RuntimeError("503")):
            self.swaps.create_swap(date(2026, 9, 3), "evening", "Афонин Кирилл Борисович", CALLER, 1)

        with self.assertRaises(SwapError):
            self.swaps.create_swap(
                date(2026, 9, 3), "evening", "Афонин Кирилл Борисович", "Юрчик", 2
            )

    def test_replacing_a_pending_substitute_targets_the_original_person(self) -> None:
        with patch.object(self.service, "open_worksheet", side_effect=RuntimeError("503")):
            self.swaps.create_swap(date(2026, 9, 3), "evening", "Афонин Кирилл Борисович", CALLER, 1)

        # Теперь Юрчик подменяет того, кто подменял, — в таблице всё ещё Афонин.
        _, result = self.swaps.create_swap(date(2026, 9, 3), "evening", CALLER, "Юрчик", 2)

        self.assertEqual(result, RESULT_WRITTEN)
        self.assertEqual(self.cell(4, 5), "Юрчик")
        self.assertEqual([swap["new_name"] for swap in self.swaps.active_swaps()], ["Юрчик"])
        self.assertEqual(self.swaps.history()[-1]["outcome"], "replaced")

    # --- сверка при синхронизации ---------------------------------------

    def test_sync_closes_swap_confirmed_by_the_table(self) -> None:
        self.swaps.create_swap(date(2026, 9, 3), "evening", "Афонин Кирилл Борисович", CALLER, 1)

        schedule = self.sync()

        self.assertEqual(self.swaps.active_swaps(), [])
        self.assertEqual(self.swaps.history()[-1]["outcome"], "confirmed")
        by_date = {record["date"]: record for record in schedule}
        self.assertEqual(by_date[date(2026, 9, 3)]["evening"], CALLER)

    def test_sync_does_not_bring_back_the_old_person_and_retries_the_write(self) -> None:
        with patch.object(self.service, "open_worksheet", side_effect=RuntimeError("503")):
            self.swaps.create_swap(date(2026, 9, 3), "evening", "Афонин Кирилл Борисович", CALLER, 1)

        # Первая синхронизация — ещё в паузе между попытками: пишем не сразу, но подмена видна.
        schedule = self.sync()
        by_date = {record["date"]: record for record in schedule}
        self.assertEqual(by_date[date(2026, 9, 3)]["evening"], CALLER)
        self.assertEqual(self.sheet.updates, [])

        # Пауза прошла — запись повторяется.
        with patch("duty_scheduler.swaps.time.time", return_value=time.time() + 3600):
            self.sync()
        self.assertEqual(self.cell(4, 5), CALLER)
        self.assertEqual(self.swaps.active_swaps()[0]["status"], RESULT_WRITTEN)

    def test_sync_drops_swap_when_someone_else_was_put_in_by_hand(self) -> None:
        with patch.object(self.service, "open_worksheet", side_effect=RuntimeError("503")):
            self.swaps.create_swap(date(2026, 9, 3), "evening", "Афонин Кирилл Борисович", CALLER, 1)
        self.sheet.update_cell(4, 5, "Юрчик")

        schedule = self.sync()

        by_date = {record["date"]: record for record in schedule}
        self.assertEqual(by_date[date(2026, 9, 3)]["evening"], "Юрчик")
        self.assertEqual(self.swaps.history()[-1]["outcome"], "overridden")

    def test_sync_respects_a_manual_revert_after_the_grace_period(self) -> None:
        self.swaps.create_swap(date(2026, 9, 3), "evening", "Афонин Кирилл Борисович", CALLER, 1)
        self.sheet.update_cell(4, 5, "Афонин Кирилл Борисович")

        # Сразу после записи чтение могло начаться раньше неё — подмену не снимаем.
        schedule = self.sync()
        by_date = {record["date"]: record for record in schedule}
        self.assertEqual(by_date[date(2026, 9, 3)]["evening"], CALLER)

        with patch("duty_scheduler.swaps.time.time", return_value=time.time() + WRITTEN_GRACE_SECONDS + 1):
            schedule = self.sync()
        by_date = {record["date"]: record for record in schedule}
        self.assertEqual(by_date[date(2026, 9, 3)]["evening"], "Афонин Кирилл Борисович")
        self.assertEqual(self.swaps.history()[-1]["outcome"], "reverted")

    def test_past_swaps_go_to_history(self) -> None:
        with patch.object(self.service, "open_worksheet", side_effect=RuntimeError("503")):
            self.swaps.create_swap(date(2026, 9, 3), "evening", "Афонин Кирилл Борисович", CALLER, 1)
        self.now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=self.service.server_tz)

        self.sync()

        self.assertEqual(self.swaps.active_swaps(), [])
        self.assertEqual(self.swaps.history()[-1]["outcome"], "expired")

    def test_swaps_survive_a_restart(self) -> None:
        with patch.object(self.service, "open_worksheet", side_effect=RuntimeError("503")):
            self.swaps.create_swap(date(2026, 9, 3), "evening", "Афонин Кирилл Борисович", CALLER, 1)

        restarted = SwapService(self.config, logging.getLogger("swaps-test"), self.service, self.swaps.path)

        self.assertEqual(restarted.active_swaps()[0]["new_name"], CALLER)


if __name__ == "__main__":
    unittest.main()
