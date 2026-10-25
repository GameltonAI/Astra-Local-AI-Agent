"""Инструменты работы с окнами: ассистент действует внутри конкретного окна."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

from ..paths import SCREENSHOT_DIR
from . import os_control, window_control
from .base import Param, Tool


def list_windows() -> str:
    return window_control.format_windows()


def focus_window(title: str = "") -> str:
    window = window_control.focus_window(title)
    return (f"Активно окно «{window.title}» ({window.app}).\n"
            f"Размер окна: {window.width}×{window.height} px, левый верхний угол экрана: "
            f"({window.left}, {window.top}). Область содержимого (client area): "
            f"{window.client_width}×{window.client_height} px начинается в точке "
            f"({window.client_left}, {window.client_top}).\n"
            f"Координаты для click_in_window считаются от левого верхнего угла содержимого окна.")


def window_action(action: str, title: str = "") -> str:
    return window_control.window_action(action, title)


def click_in_window(title: str, x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    window = window_control.focus_window(title)
    time.sleep(0.15)
    screen_x, screen_y = window_control.client_point(window, x, y)
    if not (0 <= int(x) <= window.client_width and 0 <= int(y) <= window.client_height):
        note = (f" (внимание: точка выходит за пределы окна "
                f"{window.client_width}×{window.client_height})")
    else:
        note = ""
    result = os_control.mouse_click(button, clicks, screen_x, screen_y)
    return f"В окне «{window.title}»: {result}, координаты внутри окна ({x}, {y}){note}"


def type_in_window(title: str, text: str, submit: bool = False) -> str:
    window = window_control.focus_window(title)
    time.sleep(0.15)
    result = os_control.type_text(text)
    if submit:
        time.sleep(0.1)
        os_control.press_key("enter")
        result += " и нажата Enter"
    return f"В окне «{window.title}»: {result}"


def hotkey_in_window(title: str, keys) -> str:
    window = window_control.focus_window(title)
    time.sleep(0.15)
    return f"В окне «{window.title}»: {os_control.hotkey(keys)}"


def scroll_in_window(title: str, amount: int) -> str:
    window = window_control.focus_window(title)
    time.sleep(0.1)
    center_x, center_y = window_control.client_point(window, window.client_width // 2, window.client_height // 2)
    os_control.mouse_move(center_x, center_y)
    return f"В окне «{window.title}»: {os_control.mouse_scroll(amount)}"


def screenshot_window(title: str = "") -> Dict[str, Any]:
    window = window_control.focus_window(title) if title.strip() else window_control.find_window("")
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in window.title)[:40] or "window"
    path = SCREENSHOT_DIR / f"window_{safe}_{ts}.png"
    saved, size = window_control.capture_window(window, path)

    artifact = None
    try:
        import base64
        import io

        from PIL import Image  # type: ignore

        with Image.open(saved) as img:
            img.thumbnail((720, 720))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=72)
            artifact = {"kind": "image", "name": Path(saved).name, "path": saved,
                        "data_uri": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")}
    except Exception as exc:  # noqa: BLE001
        print(f"[screenshot_window] превью недоступно: {exc}")

    return {
        "text": (f"Снимок окна «{window.title}» сохранён: {saved} ({size[0]}×{size[1]} px). "
                 f"Область содержимого окна начинается в точке ({window.client_left}, {window.client_top})."),
        "artifact": artifact,
    }


def open_in_browser(url: str, browser: str = "") -> str:
    return window_control.open_in_browser(url, browser)


TOOLS = [
    Tool(
        name="list_windows",
        summary="Список открытых окон: заголовок, программа, id, размер, свёрнуто/развёрнуто. "
                "Начни с него, если нужно работать в конкретном окне.",
        params=[],
        func=list_windows,
        category="windows",
    ),
    Tool(
        name="focus_window",
        summary="Активировать окно (браузер, проводник, любое приложение) по части заголовка или имени "
                "программы и получить его координаты для кликов внутри окна.",
        params=[
            Param("title", "str", "Часть заголовка окна или имя программы "
                                  "(например, 'chrome', 'яндекс', 'документ.docx')", False, ""),
        ],
        func=focus_window,
        category="windows",
        examples=['{"title": "chrome"}', '{"title": "Яндекс"}'],
    ),
    Tool(
        name="window_action",
        summary="Действие с окном: maximize, minimize, restore, normal, close.",
        params=[
            Param("action", "str", "Что сделать с окном", True,
                  enum=["maximize", "minimize", "restore", "normal", "close"]),
            Param("title", "str", "Часть заголовка или имя программы", False, ""),
        ],
        func=window_action,
        category="windows",
        danger="confirm",
    ),
    Tool(
        name="click_in_window",
        summary="Клик внутри окна по координатам относительно его содержимого (0,0 — левый верхний угол "
                "окна, а не экрана). Удобно для сайтов и интерфейсов программ.",
        params=[
            Param("title", "str", "Часть заголовка или имя программы", True),
            Param("x", "int", "X внутри окна", True),
            Param("y", "int", "Y внутри окна", True),
            Param("button", "str", "Кнопка мыши", False, "left", enum=["left", "right", "middle"]),
            Param("clicks", "int", "Количество кликов", False, 1),
        ],
        func=click_in_window,
        category="windows",
        danger="confirm",
        examples=['{"title": "chrome", "x": 420, "y": 300}'],
    ),
    Tool(
        name="type_in_window",
        summary="Активировать окно и напечатать в нём текст (полезно для поиска на сайте, полей ввода).",
        params=[
            Param("title", "str", "Часть заголовка или имя программы", True),
            Param("text", "str", "Текст", True),
            Param("submit", "bool", "Нажать Enter после ввода", False, False),
        ],
        func=type_in_window,
        category="windows",
        danger="confirm",
    ),
    Tool(
        name="hotkey_in_window",
        summary="Активировать окно и нажать комбинацию клавиш (ctrl+l, ctrl+t, ctrl+f, alt+left…).",
        params=[
            Param("title", "str", "Часть заголовка или имя программы", True),
            Param("keys", "list[str]", "Клавиши комбинации", True),
        ],
        func=hotkey_in_window,
        category="windows",
        danger="confirm",
        examples=['{"title": "chrome", "keys": ["ctrl", "l"]}'],
    ),
    Tool(
        name="scroll_in_window",
        summary="Прокрутить содержимое окна (положительное значение — вверх, отрицательное — вниз).",
        params=[
            Param("title", "str", "Часть заголовка или имя программы", True),
            Param("amount", "int", "Щелчки колеса", True),
        ],
        func=scroll_in_window,
        category="windows",
        danger="confirm",
    ),
    Tool(
        name="screenshot_window",
        summary="Снимок только одного окна (а не всего экрана) — чтобы понять, что на странице или в программе.",
        params=[Param("title", "str", "Часть заголовка или имя программы (пусто — активное окно)", False, "")],
        func=screenshot_window,
        category="windows",
        examples=['{"title": "chrome"}'],
    ),
    Tool(
        name="open_in_browser",
        summary="Открыть сайт в уже запущенном браузере (активирует его окно, Ctrl+L, вводит адрес). "
                "Если браузер не запущен — запустит.",
        params=[
            Param("url", "str", "Адрес сайта", True),
            Param("browser", "str", "Имя браузера (chrome, edge, firefox, yandex)", False, ""),
        ],
        func=open_in_browser,
        category="windows",
        examples=['{"url": "youtube.com/results?search_query=python"}'],
    ),
]
