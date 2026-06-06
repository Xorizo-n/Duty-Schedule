from __future__ import annotations

from datetime import date, datetime
import logging
import tempfile
import unittest
from unittest.mock import patch

from duty_scheduler.schedule_service import ScheduleService

from tests.helpers import make_config


class ScheduleServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config = make_config(base_dir=self._tmp_path)
        self.service = ScheduleService(self.config, logging.getLogger("schedule-service-test"))

    @property
    def _tmp_path(self):
        from pathlib import Path
        return Path(self.temp_dir.name)

    def test_combine_schedules_merges_morning_and_evening_by_date(self) -> None:
        evening_schedule = [
            {
                "date": date(2026, 6, 8),
                "evening": "Иван Иванов",
                "morning": "",
                "date_str": "08.06.2026",
                "weekday": "ПН",
            }
        ]
        morning_schedule = [
            {
                "date": date(2026, 6, 8),
                "evening": "",
                "morning": "Петр Петров",
                "date_str": "08.06.2026",
                "weekday": "ПН",
            }
        ]

        combined = self.service.combine_schedules(evening_schedule, morning_schedule)

        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0]["morning"], "Петр Петров")
        self.assertEqual(combined[0]["evening"], "Иван Иванов")
        self.assertEqual(combined[0]["weekday"], "ПН")

    def test_build_api_payload_returns_expected_frontend_shape(self) -> None:
        fake_schedule = [
            {
                "date": date(2026, 6, 8),
                "morning": "Петр Петров",
                "evening": "Иван Иванов",
                "date_str": "08.06.2026",
                "weekday": "ПН",
            }
        ]
        self.service.data_cache["schedule"] = fake_schedule
        self.service.data_cache["last_update"] = 1234567890

        fake_now = datetime(2026, 6, 8, 10, 0, 0, tzinfo=self.service.server_tz)
        with patch.object(self.service, "get_current_datetime", return_value=fake_now):
            payload = self.service.build_api_payload()

        self.assertEqual(payload["today"], "2026-06-08")
        self.assertEqual(payload["today_duty"]["morning"], "Петр Петров")
        self.assertEqual(payload["today_duty"]["evening"], "Иван Иванов")
        self.assertEqual(payload["weeks"][0][0]["date"], "2026-06-08")
        self.assertEqual(payload["weeks"][0][0]["date_str"], "08.06")


if __name__ == "__main__":
    unittest.main()
