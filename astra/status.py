"""Человеческие статусы действий агента.

Зачем: в голосовом режиме пользователь видит под сферой не сухие имена инструментов
(``open_app``, ``click_window_mark``), а живые фразы: «Открываю блокнот», «Листаю»,
«Ввожу текст в поле ввода». Статус приходит от самой модели (поле ``status`` в плане),
а если модель его не прислала — собираем осмысленную фразу из имени инструмента и аргументов.
"""
from __future__ import annotations

import re
from typing import Any, Dict

MAX_STATUS_LEN = 72

# как называть программы по-русски
APP_NAMES: Dict[str, str] = {
    "notepad": "блокнот", "notepad++": "Notepad++", "calculator": "калькулятор", "calc": "калькулятор",
    "chrome": "Chrome", "google chrome": "Chrome", "edge": "Edge", "firefox": "Firefox", "opera": "Opera",
    "yandex": "Яндекс Браузер", "explorer": "проводник", "cmd": "командную строку",
    "powershell": "PowerShell", "terminal": "терминал", "vscode": "VS Code", "code": "VS Code",
    "word": "Word", "excel": "Excel", "powerpoint": "PowerPoint", "paint": "Paint",
    "telegram": "Telegram", "discord": "Discord", "whatsapp": "WhatsApp", "steam": "Steam",
    "spotify": "Spotify", "vlc": "VLC", "settings": "параметры", "taskmgr": "диспетчер задач",
    "photos": "фотографии", "camera": "камеру", "clock": "часы", "mail": "почту",
}

KEY_NAMES: Dict[str, str] = {
    "enter": "Enter", "esc": "Esc", "escape": "Esc", "tab": "Tab", "space": "пробел",
    "backspace": "Backspace", "delete": "Delete", "up": "вверх", "down": "вниз",
    "left": "влево", "right": "вправо", "home": "Home", "end": "End",
    "pageup": "Page Up", "pagedown": "Page Down", "f1": "F1", "f2": "F2", "f3": "F3",
    "f4": "F4", "f5": "F5", "f6": "F6", "f7": "F7", "f8": "F8", "f9": "F9",
    "f10": "F10", "f11": "F11", "f12": "F12", "win": "Win", "ctrl": "Ctrl", "alt": "Alt",
    "shift": "Shift", "printscreen": "Print Screen",
}


def _short(text: Any, limit: int = 40) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def app_label(raw: Any) -> str:
    """``notepad`` → «блокнот», ``C:\\...\\chrome.exe`` → «Chrome»."""
    text = _short(raw, 60)
    if not text:
        return "программу"
    stem = text.replace("\\", "/").split("/")[-1]
    stem = re.sub(r"\.exe$", "", stem, flags=re.I).strip()
    key = stem.lower()
    if key in APP_NAMES:
        return APP_NAMES[key]
    return stem or "программу"


def host_label(raw: Any) -> str:
    url = _short(raw, 200)
    match = re.match(r"^(?:[a-z]+://)?([^/?#]+)", url, re.I)
    host = (match.group(1) if match else url).replace("www.", "")
    return host or "сайт"


def _one_key(raw: Any) -> str:
    """Одна клавиша в человеческом виде: enter → Enter, s → S."""
    text = str(raw).strip()
    known = KEY_NAMES.get(text.lower())
    if known:
        return known
    return text.upper() if len(text) == 1 else text


def keys_label(raw: Any) -> str:
    if isinstance(raw, (list, tuple)):
        parts = [_one_key(k) for k in raw]
    else:
        parts = [_one_key(p) for p in re.split(r"[+,]", str(raw or ""))]
    parts = [p for p in parts if p.strip()]
    return " + ".join(parts) if parts else "клавишу"


def describe_action(tool: str, args: Dict[str, Any] | None = None) -> str:
    """Фраза для блока статусов. Всегда осмысленная и короткая."""
    args = args or {}
    name = (tool or "").strip()
    text = str(args.get("text") or "")

    simple = {
        "take_screenshot": "Делаю снимок экрана",
        "screenshot_window": "Смотрю, что в окне",
        "see_window": "Разглядываю окно и элементы",
        "click_window_mark": "Нажимаю нужный элемент",
        "wait_for_change": "Жду, пока экран изменится",
        "wait_until_stable": "Жду, пока загрузится",
        "wait_for_new_window": "Жду появления окна",
        "detect_captcha": "Проверяю, нет ли проверки на робота",
        "list_windows": "Смотрю, какие окна открыты",
        "list_dir": "Смотрю содержимое папки",
        "search_files": "Ищу файлы",
        "web_search": "Ищу в интернете",
        "fetch_url": "Читаю страницу",
        "run_command": "Выполняю команду",
        "get_system_info": "Смотрю состояние компьютера",
        "volume_control": "Регулирую звук",
        "clipboard_copy": "Копирую текст в буфер обмена",
        "clipboard_read": "Читаю буфер обмена",
        "lock_screen": "Блокирую экран",
        "wait": "Жду",
        "create_dir": "Создаю папку",
        "delete_path": "Удаляю файл",
    }
    if name in simple:
        return simple[name]

    if name == "open_app":
        return f"Открываю {app_label(args.get('app') or args.get('name') or args.get('path'))}"
    if name == "open_url":
        return f"Открываю сайт {host_label(args.get('url'))}"
    if name == "open_in_browser":
        return f"Открываю в браузере {host_label(args.get('url'))}"
    if name == "open_path":
        target = _short(args.get("path"), 34)
        return f"Открываю {target}" if target else "Открываю файл"
    if name == "focus_window":
        title = _short(args.get("title") or args.get("query"), 30)
        return f"Переключаюсь на «{title}»" if title else "Переключаюсь на нужное окно"
    if name == "window_action":
        action = str(args.get("action") or "").lower()
        return {
            "maximize": "Разворачиваю окно", "minimize": "Сворачиваю окно",
            "close": "Закрываю окно", "restore": "Возвращаю окно",
        }.get(action, "Меняю размер окна")
    if name in ("type_text", "type_in_window"):
        # сам текст не показываем: он бывает длинным и личным
        return "Ввожу текст в поле ввода" if text else "Печатаю текст"
    if name == "press_key":
        return f"Нажимаю {keys_label(args.get('key'))}"
    if name in ("hotkey", "hotkey_in_window"):
        return f"Нажимаю {keys_label(args.get('keys') or args.get('key'))}"
    if name == "mouse_move":
        return "Двигаю курсор"
    if name == "mouse_click":
        return "Кликаю мышью"
    if name in ("mouse_scroll", "scroll_in_window", "scroll_until_end"):
        return "Листаю"
    if name == "click_in_window":
        return "Нажимаю в окне"
    if name == "read_text_file":
        target = _short(args.get("path"), 30)
        return f"Читаю {target}" if target else "Читаю файл"
    if name == "write_text_file":
        target = _short(args.get("path"), 30)
        return f"Записываю {target}" if target else "Записываю файл"
    if name == "power_action":
        action = str(args.get("action") or "").lower()
        return {
            "shutdown": "Выключаю компьютер", "restart": "Перезагружаю компьютер",
            "sleep": "Усыпляю компьютер", "logoff": "Выхожу из системы",
            "cancel": "Отменяю выключение",
        }.get(action, "Управляю питанием")
    return f"Выполняю {name or 'действие'}"


def clean_status(text: Any) -> str:
    """Причёсываем статус от модели: без JSON, кавычек, точки и лишней длины."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value or value.startswith("{") or value.startswith("["):
        return ""
    # сначала точки/кавычки по краям, потом ещё раз — уже без них
    for _ in range(2):
        value = value.strip("\"'`«»„“” ").strip()
        value = value.rstrip(".!,;:").strip("\"'`«»„“” ").strip()
    if len(value) > MAX_STATUS_LEN:
        value = value[: MAX_STATUS_LEN - 1].rstrip() + "…"
    if value:
        value = value[0].upper() + value[1:]
    return value


def status_for(tool: str, args: Dict[str, Any] | None, hint: Any = "") -> str:
    """Статус от модели, а если его нет — автоматический по инструменту."""
    return clean_status(hint) or describe_action(tool, args or {})
