from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from duty_scheduler.settings_store import (
    FIELDS_BY_KEY,
    SECRET_PLACEHOLDER,
    SettingsError,
    SettingsStore,
    coerce_value,
)


class CoerceValueTestCase(unittest.TestCase):
    def coerce(self, key: str, value):
        return coerce_value(FIELDS_BY_KEY[key], value)

    def test_optional_int_accepts_empty_value(self) -> None:
        self.assertIsNone(self.coerce("duty_sheet_gid", ""))

    def test_int_rejects_non_numeric_value(self) -> None:
        with self.assertRaises(SettingsError):
            self.coerce("duty_sheet_gid", "первый")

    def test_interval_has_a_lower_bound(self) -> None:
        # Цикл обновителя тикает раз в 10 секунд — меньше просто не сработает.
        with self.assertRaises(SettingsError):
            self.coerce("google_update_interval", 1)
        self.assertEqual(self.coerce("google_update_interval", "45"), 45)

    def test_bool_understands_russian_and_english_words(self) -> None:
        self.assertTrue(self.coerce("vk_commands_enabled", "да"))
        self.assertFalse(self.coerce("vk_commands_enabled", "off"))
        with self.assertRaises(SettingsError):
            self.coerce("vk_commands_enabled", "иногда")

    def test_select_normalizes_case_and_rejects_unknown_level(self) -> None:
        self.assertEqual(self.coerce("console_log_level", "debug"), "DEBUG")
        with self.assertRaises(SettingsError):
            self.coerce("console_log_level", "TRACE")

    def test_timezone_is_validated_against_the_iana_database(self) -> None:
        self.assertEqual(self.coerce("server_timezone", "Europe/Moscow"), "Europe/Moscow")
        with self.assertRaises(SettingsError):
            self.coerce("server_timezone", "Asia/Ekaterinburg")

    def test_required_text_rejects_empty_value(self) -> None:
        with self.assertRaises(SettingsError):
            self.coerce("google_sheet_url", "   ")

    def test_optional_text_accepts_empty_value(self) -> None:
        self.assertEqual(self.coerce("vk_peer_id", ""), "")


class SettingsStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "nested" / "settings.json"
        self.store = SettingsStore(self.path)

    def test_store_starts_empty_and_creates_the_file_on_first_write(self) -> None:
        self.assertFalse(self.store.has_password())
        self.assertEqual(self.store.overrides(), {})

        self.store.set_password("секрет123")

        self.assertTrue(self.path.exists())
        self.assertTrue(self.store.has_password())

    def test_password_is_not_stored_in_clear_text(self) -> None:
        self.store.set_password("секрет123")

        document = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertNotIn("секрет123", self.path.read_text(encoding="utf-8"))
        self.assertIn("salt", document["auth"])
        self.assertTrue(self.store.verify_password("секрет123"))
        self.assertFalse(self.store.verify_password("секрет124"))

    def test_short_password_is_rejected(self) -> None:
        with self.assertRaises(SettingsError):
            self.store.set_password("123")

    def test_saved_values_survive_a_restart(self) -> None:
        self.store.save_values({"vk_peer_id": "2000000042", "google_update_interval": "120"})

        reopened = SettingsStore(self.path)

        self.assertEqual(
            reopened.overrides(),
            {"vk_peer_id": "2000000042", "google_update_interval": 120},
        )

    def test_unknown_keys_are_ignored(self) -> None:
        self.store.save_values({"vk_peer_id": "1", "log_dir": "/tmp/hack"})

        self.assertEqual(self.store.overrides(), {"vk_peer_id": "1"})

    def test_placeholder_keeps_the_stored_secret(self) -> None:
        self.store.save_values({"vk_bot_token": "vk1.a.real-token"})

        self.store.save_values({"vk_bot_token": SECRET_PLACEHOLDER, "vk_peer_id": "7"})

        self.assertEqual(self.store.overrides()["vk_bot_token"], "vk1.a.real-token")

    def test_invalid_value_does_not_touch_the_file(self) -> None:
        self.store.save_values({"vk_peer_id": "7"})

        with self.assertRaises(SettingsError):
            self.store.save_values({"vk_peer_id": "8", "google_update_interval": 1})

        self.assertEqual(SettingsStore(self.path).overrides(), {"vk_peer_id": "7"})

    def test_secret_key_is_generated_once_and_persisted(self) -> None:
        first = self.store.secret_key()

        self.assertEqual(first, self.store.secret_key())
        self.assertEqual(SettingsStore(self.path).secret_key(), first)


if __name__ == "__main__":
    unittest.main()
