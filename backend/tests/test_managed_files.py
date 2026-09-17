from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from duty_scheduler.managed_files import (
    describe_credentials,
    describe_path,
    read_vk_users,
    resolve_path,
    validate_credentials,
    validate_vk_users,
    write_credentials,
    write_vk_users,
)
from duty_scheduler.settings_store import SettingsError


def service_account_key(**overrides) -> dict:
    key = {
        "type": "service_account",
        "project_id": "duty",
        "client_email": "duty@duty.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    key.update(overrides)
    return key


class ManagedFilesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    # ------------------------------------------------------------------
    # Пути
    # ------------------------------------------------------------------

    def test_relative_path_is_taken_from_project_root_and_absolute_as_is(self) -> None:
        self.assertEqual(resolve_path(self.root, "vk_users.json"), self.root / "vk_users.json")

        absolute = self.root / "data" / "credentials.json"
        self.assertEqual(resolve_path(self.root, str(absolute)), absolute)

    def test_missing_file_in_missing_directory_is_still_reported_writable(self) -> None:
        # Каталог создастся при записи — важны права ближайшего существующего.
        info = describe_path(self.root / "data" / "nested" / "vk_users.json")

        self.assertFalse(info["exists"])
        self.assertTrue(info["writable"])

    # ------------------------------------------------------------------
    # vk_users.json
    # ------------------------------------------------------------------

    def test_read_flattens_both_value_formats(self) -> None:
        path = self.root / "vk_users.json"
        path.write_text(
            json.dumps({"Иван Иванов": 101, "Пётр Петров": {"id": 202, "label": "Пётр"}}),
            encoding="utf-8",
        )

        self.assertEqual(
            read_vk_users(path),
            [
                {"name": "Иван Иванов", "id": 101, "label": ""},
                {"name": "Пётр Петров", "id": 202, "label": "Пётр"},
            ],
        )

    def test_read_of_missing_file_is_an_empty_list(self) -> None:
        self.assertEqual(read_vk_users(self.root / "vk_users.json"), [])

    def test_read_rejects_a_file_that_is_not_an_object(self) -> None:
        path = self.root / "vk_users.json"
        path.write_text("[1, 2]", encoding="utf-8")

        with self.assertRaises(SettingsError):
            read_vk_users(path)

    def test_validate_restores_the_short_format_when_there_is_no_label(self) -> None:
        mapping = validate_vk_users(
            [
                {"name": "  Иван   Иванов ", "id": " 101 ", "label": ""},
                {"name": "Пётр Петров", "id": 202, "label": "Пётр"},
            ]
        )

        self.assertEqual(mapping, {"Иван Иванов": 101, "Пётр Петров": {"id": 202, "label": "Пётр"}})

    def test_validate_rejects_bad_rows(self) -> None:
        cases = [
            ("не список", {"name": "x"}),
            ("пустое имя", [{"name": "", "id": 1}]),
            ("не число", [{"name": "Иван Иванов", "id": "id101"}]),
            ("ноль", [{"name": "Иван Иванов", "id": 0}]),
            ("дубль без учёта регистра", [{"name": "Иван Иванов", "id": 1}, {"name": "иван иванов", "id": 2}]),
        ]
        for title, raw in cases:
            with self.subTest(title):
                with self.assertRaises(SettingsError):
                    validate_vk_users(raw)

    def test_write_creates_the_file_and_round_trips(self) -> None:
        path = self.root / "data" / "vk_users.json"

        write_vk_users(path, {"Иван Иванов": 101})

        self.assertTrue(path.is_file())
        self.assertEqual(read_vk_users(path), [{"name": "Иван Иванов", "id": 101, "label": ""}])
        self.assertFalse(path.with_suffix(".json.tmp").exists())

    # ------------------------------------------------------------------
    # credentials.json
    # ------------------------------------------------------------------

    def test_validate_accepts_a_service_account_key(self) -> None:
        text = validate_credentials(json.dumps(service_account_key()).encode("utf-8"))

        self.assertEqual(json.loads(text)["client_email"], "duty@duty.iam.gserviceaccount.com")

    def test_validate_rejects_anything_that_is_not_a_service_account_key(self) -> None:
        cases = [
            ("пусто", b""),
            ("не JSON", b"{oops"),
            ("не объект", b"[]"),
            ("другой тип", json.dumps(service_account_key(type="authorized_user")).encode("utf-8")),
            ("нет ключа", json.dumps(service_account_key(private_key="")).encode("utf-8")),
            ("слишком большой", json.dumps(service_account_key(pad="x" * 70_000)).encode("utf-8")),
        ]
        for title, content in cases:
            with self.subTest(title):
                with self.assertRaises(SettingsError):
                    validate_credentials(content)

    def test_describe_reports_the_account_email_but_never_the_key(self) -> None:
        path = self.root / "credentials.json"
        write_credentials(path, validate_credentials(json.dumps(service_account_key()).encode("utf-8")))

        info = describe_credentials(path)

        self.assertTrue(info["exists"])
        self.assertEqual(info["client_email"], "duty@duty.iam.gserviceaccount.com")
        self.assertNotIn("private_key", json.dumps(info))

    def test_describe_missing_key(self) -> None:
        info = describe_credentials(self.root / "credentials.json")

        self.assertFalse(info["exists"])
        self.assertIsNone(info["client_email"])


if __name__ == "__main__":
    unittest.main()
