from __future__ import annotations

import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from duty_scheduler.settings_api import LOGIN_MAX_ATTEMPTS, settings_api_bp
from duty_scheduler.settings_store import SECRET_PLACEHOLDER, SettingsStore

from tests.helpers import make_config


class FakeService:
    """Сервис, которому важно только то, что ему отдали новый конфиг."""

    def __init__(self) -> None:
        self.applied = []

    def apply_config(self, config) -> None:
        self.applied.append(config)

    def update_google_sheets(self) -> None:
        pass


class SettingsApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)
        self.store = SettingsStore(self.project_root / "settings.json")

        app = Flask(__name__)
        app.secret_key = "test-secret"
        app.config["APP_VERSION"] = "2.4.0"
        app.extensions["config"] = make_config(project_root=self.project_root)
        app.extensions["logger"] = logging.getLogger("settings-api-test")
        app.extensions["settings_store"] = self.store
        app.extensions["schedule_service"] = FakeService()
        app.extensions["vk_notifier"] = FakeService()
        app.register_blueprint(settings_api_bp)

        self.app = app
        self.client = app.test_client()
        # load_config читает реальное окружение — подменяем на конфиг из хелпера.
        patcher = patch(
            "duty_scheduler.runtime.load_config",
            side_effect=lambda overrides=None: make_config(project_root=self.project_root),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_password(self, password: str = "секрет123"):
        return self.client.post("/api/settings/password", json={"password": password})

    # ------------------------------------------------------------------

    def test_session_reports_that_no_password_is_set_yet(self) -> None:
        payload = self.client.get("/api/settings/session").get_json()

        self.assertFalse(payload["password_set"])
        self.assertFalse(payload["authenticated"])

    def test_first_password_is_accepted_without_authentication(self) -> None:
        response = self.set_password()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["authenticated"])
        self.assertTrue(self.store.has_password())

    def test_short_first_password_is_rejected(self) -> None:
        response = self.set_password("123")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.store.has_password())

    def test_settings_require_authentication(self) -> None:
        self.store.set_password("секрет123")

        self.assertEqual(self.client.get("/api/settings").status_code, 401)
        self.assertEqual(self.client.post("/api/settings", json={"values": {}}).status_code, 401)

    def test_login_opens_access_and_logout_closes_it(self) -> None:
        self.store.set_password("секрет123")

        self.assertEqual(self.client.post("/api/settings/login", json={"password": "секрет123"}).status_code, 200)
        self.assertEqual(self.client.get("/api/settings").status_code, 200)

        self.client.post("/api/settings/logout")

        self.assertEqual(self.client.get("/api/settings").status_code, 401)

    def test_wrong_password_is_rejected_and_then_locked_out(self) -> None:
        self.store.set_password("секрет123")

        with patch("duty_scheduler.settings_api.FAILED_ATTEMPT_DELAY", 0):
            for _ in range(LOGIN_MAX_ATTEMPTS):
                response = self.client.post("/api/settings/login", json={"password": "нет"})
                self.assertEqual(response.status_code, 403)

            # Даже верный пароль теперь ждёт окончания блокировки.
            response = self.client.post("/api/settings/login", json={"password": "секрет123"})

        self.assertEqual(response.status_code, 429)

    def test_password_can_be_changed_only_with_the_current_one(self) -> None:
        self.store.set_password("секрет123")

        rejected = self.client.post(
            "/api/settings/password",
            json={"password": "новыйпароль", "current_password": "мимо"},
        )
        self.assertEqual(rejected.status_code, 403)

        accepted = self.client.post(
            "/api/settings/password",
            json={"password": "новыйпароль", "current_password": "секрет123"},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(self.store.verify_password("новыйпароль"))

    def test_read_masks_the_vk_token(self) -> None:
        self.set_password()

        groups = self.client.get("/api/settings").get_json()["groups"]
        fields = {field["key"]: field for group in groups for field in group["fields"]}

        # В make_config токен непустой, наружу должен уйти только плейсхолдер.
        self.assertEqual(fields["vk_bot_token"]["value"], SECRET_PLACEHOLDER)
        self.assertEqual(fields["vk_peer_id"]["value"], "123")

    def test_read_marks_where_each_value_comes_from(self) -> None:
        self.set_password()
        self.store.save_values({"vk_peer_id": "2000000042"})

        groups = self.client.get("/api/settings").get_json()["groups"]
        fields = {field["key"]: field for group in groups for field in group["fields"]}

        self.assertEqual(fields["vk_peer_id"]["source"], "settings")
        self.assertEqual(fields["duty_sheet_name"]["source"], "env")

    def test_write_saves_values_and_pushes_the_new_config_to_services(self) -> None:
        self.set_password()

        response = self.client.post(
            "/api/settings",
            json={"values": {"vk_peer_id": "2000000042", "console_log_level": "debug"}},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.store.overrides(),
            {"vk_peer_id": "2000000042", "console_log_level": "DEBUG"},
        )
        self.assertEqual(len(self.app.extensions["schedule_service"].applied), 1)
        self.assertEqual(len(self.app.extensions["vk_notifier"].applied), 1)

    def test_write_reports_validation_errors_and_keeps_the_old_values(self) -> None:
        self.set_password()

        response = self.client.post(
            "/api/settings",
            json={"values": {"server_timezone": "Asia/Ekaterinburg"}},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("часовой пояс", response.get_json()["error"])
        self.assertEqual(self.store.overrides(), {})


if __name__ == "__main__":
    unittest.main()
