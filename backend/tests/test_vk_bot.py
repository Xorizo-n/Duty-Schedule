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
        self.project_root = Path(self.temp_dir.name)
        self.config = make_config(project_root=self.project_root)
        self.schedule_service = ScheduleService(self.config, logging.getLogger("vk-schedule-service-test"))
        self.notifier = VkNotifier(self.config, logging.getLogger("vk-notifier-test"), self.schedule_service)

    def write_mapping(self, mapping: dict) -> None:
        mapping_path = self.project_root / self.config.vk_users_file
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

    def test_get_vk_mention_matches_full_name_against_short_mapping_key(self) -> None:
        self.write_mapping({"Козлов Данила": 118945590})

        mention = self.notifier.get_vk_mention("Козлов Данила Дмитриевич")

        self.assertEqual(mention, "[id118945590|Козлов Данила Дмитриевич]")

    def test_split_duty_names_splits_two_full_names_without_comma(self) -> None:
        names = self.notifier.split_duty_names(
            "Козлов Егор Евгеньевич Козлов Данила Дмитриевич"
        )

        self.assertEqual(names, ["Козлов Егор Евгеньевич", "Козлов Данила Дмитриевич"])

    def test_split_duty_names_keeps_single_full_name_intact(self) -> None:
        self.assertEqual(
            self.notifier.split_duty_names("Толстогузов Никита Вячеславович"),
            ["Толстогузов Никита Вячеславович"],
        )

    def test_saturday_notification_mentions_both_people_of_merged_shift(self) -> None:
        saturday = datetime(2026, 9, 4, 19, 0, 0, tzinfo=self.schedule_service.server_tz)
        duty_entry = {
            "evening": "Козлов Егор Евгеньевич, Козлов Данила Дмитриевич",
            "morning": "",
        }
        self.write_mapping({"Козлов Егор": 92581714, "Козлов Данила": 118945590})

        with patch.object(self.schedule_service, "get_current_datetime", return_value=saturday), \
             patch.object(self.schedule_service, "get_schedule_entry_by_date", return_value=duty_entry), \
             patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            self.notifier.check_upcoming_duties()

        sent_message = send_vk_message.call_args[0][0]
        self.assertIn("В эту субботу (05.09)", sent_message)
        self.assertIn("[id92581714|Козлов Егор Евгеньевич]", sent_message)
        self.assertIn("[id118945590|Козлов Данила Дмитриевич]", sent_message)
        self.assertIn("дежурят", sent_message)

    def test_multiple_duty_names_stay_on_one_line(self) -> None:
        self.write_mapping({"Козлов Егор": 92581714, "Козлов Данила": 118945590})

        message = self.notifier.format_vk_notification(
            "saturday_tomorrow",
            datetime(2026, 9, 5).date(),
            "Козлов Егор Евгеньевич, Козлов Данила Дмитриевич",
        )

        self.assertEqual(
            message,
            "В эту субботу (05.09) дежурят: "
            "[id92581714|Козлов Егор Евгеньевич] и [id118945590|Козлов Данила Дмитриевич].",
        )
        self.assertNotIn("\n", message)

    def test_single_duty_name_stays_on_one_line(self) -> None:
        self.write_mapping({"Козлов Егор": 92581714})

        message = self.notifier.format_vk_notification(
            "saturday_today", datetime(2026, 9, 5).date(), "Козлов Егор Евгеньевич"
        )

        self.assertEqual(message, "В эту субботу (05.09) дежурит: [id92581714|Козлов Егор Евгеньевич].")

    def test_saturday_morning_notification_is_sent_on_saturday_itself(self) -> None:
        saturday = datetime(2026, 9, 5, 10, 0, 0, tzinfo=self.schedule_service.server_tz)
        duty_entry = {"evening": "Козлов Егор Евгеньевич", "morning": ""}
        self.write_mapping({"Козлов Егор": 92581714})

        with patch.object(self.schedule_service, "get_current_datetime", return_value=saturday),              patch.object(self.schedule_service, "get_schedule_entry_by_date", return_value=duty_entry),              patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            self.notifier.check_upcoming_duties()

        sent_message = send_vk_message.call_args[0][0]
        self.assertIn("В эту субботу (05.09)", sent_message)
        # Не «вечером»: по субботам смена одна, с 8:00 до 16:00.
        self.assertNotIn("вечером", sent_message)

    def test_saturday_is_announced_both_on_friday_evening_and_saturday_morning(self) -> None:
        duty_entry = {"evening": "Козлов Егор Евгеньевич", "morning": ""}
        self.write_mapping({"Козлов Егор": 92581714})
        friday = datetime(2026, 9, 4, 19, 0, 0, tzinfo=self.schedule_service.server_tz)
        saturday = datetime(2026, 9, 5, 10, 0, 0, tzinfo=self.schedule_service.server_tz)

        with patch.object(self.schedule_service, "get_schedule_entry_by_date", return_value=duty_entry),              patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            for moment in (friday, friday, saturday, saturday):
                with patch.object(self.schedule_service, "get_current_datetime", return_value=moment):
                    self.notifier.check_upcoming_duties()

        # Ровно два сообщения: повторные проверки в ту же минуту дедуплицируются.
        self.assertEqual(send_vk_message.call_count, 2)

    def test_no_notification_outside_the_scheduled_minute(self) -> None:
        moment = datetime(2026, 9, 4, 19, 1, 0, tzinfo=self.schedule_service.server_tz)
        with patch.object(self.schedule_service, "get_current_datetime", return_value=moment),              patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            self.notifier.check_upcoming_duties()

        self.assertFalse(send_vk_message.called)

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
        self.assertIn("В эту субботу (13.06)", sent_message)
        self.assertIn("[id101|Иван Иванов]", sent_message)
        self.assertIn("[id202|Петр Петров]", sent_message)


if __name__ == "__main__":
    unittest.main()
