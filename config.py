from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_id: int
    database_path: Path


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    admin = os.getenv("ADMIN_ID", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is missing. Add it to Replit Secrets.")
    if not admin.isdigit():
        raise RuntimeError("ADMIN_ID must be the numeric Telegram ID of the bot administrator.")

    database_path = Path(os.getenv("DATABASE_PATH", "data/standoff2.sqlite3"))
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return Settings(bot_token=token, admin_id=int(admin), database_path=database_path)
