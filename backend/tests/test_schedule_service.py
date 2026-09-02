from __future__ import annotations

from datetime import date, datetime
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from duty_scheduler.schedule_service import ScheduleService

from tests.helpers import FakeWorksheet, duty_sheet_fixture, make_config


class ScheduleServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config = make_config(project_root=Path(self.temp_dir.name))
        self.service = ScheduleService(self.config, logging.getLogger("schedule-service-test"))
        self.reference = datetime(2026, 9, 2, 10, 0, 0, tzinfo=self.service.server_tz)

    def parse_fixture(self) -> dict[date, dict]:
        worksheet = FakeWorksheet(duty_sheet_fixture())
        with patch.object(self.service, "get_current_datetime", return_value=self.reference):
            parsed = self.service.parse_duty_sheet(worksheet)
        return {duty["date"]: duty for duty in parsed}

    def test_parse_duty_sheet_reads_morning_and_evening_from_one_block(self) -> None:
        by_date = self.parse_fixture()

        self.assertEqual(by_date[date(2026, 9, 1)]["morning"], "Булатов Иван Олегович")
        self.assertEqual(by_date[date(2026, 9, 1)]["evening"], "Афонин Кирилл Борисович")
        self.assertEqual(by_date[date(2026, 9, 1)]["weekday"], "ВТ")

    def test_parse_duty_sheet_covers_every_date_of_both_weeks(self) -> None:
        by_date = self.parse_fixture()

        self.assertEqual(len(by_date), 12)
        self.assertEqual(min(by_date), date(2026, 8, 31))
        self.assertEqual(max(by_date), date(2026, 9, 12))
        # Понедельник 31.08 в таблице пуст, но дата все равно разобрана.
        self.assertEqual(by_date[date(2026, 8, 31)]["morning"], "")

    def test_parse_duty_sheet_ignores_time_and_reference_columns(self) -> None:
        names = {
            value
            for duty in self.parse_fixture().values()
            for value in (duty["morning"], duty["evening"])
        }

        self.assertNotIn("с 8:00 до 10:00", names)
        self.assertNotIn("с 17:00 до 20:00", names)
        self.assertNotIn("с 8:00 до 16:00", names)
        self.assertNotIn("мистер х", names)

    def test_parse_duty_sheet_merges_saturday_into_single_shift(self) -> None:
        saturday = self.parse_fixture()[date(2026, 9, 5)]

        self.assertEqual(saturday["morning"], "")
        self.assertEqual(
            saturday["evening"],
            "Козлов Егор Евгеньевич, Козлов Данила Дмитриевич",
        )

    def test_parse_date_cell_picks_year_closest_to_reference(self) -> None:
        self.assertEqual(
            ScheduleService.parse_date_cell("04.01", reference_date=date(2026, 12, 29)),
            date(2027, 1, 4),
        )
        self.assertEqual(
            ScheduleService.parse_date_cell("29.12", reference_date=date(2027, 1, 4)),
            date(2026, 12, 29),
        )
        self.assertEqual(
            ScheduleService.parse_date_cell("02.09", reference_date=date(2026, 9, 2)),
            date(2026, 9, 2),
        )

    def test_clean_name_strips_full_time_range(self) -> None:
        self.assertEqual(
            ScheduleService.clean_name("с 8:00-16:00 Булатов Иван"),
            "Булатов Иван",
        )
        self.assertEqual(
            ScheduleService.clean_name("Толстогузов Никита\nЮртаев Дмитрий"),
            "Толстогузов Никита, Юртаев Дмитрий",
        )

    def test_shorten_name_drops_patronymics_only(self) -> None:
        self.assertEqual(
            ScheduleService.shorten_name("Козлов Данила Дмитриевич"),
            "Козлов Данила",
        )
        self.assertEqual(
            ScheduleService.shorten_name("Козлов Егор Евгеньевич, Козлов Данила Дмитриевич"),
            "Козлов Егор, Козлов Данила",
        )
        # Одно слово и пара «фамилия имя» остаются как есть.
        self.assertEqual(ScheduleService.shorten_name("Юрчик"), "Юрчик")
        self.assertEqual(ScheduleService.shorten_name("Иванович Пётр"), "Иванович Пётр")
        # Легаси-ячейка с двумя людьми без отчеств не должна обрезаться.
        self.assertEqual(
            ScheduleService.shorten_name("Толстогузов Никита Юртаев Дмитрий"),
            "Толстогузов Никита Юртаев Дмитрий",
        )

    def test_build_api_payload_shortens_names_but_cache_keeps_them_full(self) -> None:
        self.service.data_cache["schedule"] = [
            {
                "date": date(2026, 9, 5),
                "morning": "",
                "evening": "Козлов Егор Евгеньевич, Козлов Данила Дмитриевич",
                "date_str": "05.09",
                "weekday": "СБ",
            }
        ]

        with patch.object(self.service, "get_current_datetime", return_value=self.reference):
            payload = self.service.build_api_payload()

        saturday = payload["weeks"][0][5]
        self.assertEqual(saturday["evening"], "Козлов Егор, Козлов Данила")
        # В кэше остаются полные ФИО — их использует VK-нотифаер.
        self.assertEqual(
            self.service.get_schedule_entry_by_date(date(2026, 9, 5))["evening"],
            "Козлов Егор Евгеньевич, Козлов Данила Дмитриевич",
        )

    def test_build_api_payload_returns_expected_frontend_shape(self) -> None:
        self.service.data_cache["schedule"] = [
            {
                "date": date(2026, 9, 2),
                "morning": "Щербатых Кирилл Александрович",
                "evening": "Пичугин Максим Константинович",
                "date_str": "02.09",
                "weekday": "СР",
            }
        ]
        self.service.data_cache["last_update"] = 1234567890

        with patch.object(self.service, "get_current_datetime", return_value=self.reference):
            payload = self.service.build_api_payload()

        self.assertEqual(payload["today"], "2026-09-02")
        self.assertEqual(payload["today_duty"]["morning"], "Щербатых Кирилл")
        self.assertEqual(payload["today_duty"]["evening"], "Пичугин Максим")
        self.assertEqual(payload["weeks"][0][0]["date"], "2026-08-31")
        self.assertEqual(payload["weeks"][0][2]["date_str"], "02.09")


if __name__ == "__main__":
    unittest.main()
