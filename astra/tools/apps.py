"""Инструменты: запуск программ, открытие ссылок и путей."""
from __future__ import annotations

from . import os_control
from .base import Param, Tool


def open_app(name: str, args=None) -> str:
    return os_control.launch_app(name, args)


def open_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise os_control.ControlError("Пустой адрес")
    if not url.startswith(("http://", "https://", "file://")):
        url = "https://" + url
    return os_control.open_target(url)


def open_path(path: str) -> str:
    return os_control.open_target(path)


TOOLS = [
    Tool(
        name="open_app",
        summary="Запустить установленную программу по имени или алиасу (chrome, notepad, calculator, "
                "telegram, explorer, vscode, word, steam…).",
        params=[
            Param("name", "str", "Имя программы или алиас", True),
            Param("args", "list[str]", "Аргументы командной строки", False, None),
        ],
        func=open_app,
        category="apps",
        examples=['{"name": "chrome"}', '{"name": "notepad"}'],
    ),
    Tool(
        name="open_url",
        summary="Открыть сайт в браузере по умолчанию.",
        params=[Param("url", "str", "Адрес сайта", True)],
        func=open_url,
        category="apps",
        examples=['{"url": "youtube.com"}'],
    ),
    Tool(
        name="open_path",
        summary="Открыть файл или папку в проводнике / приложении по умолчанию.",
        params=[Param("path", "str", "Путь к файлу или папке", True)],
        func=open_path,
        category="apps",
        examples=['{"path": "C:/Users/User/Downloads"}'],
    ),
]
