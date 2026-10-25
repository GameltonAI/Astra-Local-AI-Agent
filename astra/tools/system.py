"""Инструменты: система, звук, питание, буфер обмена, скриншоты."""
from __future__ import annotations

import base64
import io
import json
import time
from typing import Any, Dict

from ..paths import SCREENSHOT_DIR
from . import os_control
from .base import Param, Tool


def get_system_info() -> str:
    info = os_control.system_info()
    return json.dumps(info, ensure_ascii=False, indent=2, default=str)


def get_active_windows() -> str:
    from .window_control import format_windows

    return format_windows()


def volume_control(action: str) -> str:
    return os_control.volume(action)


def lock_screen() -> str:
    return os_control.lock_screen()


def power_action(action: str, delay_s: int = 0) -> str:
    return os_control.power(action, delay_s)


def take_screenshot(monitor: int = 0) -> Dict[str, Any]:
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = SCREENSHOT_DIR / f"screenshot_{ts}.png"
    saved, size = os_control.screenshot(path)
    artifact = None
    try:
        from PIL import Image  # type: ignore

        img = Image.open(saved)
        img.thumbnail((720, 720))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72)
        artifact = {
            "kind": "image",
            "name": path.name,
            "path": saved,
            "data_uri": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii"),
        }
    except Exception as exc:  # превью не критично
        print(f"[screenshot] превью недоступно: {exc}")
    return {
        "text": f"Скриншот сохранён: {saved} (размер экрана {size[0]}×{size[1]})",
        "artifact": artifact,
    }


def clipboard_copy(text: str) -> str:
    return os_control.clipboard_set(text)


def clipboard_read() -> str:
    content = os_control.clipboard_get()
    if not content:
        return "Буфер обмена пуст."
    if len(content) > 3000:
        content = content[:3000] + "… (обрезано)"
    return content


def wait(seconds: float = 1.0) -> str:
    seconds = max(0.0, min(float(seconds or 0), 30.0))
    time.sleep(seconds)
    return f"Подождал {seconds:.1f} с"


TOOLS = [
    Tool(
        name="get_system_info",
        summary="Узнать ОС, имя пользователя, разрешение экрана, загрузку CPU/RAM, заряд батареи.",
        params=[],
        func=get_system_info,
        category="system",
    ),
    Tool(
        name="get_active_windows",
        summary="Список открытых окон, чтобы понять, что сейчас на экране.",
        params=[],
        func=get_active_windows,
        category="system",
    ),
    Tool(
        name="volume_control",
        summary="Управление звуком и медиа через системные клавиши.",
        params=[Param("action", "str", "Действие", True,
                      enum=["up", "down", "mute", "play_pause", "next", "prev", "stop"])],
        func=volume_control,
        category="system",
        examples=['{"action": "up"}', '{"action": "play_pause"}'],
    ),
    Tool(
        name="lock_screen",
        summary="Заблокировать экран (Win+L).",
        params=[],
        func=lock_screen,
        category="system",
        danger="confirm",
    ),
    Tool(
        name="power_action",
        summary="Выключить, перезагрузить, усыпить компьютер или выйти из системы. Есть отмена.",
        params=[
            Param("action", "str", "Действие", True,
                  enum=["shutdown", "restart", "sleep", "logoff", "cancel"]),
            Param("delay_s", "int", "Задержка в секундах", False, 10),
        ],
        func=power_action,
        category="system",
        danger="confirm",
    ),
    Tool(
        name="take_screenshot",
        summary="Сделать скриншот экрана и вернуть путь к файлу.",
        params=[Param("monitor", "int", "Номер монитора", False, 0)],
        func=take_screenshot,
        category="system",
    ),
    Tool(
        name="clipboard_copy",
        summary="Записать текст в буфер обмена.",
        params=[Param("text", "str", "Текст", True)],
        func=clipboard_copy,
        category="system",
    ),
    Tool(
        name="clipboard_read",
        summary="Прочитать содержимое буфера обмена.",
        params=[],
        func=clipboard_read,
        category="system",
    ),
    Tool(
        name="wait",
        summary="Подождать N секунд (полезно между шагами, пока загружается программа).",
        params=[Param("seconds", "float", "Секунды", False, 1.0)],
        func=wait,
        category="system",
    ),
]
