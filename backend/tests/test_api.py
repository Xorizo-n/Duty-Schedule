from __future__ import annotations

from datetime import datetime
import unittest

from flask import Flask

from duty_scheduler.api import api_bp


class FakeScheduleService:
    def build_api_payload(self) -> dict:
        return {
            "today": "2026-06-08",
            "today_duty": {"morning": "Петр Петров", "evening": "Иван Иванов", "date": "2026-06-08"},
            "weeks": [],
            "error": None,
            "server_time": "2026-06-08T10:00:00+05:00",
            "last_updated": 1234567890,
        }

    def build_health_payload(self) -> dict:
        return {"status": "healthy"}

    def get_current_datetime(self) -> datetime:
        return datetime(2026, 6, 8, 10, 0, 0)


class ApiBlueprintTestCase(unittest.TestCase):
    def setUp(self) -> None:
        app = Flask(__name__)
        app.config["APP_VERSION"] = "2.2.1"
        app.extensions["schedule_service"] = FakeScheduleService()
        app.register_blueprint(api_bp)
        self.client = app.test_client()

    def test_api_data_returns_expected_payload(self) -> None:
        response = self.client.get("/api/data")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"]["today"], "2026-06-08")
        self.assertEqual(payload["data"]["today_duty"]["evening"], "Иван Иванов")

    def test_version_endpoint_returns_configured_version(self) -> None:
        response = self.client.get("/version")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertEqual(payload["version"], "2.2.1")
        self.assertIn("timestamp", payload)


if __name__ == "__main__":
    unittest.main()
