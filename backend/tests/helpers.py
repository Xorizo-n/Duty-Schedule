from __future__ import annotations

from pathlib import Path

from duty_scheduler.config import AppConfig


def make_config(project_root: Path) -> AppConfig:
    return AppConfig(
        project_root=project_root,
        frontend_dir=project_root / "frontend",
        google_sheet_url="https://example.com/sheet",
        credentials_file="credentials.json",
        duty_sheet_gid=1262048925,
        duty_sheet_name="Новое Дежуство",
        server_timezone="Asia/Yekaterinburg",
        vk_bot_token="token",
        vk_peer_id="123",
        vk_api_version="5.199",
        vk_users_file="vk_users.json",
        console_log_level="INFO",
        file_log_level="WARNING",
        log_dir=str(project_root / "logs"),
        google_update_interval=60,
        ntp_update_interval=60,
        app_version="2.2.1",
    )


class FakeWorksheet:
    """Заглушка листа Google Sheets: отдает заранее заданную матрицу значений."""

    def __init__(self, values: list[list[str]], title: str = "Новое Дежуство ") -> None:
        self._values = values
        self.title = title

    def get_all_values(self) -> list[list[str]]:
        return [list(row) for row in self._values]


def duty_sheet_fixture() -> list[list[str]]:
    """Фрагмент реального листа 'Новое Дежуство ' (две недели, ПН-СБ)."""
    return [
        ["Столбец 1", "пн", "вт", "ср", "чт", "пт", "Время", "сб", "", "", "", "Афонин Кирилл Борисович"],
        ["дата", "31.08", "01.09", "02.09", "03.09", "04.09", "", "05.09", "", "", "", "Булатов Иван Олегович"],
        [
            "утро", "", "Булатов Иван Олегович", "Щербатых Кирилл Александрович", "Юрчик",
            "Козлов Данила Дмитриевич", "с 8:00 до 10:00", "Козлов Егор Евгеньевич",
            "с 8:00 до 16:00", "", "", "Гришин Роман Юрьевич",
        ],
        [
            "вечер", "", "Афонин Кирилл Борисович", "Пичугин Максим Константинович",
            "Афонин Кирилл Борисович", "Назаров Михаил Владимирович", "с 17:00 до 20:00",
            "Козлов Данила Дмитриевич", "", "", "", "мистер х",
        ],
        ["дата", "07.09", "08.09", "09.09", "10.09", "11.09", "", "12.09"],
        [
            "утро", "Кузнецов Савелий Витальевич", "Мирзагитов Сергей Юрьевич",
            "Назаров Михаил Владимирович", "Пичугин Максим Константинович",
            "Толстогузов Никита Вячеславович", "с 8:00 до 10:00", "Удочкин Сергей Юрьевич",
            "с 8:00 до 16:00",
        ],
        [
            "вечер", "Козлов Егор Евгеньевич", "Кузнецов Савелий Витальевич",
            "Мирзагитов Сергей Юрьевич", "Назаров Михаил Владимирович",
            "Булатов Иван Олегович", "с 17:00 до 20:00", "Толстогузов Никита Вячеславович",
        ],
    ]
