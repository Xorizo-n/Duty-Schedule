from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
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

        self.assertEqual(mention, "[id118945590|Козлов Данила]")

    def test_get_vk_mention_drops_patronymic_when_vk_id_is_unknown(self) -> None:
        self.write_mapping({})

        self.assertEqual(
            self.notifier.get_vk_mention("Козлов Данила Дмитриевич"),
            "Козлов Данила",
        )

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
        self.assertIn("[id92581714|Козлов Егор]", sent_message)
        self.assertIn("[id118945590|Козлов Данила]", sent_message)
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
            "[id92581714|Козлов Егор] и [id118945590|Козлов Данила].",
        )
        self.assertNotIn("\n", message)

    def test_single_duty_name_stays_on_one_line(self) -> None:
        self.write_mapping({"Козлов Егор": 92581714})

        message = self.notifier.format_vk_notification(
            "saturday_today", datetime(2026, 9, 5).date(), "Козлов Егор Евгеньевич"
        )

        self.assertEqual(message, "В эту субботу (05.09) дежурит: [id92581714|Козлов Егор].")

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


class VkCommandsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        self.config = make_config(project_root=self.project_root)
        self.schedule_service = ScheduleService(self.config, logging.getLogger("vk-commands-schedule-test"))
        self.notifier = VkNotifier(self.config, logging.getLogger("vk-commands-test"), self.schedule_service)
        # Обычно это заполняет _init_longpoll по groups.getById.
        self.notifier.group_id = 777
        self.notifier.group_screen_name = "duty_bot"
        self.now = datetime(2026, 9, 9, 12, 30, 0, tzinfo=self.schedule_service.server_tz)
        patcher = patch.object(self.schedule_service, "get_current_datetime", return_value=self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_keyboard_offers_three_inline_buttons(self) -> None:
        keyboard = json.loads(self.notifier.build_keyboard())

        self.assertTrue(keyboard["inline"])
        labels = [button["action"]["label"] for button in keyboard["buttons"][0]]
        self.assertEqual(labels, ["Сегодня", "Завтра", "Неделя"])

        payloads = [json.loads(button["action"]["payload"])["command"] for button in keyboard["buttons"][0]]
        self.assertEqual(payloads, ["today", "tomorrow", "week"])

    def test_keyboard_buttons_share_one_color(self) -> None:
        keyboard = json.loads(self.notifier.build_keyboard())

        colors = {button["color"] for button in keyboard["buttons"][0]}
        self.assertEqual(len(colors), 1)

    def test_private_keyboard_has_the_same_buttons_but_stays_under_the_input(self) -> None:
        inline = json.loads(self.notifier.build_keyboard())
        persistent = json.loads(self.notifier.build_keyboard(inline=False))

        self.assertFalse(persistent["inline"])
        self.assertFalse(persistent["one_time"])
        self.assertEqual(persistent["buttons"], inline["buttons"])

    def test_keyboard_is_omitted_when_commands_are_disabled(self) -> None:
        self.notifier.config = replace(self.config, vk_commands_enabled=False)

        self.assertIsNone(self.notifier.build_keyboard())
        self.assertIsNone(self.notifier.build_keyboard(inline=False))

    def test_resolve_command_reads_button_payload(self) -> None:
        self.assertEqual(self.notifier.resolve_command('{"command": "week"}', "Неделя"), "week")

    def test_resolve_command_ignores_bot_mention_in_text(self) -> None:
        self.assertEqual(self.notifier.resolve_command(None, "[club1|@duty_bot] Завтра!"), "tomorrow")

    def test_resolve_command_ignores_plain_typed_mention(self) -> None:
        self.assertEqual(self.notifier.resolve_command(None, "@duty_bot неделя"), "week")

    def test_resolve_command_returns_none_for_ordinary_message(self) -> None:
        self.assertIsNone(self.notifier.resolve_command(None, "всем привет"))

    def test_today_answer_uses_short_names(self) -> None:
        duty_entry = {
            "morning": "Булатов Иван Олегович",
            "evening": "Афонин Кирилл Борисович",
        }

        with patch.object(self.schedule_service, "get_schedule_entry_by_date", return_value=duty_entry):
            answer = self.notifier.build_command_answer("today")

        self.assertEqual(
            answer,
            "Сегодня (09.09, СР):\nУтро: Булатов Иван\nВечер: Афонин Кирилл",
        )

    def test_saturday_answer_lists_both_people_of_the_single_shift(self) -> None:
        duty_entry = {"morning": "", "evening": "Козлов Егор Евгеньевич, Козлов Данила Дмитриевич"}

        with patch.object(self.schedule_service, "get_schedule_entry_by_date", return_value=duty_entry):
            answer = self.notifier._format_day_answer("Суббота", date(2026, 9, 12))

        self.assertEqual(answer, "Суббота (12.09, СБ): Козлов Егор, Козлов Данила.")

    def test_tomorrow_answer_reports_empty_sunday(self) -> None:
        answer = self.notifier._format_day_answer("Завтра", date(2026, 9, 13))

        self.assertEqual(answer, "Завтра (13.09, ВС): воскресенье, дежурных нет.")

    def test_week_answer_lists_one_line_per_day(self) -> None:
        week = [
            {"date": date(2026, 9, 7), "weekday": "ПН", "morning": "Кузнецов Савелий Витальевич", "evening": ""},
            {"date": date(2026, 9, 12), "weekday": "СБ", "morning": "", "evening": "Удочкин Сергей Юрьевич"},
        ]

        with patch.object(self.schedule_service, "get_display_weeks", return_value=[week]):
            answer = self.notifier.build_command_answer("week")

        self.assertEqual(
            answer,
            "Дежурные на неделю (07.09 — 12.09):\n"
            "ПН 07.09 — утро: Кузнецов Савелий\n"
            "СБ 12.09 — Удочкин Сергей",
        )

    def test_handle_command_message_answers_in_the_configured_peer(self) -> None:
        message = {"peer_id": 123, "from_id": 55, "text": "[club777|@duty_bot] сегодня", "payload": None}

        with patch.object(self.schedule_service, "get_schedule_entry_by_date", return_value={}), \
             patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            self.assertTrue(self.notifier.handle_command_message(message))

        self.assertEqual(send_vk_message.call_args.kwargs["peer_id"], 123)
        self.assertTrue(json.loads(send_vk_message.call_args.kwargs["keyboard"])["inline"])

    def test_handle_command_message_ignores_other_conversations(self) -> None:
        message = {"peer_id": 999, "from_id": 55, "text": "[club777|@duty_bot] сегодня"}

        with patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            self.assertFalse(self.notifier.handle_command_message(message))

        self.assertFalse(send_vk_message.called)

    def test_handle_command_message_ignores_its_own_reply(self) -> None:
        # Ответ самого сообщества приходит тем же событием message_new.
        message = {"peer_id": 123, "from_id": -777, "text": "сегодня"}

        with patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            self.assertFalse(self.notifier.handle_command_message(message))

        self.assertFalse(send_vk_message.called)

    def write_mapping(self, mapping: dict) -> None:
        mapping_path = self.project_root / self.config.vk_users_file
        mapping_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")

    def test_allowed_user_ids_reads_both_mapping_formats(self) -> None:
        self.write_mapping(
            {
                "Иван Иванов": 101,
                "Пётр Петров": {"id": 202, "label": "Пётр"},
                "Строка": "303",
                "Без id": {"label": "x"},
                "Мусор": "abc",
            }
        )

        self.assertEqual(self.notifier.allowed_user_ids(), {101, 202, 303})

    def test_private_message_from_listed_user_is_answered_in_that_dialog(self) -> None:
        self.write_mapping({"Иван Иванов": 101})
        message = {"peer_id": 101, "from_id": 101, "text": "завтра", "payload": None}

        with patch.object(self.schedule_service, "get_schedule_entry_by_date", return_value={}), \
             patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            self.assertTrue(self.notifier.handle_command_message(message))

        self.assertEqual(send_vk_message.call_args.kwargs["peer_id"], 101)
        self.assertIn("Завтра", send_vk_message.call_args.args[0])
        # В личке кнопки не цепляются к ответу, а живут под полем ввода.
        self.assertFalse(json.loads(send_vk_message.call_args.kwargs["keyboard"])["inline"])

    def test_private_message_from_unknown_user_is_ignored(self) -> None:
        self.write_mapping({"Иван Иванов": 101})
        message = {"peer_id": 404, "from_id": 404, "text": "сегодня"}

        with patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            self.assertFalse(self.notifier.handle_command_message(message))

        self.assertFalse(send_vk_message.called)

    def test_private_message_without_command_gets_help(self) -> None:
        self.write_mapping({"Пётр Петров": {"id": 202, "label": "Пётр"}})
        message = {"peer_id": 202, "from_id": 202, "text": "привет"}

        with patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            self.assertTrue(self.notifier.handle_command_message(message))

        self.assertIn("сегодня", send_vk_message.call_args.args[0])

    def test_chat_message_without_command_stays_unanswered(self) -> None:
        message = {"peer_id": 123, "from_id": 55, "text": "всем привет"}

        with patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            self.assertFalse(self.notifier.handle_command_message(message))

        self.assertFalse(send_vk_message.called)

    def test_listed_user_is_not_answered_in_a_foreign_chat(self) -> None:
        # Участник из списка пишет в другой беседе — там бот молчит.
        self.write_mapping({"Иван Иванов": 101})
        message = {"peer_id": 2_000_000_005, "from_id": 101, "text": "сегодня"}

        with patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            self.assertFalse(self.notifier.handle_command_message(message))

        self.assertFalse(send_vk_message.called)

    def assert_chat_reply(self, text: str | None, payload: str | None = None) -> str:
        message = {"peer_id": 123, "from_id": 55, "text": text, "payload": payload}
        with patch.object(self.schedule_service, "get_schedule_entry_by_date", return_value={}),              patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            self.assertTrue(self.notifier.handle_command_message(message))
        return send_vk_message.call_args.args[0]

    def assert_chat_silence(self, text: str | None) -> None:
        message = {"peer_id": 123, "from_id": 55, "text": text}
        with patch.object(self.notifier, "send_vk_message", return_value=True) as send_vk_message:
            self.assertFalse(self.notifier.handle_command_message(message))
        self.assertFalse(send_vk_message.called)

    def test_chat_command_without_mention_is_ignored(self) -> None:
        self.assert_chat_silence("сегодня")

    def test_chat_command_with_typed_mention_is_answered(self) -> None:
        self.assertIn("Завтра", self.assert_chat_reply("@duty_bot завтра"))

    def test_chat_command_mentioning_by_club_id_is_answered(self) -> None:
        self.assertIn("Сегодня", self.assert_chat_reply("@club777, сегодня"))

    def test_chat_command_mentioning_someone_else_is_ignored(self) -> None:
        self.assert_chat_silence("[club555|@other_bot] сегодня")
        self.assert_chat_silence("[id55|Вася] сегодня")
        self.assert_chat_silence("@vasya сегодня")

    def test_chat_button_press_needs_no_mention(self) -> None:
        with patch.object(self.schedule_service, "get_display_weeks", return_value=[]):
            answer = self.assert_chat_reply("Неделя", payload='{"command": "week"}')

        self.assertEqual(answer, "Расписание ещё не загружено.")

    def test_bare_mention_in_chat_gets_help(self) -> None:
        self.assertIn("«сегодня»", self.assert_chat_reply("[club777|@duty_bot]"))

    def test_chat_mention_is_not_recognised_before_group_is_known(self) -> None:
        self.notifier.group_id = None

        self.assert_chat_silence("[club777|@duty_bot] сегодня")


if __name__ == "__main__":
    unittest.main()
