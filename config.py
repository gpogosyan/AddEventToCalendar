"""Настройки из переменных окружения и файла .env (не коммитьте .env)."""

import os
from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)


def _require(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(
            f"Отсутствует переменная окружения {name}. "
            "Скопируйте .env.example в .env и заполните значения."
        )
    return v.strip()


TELEGRAM_TOKEN = _require("TELEGRAM_TOKEN")
OPENAI_API_KEY = _require("OPENAI_API_KEY")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
EMAIL_LOGIN = _require("EMAIL_LOGIN")
EMAIL_PASSWORD = _require("EMAIL_PASSWORD")
