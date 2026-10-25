"""Инструменты ввода: клавиатура и мышь."""
from __future__ import annotations

from . import os_control
from .base import Param, Tool


def type_text(text: str) -> str:
    return os_control.type_text(text)


def press_key(key: str, presses: int = 1) -> str:
    return os_control.press_key(key, presses)


def hotkey(keys) -> str:
    return os_control.hotkey(keys)


def mouse_move(x: int, y: int) -> str:
    return os_control.mouse_move(x, y)


def mouse_click(button: str = "left", clicks: int = 1, x=None, y=None) -> str:
    return os_control.mouse_click(button, clicks, x, y)


def mouse_scroll(amount: int) -> str:
    return os_control.mouse_scroll(amount)


TOOLS = [
    Tool(
        name="type_text",
        summary="Напечатать текст в активном окне (поддерживает русский язык и любые символы).",
        params=[Param("text", "str", "Текст, который нужно напечатать", True)],
        func=type_text,
        category="input",
        danger="confirm",
        examples=['{"text": "Привет, мир"}'],
    ),
    Tool(
        name="press_key",
        summary="Нажать клавишу: enter, esc, tab, space, backspace, delete, up/down/left/right, "
                "home, end, pageup, pagedown, f1..f12, win, printscreen, letter (a), digit (5).",
        params=[
            Param("key", "str", "Имя клавиши", True),
            Param("presses", "int", "Сколько раз нажать", False, 1),
        ],
        func=press_key,
        category="input",
        examples=['{"key": "enter"}'],
    ),
    Tool(
        name="hotkey",
        summary="Нажать комбинацию клавиш, например ctrl+s, alt+tab, win+d, ctrl+shift+esc.",
        params=[Param("keys", "list[str]", "Клавиши комбинации по порядку", True)],
        func=hotkey,
        category="input",
        examples=['{"keys": ["ctrl", "s"]}', '{"keys": ["alt", "tab"]}'],
    ),
    Tool(
        name="mouse_move",
        summary="Переместить курсор мыши в абсолютные координаты экрана.",
        params=[
            Param("x", "int", "Координата X", True),
            Param("y", "int", "Координата Y", True),
        ],
        func=mouse_move,
        category="input",
        danger="confirm",
    ),
    Tool(
        name="mouse_click",
        summary="Клик мышью (опционально — сразу с перемещением в точку).",
        params=[
            Param("button", "str", "Кнопка мыши", False, "left", enum=["left", "right", "middle"]),
            Param("clicks", "int", "Количество кликов", False, 1),
            Param("x", "int", "Координата X (необязательно)", False, None),
            Param("y", "int", "Координата Y (необязательно)", False, None),
        ],
        func=mouse_click,
        category="input",
        danger="confirm",
    ),
    Tool(
        name="mouse_scroll",
        summary="Прокрутить колесо мыши. Положительное значение — вверх, отрицательное — вниз.",
        params=[Param("amount", "int", "Количество щелчков", True)],
        func=mouse_scroll,
        category="input",
        danger="confirm",
    ),
]
