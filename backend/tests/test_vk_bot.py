from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from duty_scheduler.config import load_config
from duty_scheduler.schedule_service import ScheduleService
from duty_scheduler.settings_store import SETTING_FIELDS
from duty_scheduler.swaps import SwapService
from duty_scheduler.vk_bot import VkNotifier

from tests.helpers import FakeWorksheet, duty_sheet_fixture, make_config


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



class VkChatMembersTestCase(unittest.TestCase):
    CHAT_PEER = "2000000001"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        self.config = replace(make_config(project_root=self.project_root), vk_peer_id=self.CHAT_PEER)
        self.schedule_service = ScheduleService(self.config, logging.getLogger("vk-members-schedule-test"))
        self.notifier = VkNotifier(self.config, logging.getLogger("vk-members-test"), self.schedule_service)
        self.mapping_path = self.project_root / self.config.vk_users_file
        self.mapping_path.write_text(json.dumps({"Иванов Иван": 101}, ensure_ascii=False), encoding="utf-8")

    def members_response(self) -> dict:
        # Форма ответа messages.getConversationMembers.
        return {
            "count": 5,
            "items": [
                {"member_id": 101},
                {"member_id": 202},
                {"member_id": 303},
                {"member_id": -777},
                {"member_id": 404},
            ],
            "profiles": [
                {"id": 101, "first_name": "Ваня", "last_name": "Иванов"},
                {"id": 202, "first_name": "Пётр", "last_name": "Петров"},
                {"id": 303, "first_name": "DELETED", "last_name": "", "deactivated": "deleted"},
                {"id": 404, "first_name": "Олег", "last_name": "Сидоров"},
            ],
            "groups": [{"id": 777, "name": "Бот дежурств"}],
        }

    def mapping(self) -> dict:
        return json.loads(self.mapping_path.read_text(encoding="utf-8"))

    def test_fetch_skips_communities_and_deleted_pages(self) -> None:
        with patch.object(self.notifier, "call_api", return_value=self.members_response()) as call_api:
            members = self.notifier.fetch_chat_members()

        call_api.assert_called_once_with("messages.getConversationMembers", peer_id=self.CHAT_PEER)
        self.assertEqual(
            members,
            [
                {"id": 101, "name": "Иванов Ваня"},
                {"id": 202, "name": "Петров Пётр"},
                {"id": 404, "name": "Сидоров Олег"},
            ],
        )

    def test_sync_adds_only_unknown_people_and_keeps_admin_names(self) -> None:
        with patch.object(self.notifier, "call_api", return_value=self.members_response()):
            added = self.notifier.sync_chat_members()

        self.assertEqual(added, ["Петров Пётр", "Сидоров Олег"])
        self.assertEqual(
            self.mapping(),
            {
                "Иванов Иван": 101,
                "Петров Пётр": {"id": 202, "auto": True},
                "Сидоров Олег": {"id": 404, "auto": True},
            },
        )

    def test_added_people_get_private_access_and_mentions(self) -> None:
        with patch.object(self.notifier, "call_api", return_value=self.members_response()):
            self.notifier.sync_chat_members()

        self.assertIn(404, self.notifier.allowed_user_ids())
        self.assertEqual(self.notifier.get_vk_mention("Сидоров Олег Петрович"), "[id404|Сидоров Олег]")

    def test_sync_changes_nothing_when_vk_refuses(self) -> None:
        # Бот не администратор беседы — VK отвечает ошибкой, call_api отдаёт None.
        with patch.object(self.notifier, "call_api", return_value=None):
            self.assertEqual(self.notifier.sync_chat_members(), [])

        self.assertEqual(self.mapping(), {"Иванов Иван": 101})

    def test_sync_skips_a_private_dialog_in_peer_setting(self) -> None:
        self.notifier.config = replace(self.config, vk_peer_id="123")

        with patch.object(self.notifier, "call_api") as call_api:
            self.assertEqual(self.notifier.sync_chat_members(), [])

        self.assertFalse(call_api.called)

    def test_join_message_in_the_chat_triggers_sync(self) -> None:
        message = {"peer_id": int(self.CHAT_PEER), "from_id": 101, "text": "", "action": {"type": "chat_invite_user", "member_id": 404}}

        with patch.object(self.notifier, "sync_chat_members") as sync:
            self.assertTrue(self.notifier.handle_chat_action(message))

        sync.assert_called_once()

    def test_join_in_another_chat_and_ordinary_messages_are_not_actions(self) -> None:
        foreign = {"peer_id": 2000000009, "action": {"type": "chat_invite_user_by_link"}}
        ordinary = {"peer_id": int(self.CHAT_PEER), "text": "сегодня"}

        with patch.object(self.notifier, "sync_chat_members") as sync:
            self.assertFalse(self.notifier.handle_chat_action(foreign))
            self.assertFalse(self.notifier.handle_chat_action(ordinary))

        self.assertFalse(sync.called)

    def test_settings_change_schedules_a_new_sync(self) -> None:
        self.notifier.last_members_sync = 12345.0

        self.notifier.apply_config(self.config)

        self.assertEqual(self.notifier.last_members_sync, 0.0)


class VkSwapDialogTestCase(unittest.TestCase):
    """Диалог подмены в личке. Фикстура листа, «сейчас» — СР 02.09, 12:00."""

    CALLER_ID = 101

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.config = make_config(project_root=root)
        logger = logging.getLogger("vk-swap-test")

        self.schedule_service = ScheduleService(self.config, logger)
        now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=self.schedule_service.server_tz)
        self.start_patch(patch.object(self.schedule_service, "get_current_datetime", return_value=now))
        self.sheet = FakeWorksheet(duty_sheet_fixture())
        self.start_patch(patch.object(self.schedule_service, "open_worksheet", side_effect=lambda: self.sheet))
        # После записи бот перечитывает таблицу в фоне — в тестах сети нет.
        self.start_patch(patch.object(self.schedule_service, "update_google_sheets"))
        self.schedule_service.data_cache["schedule"] = self.schedule_service.parse_duty_sheet(self.sheet)

        self.swaps = SwapService(self.config, logger, self.schedule_service, root / "swaps.json")
        self.notifier = VkNotifier(self.config, logger, self.schedule_service, self.swaps)
        (root / self.config.vk_users_file).write_text(
            json.dumps({"Булатов Иван": self.CALLER_ID, "Афонин Кирилл": 202}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.sent = self.start_patch(patch.object(self.notifier, "send_vk_message", return_value=True))

    def start_patch(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def dm(self, text: str | None = None, **payload) -> tuple[str, dict | None]:
        message = {
            "peer_id": self.CALLER_ID,
            "from_id": self.CALLER_ID,
            "text": text or "",
            "payload": json.dumps(payload) if payload else None,
        }
        self.assertTrue(self.notifier.handle_command_message(message))
        call = self.sent.call_args
        keyboard = call.kwargs.get("keyboard")
        return call.args[0], json.loads(keyboard) if keyboard else None

    @staticmethod
    def labels(keyboard: dict) -> list[list[str]]:
        return [[button["action"]["label"] for button in row] for row in keyboard["buttons"]]

    def test_private_keyboard_has_swap_button_in_the_same_style(self) -> None:
        keyboard = json.loads(self.notifier.build_keyboard(inline=False))

        self.assertEqual(self.labels(keyboard), [["Сегодня", "Завтра", "Неделя"], ["Подмена"]])
        colors = {button["color"] for row in keyboard["buttons"] for button in row}
        self.assertEqual(len(colors), 1)

    def test_chat_keyboard_has_no_swap_button(self) -> None:
        keyboard = json.loads(self.notifier.build_keyboard())

        self.assertEqual(self.labels(keyboard), [["Сегодня", "Завтра", "Неделя"]])

    def test_private_help_mentions_swap(self) -> None:
        answer, _ = self.dm("привет")

        self.assertIn("Подмена", answer)

    def test_full_swap_dialog_writes_table_and_tells_the_chat(self) -> None:
        answer, keyboard = self.dm("Подмена", command="swap")
        self.assertIn("на какой день", answer)
        self.assertFalse(keyboard["inline"])
        self.assertEqual(self.labels(keyboard)[0], ["СР 02.09", "ЧТ 03.09", "ПТ 04.09"])
        self.assertEqual(self.labels(keyboard)[-1], ["Отмена"])

        answer, keyboard = self.dm("ЧТ 03.09", command="swap_date", date="2026-09-03")
        self.assertEqual(answer, "03.09 (ЧТ):\nУтро: Юрчик\nВечер: Афонин Кирилл\n\nКого подменяете?")
        self.assertEqual(self.labels(keyboard), [["Юрчик", "Афонин К."], ["Другая дата", "Отмена"]])

        answer, keyboard = self.dm("Афонин К.", command="swap_person", date="2026-09-03", index=1)
        self.assertIn("Подмена: 03.09 (ЧТ), вечер.", answer)
        self.assertIn("Вместо: Афонин Кирилл", answer)
        self.assertIn("Дежурит: Булатов Иван (вы)", answer)
        self.assertEqual(self.labels(keyboard), [["Подтвердить", "Отмена"]])

        answer, keyboard = self.dm("Подтвердить", command="swap_confirm")
        self.assertIn("Готово", answer)
        self.assertEqual(self.labels(keyboard)[-1], ["Подмена"])
        self.assertEqual(self.sheet.cell(4, 5).value, "Булатов Иван Олегович")

        # Перед ответом в личку ушло сообщение в беседу — с упоминаниями обоих.
        chat_call = self.sent.call_args_list[-2]
        self.assertNotIn("peer_id", chat_call.kwargs)
        self.assertEqual(
            chat_call.args[0],
            "Подмена: 03.09 (ЧТ), вечер вместо [id202|Афонин Кирилл] дежурит [id101|Булатов Иван].",
        )

    def test_swap_is_not_announced_in_chat_when_flag_is_off(self) -> None:
        self.notifier.config = replace(self.config, vk_swap_announce=False)
        self.dm("подмена")
        self.dm(command="swap_date", date="2026-09-03")
        self.dm(command="swap_person", date="2026-09-03", index=1)

        answer, _ = self.dm(command="swap_confirm")

        self.assertIn("Готово", answer)
        self.assertEqual(self.sheet.cell(4, 5).value, "Булатов Иван Олегович")
        # Все сообщения ушли только в личку — в беседу ничего.
        self.assertTrue(all(call.kwargs.get("peer_id") == self.CALLER_ID for call in self.sent.call_args_list))

    def test_swap_announce_flag_is_read_from_env_only(self) -> None:
        with patch.dict(os.environ, {"VK_SWAP_ANNOUNCE": "0"}):
            self.assertFalse(load_config().vk_swap_announce)
            # Значение из файла настроек его не включит и не выключит.
            self.assertFalse(load_config({"vk_swap_announce": True}).vk_swap_announce)
        with patch.dict(os.environ):
            os.environ.pop("VK_SWAP_ANNOUNCE", None)
            self.assertTrue(load_config().vk_swap_announce)
        self.assertNotIn("vk_swap_announce", {field.key for field in SETTING_FIELDS})

    def test_date_can_be_typed(self) -> None:
        self.dm("подмена")

        answer, _ = self.dm("3.09")

        self.assertIn("Кого подменяете?", answer)

    def test_unknown_date_text_asks_again(self) -> None:
        self.dm("подмена")

        answer, _ = self.dm("в четверг")

        self.assertIn("Не понял дату", answer)
        self.assertEqual(self.notifier.swap_sessions[self.CALLER_ID]["step"], "date")

    def test_past_date_is_rejected(self) -> None:
        self.dm("подмена")

        answer, _ = self.dm("01.09")

        self.assertIn("уже прошло", answer)

    def test_cancel_forgets_the_dialog(self) -> None:
        self.dm("подмена")

        answer, keyboard = self.dm("Отмена", command="swap_cancel")

        self.assertEqual(answer, "Подмена отменена.")
        self.assertEqual(self.labels(keyboard)[-1], ["Подмена"])
        self.assertNotIn(self.CALLER_ID, self.notifier.swap_sessions)

    def test_stale_confirm_button_does_nothing(self) -> None:
        answer, _ = self.dm("Подтвердить", command="swap_confirm")

        self.assertIn("устарел", answer)
        self.assertEqual(self.sheet.updates, [])

    def test_regular_command_in_the_middle_ends_the_dialog(self) -> None:
        self.dm("подмена")

        answer, _ = self.dm("сегодня")

        self.assertTrue(answer.startswith("Сегодня (02.09, СР)"))
        self.assertNotIn(self.CALLER_ID, self.notifier.swap_sessions)

    def test_swap_in_chat_points_to_private_messages(self) -> None:
        self.notifier.group_id = 777
        message = {"peer_id": 123, "from_id": 55, "text": "[club777|@duty_bot] подмена"}

        self.assertTrue(self.notifier.handle_command_message(message))

        self.assertIn("в личных сообщениях", self.sent.call_args.args[0])

    def test_button_labels_stay_distinct(self) -> None:
        namesakes = [
            {"shift": "morning", "name": "Козлов Егор Евгеньевич"},
            {"shift": "evening", "name": "Козлов Ефим Петрович"},
        ]
        same_person = [
            {"shift": "morning", "name": "Юрчик"},
            {"shift": "evening", "name": "Юрчик"},
        ]

        self.assertEqual(self.notifier.person_button_labels(namesakes), ["Козлов Егор", "Козлов Ефим"])
        self.assertEqual(self.notifier.person_button_labels(same_person), ["Юрчик (утро)", "Юрчик (вечер)"])


if __name__ == "__main__":
    unittest.main()
