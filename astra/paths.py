"""Пути приложения и служебные каталоги."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Корень проекта (там, где лежит run.py)
ROOT: Path = Path(__file__).resolve().parent.parent

# Каталог с данными можно переопределить переменной окружения ASTRA_DATA_DIR
DATA_DIR: Path = Path(os.environ.get("ASTRA_DATA_DIR") or (ROOT / "data")).expanduser().resolve()

CONFIG_FILE = DATA_DIR / "config.json"
DB_FILE = DATA_DIR / "astra.db"
LOG_FILE = DATA_DIR / "astra.log"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
TTS_CACHE_DIR = DATA_DIR / "tts_cache"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def ensure_dirs() -> None:
    for d in (DATA_DIR, SCREENSHOT_DIR, TTS_CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def is_frozen() -> bool:
    """Запущено ли из PyInstaller-сборки."""
    return getattr(sys, "frozen", False)
