from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from duty_scheduler.schedule_service import ScheduleService
from duty_scheduler.vk_bot import VkNotifier

from tests.helpers import make_config


class VkNotifierTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base_dir = Path(self.temp_dir.name)
        self.config = make_config(base_dir=self.base_dir)
        self.schedule_service = ScheduleService(self.config, logging.getLogger("vk-schedule-service-test"))
        self.notifier = VkNotifier(self.config, logging.getLogger("vk-notifier-test"), self.schedule_service)

    def write_mapping(self, mapping: dict) -> None:
        mapping_path = self.base_dir / self.config.vk_users_file
        mapping_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")

    def test_format_vk_notification_uses_mentions_for_multiple_people(self) -> None:
        self.write_mapping(
            {
                "Иван Иванов": 101,
                "Петр Петров": {"id": 202, "label": "Петр"},
            }
        )

        message = self.notifier.format_vk_notification(
            "saturday_tomorrow",
            datetime(2026, 6, 13).date(),
            "Иван Иванов, Петр Петров",
        )

        self.assertIn("[id101|Иван Иванов]", message)
        self.assertIn("[id202|Петр]", message)
        self.assertIn("дежурят", message)

    def test_check_upcoming_duties_uses_evening_schedule_for_saturday_notification(self) -> None:
        saturday = datetime(2026, 6, 12, 19, 0, 0, tzinfo=self.schedule_service.server_tz)
        duty_entry = {"evening": "Иван Иванов, Петр Петров", "morning": ""}
        self.write_mapping({"Иван Иванов": 101, "Петр Петров": 202})

        with patch.object(self.schedule_service, "get_current_datetime", return_value=saturday), \
             patch.object(self.schedule_service, "get_schedule_entry_by_date", return_value=duty_entry), \
             patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            self.notifier.check_upcoming_duties()

        self.assertTrue(send_vk_message.called)
        sent_message = send_vk_message.call_args[0][0]
        self.assertIn("в субботу", sent_message)
        self.assertIn("[id101|Иван Иванов]", sent_message)
        self.assertIn("[id202|Петр Петров]", sent_message)


if __name__ == "__main__":
    unittest.main()
