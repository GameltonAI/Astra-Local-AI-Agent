"""Низкоуровневое управление ОС.

Windows реализован на чистом ctypes (SendInput / user32), поэтому не требует
pyautogui и работает с любыми раскладками, включая кириллицу.
Linux (X11) и macOS поддержаны через xdotool/osascript там, где это просто,
иначе выбрасывается UnsupportedError — приложение продолжает работать.
"""
from __future__ import annotations

import ctypes
import os
import platform as _pyplatform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


class ControlError(RuntimeError):
    """Ошибка управления ОС (понятное пользователю сообщение)."""


class UnsupportedError(ControlError):
    """Действие не поддерживается на этой платформе."""


# ======================================================================
#  Windows: ctypes-биндинги
# ======================================================================
if IS_WINDOWS:  # pragma: no cover - выполняется только на Windows
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTunion(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]

    INPUT_KEYBOARD = 1
    INPUT_MOUSE = 0

    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_SCANCODE = 0x0008

    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    MOUSEEVENTF_WHEEL = 0x0800
    MOUSEEVENTF_HWHEEL = 0x1000

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int

    # Виртуальные коды клавиш (https://learn.microsoft.com/windows/win32/inputdev/virtual-key-codes)
    VK: Dict[str, int] = {
        "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "shift": 0x10,
        "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12, "pause": 0x13,
        "capslock": 0x14, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "pageup": 0x21,
        "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22, "end": 0x23, "home": 0x24,
        "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "printscreen": 0x2C,
        "prtsc": 0x2C, "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E,
        "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D, "sleep": 0x5F,
        "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
        "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
        "multiply": 0x6A, "add": 0x6B, "separator": 0x6C, "subtract": 0x6D,
        "decimal": 0x6E, "divide": 0x6F,
        "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
        "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
        "numlock": 0x90, "scrolllock": 0x91, "shiftleft": 0xA0, "shiftright": 0xA1,
        "ctrlleft": 0xA2, "ctrlright": 0xA3, "altleft": 0xA4, "altright": 0xA5,
        "browserback": 0xA6, "browserforward": 0xA7, "browserrefresh": 0xA8,
        "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
        "medianexttrack": 0xB0, "mediaprevtrack": 0xB1, "mediastop": 0xB2,
        "mediaplaypause": 0xB3, "launchmail": 0xB4, "launchmediaselect": 0xB5,
        "launchapp1": 0xB6, "launchapp2": 0xB7,
        ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
        "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
        "plus": 0xBB, "minus": 0xBD,
    }
    for _i in range(10):
        VK[str(_i)] = 0x30 + _i
    for _c in "abcdefghijklmnopqrstuvwxyz":
        VK[_c] = ord(_c.upper())

    _EXTENDED_KEYS = {
        "left", "up", "right", "down", "home", "end", "pageup", "pgup", "pagedown", "pgdn",
        "insert", "ins", "delete", "del", "printscreen", "prtsc", "divide", "numlock",
        "win", "lwin", "rwin", "apps", "volumemute", "volumedown", "volumeup",
        "medianexttrack", "mediaprevtrack", "mediastop", "mediaplaypause",
    }

    def _send_inputs(events: Sequence[INPUT]) -> None:
        if not events:
            return
        arr = (INPUT * len(events))(*events)
        sent = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
        if sent != len(events):
            raise ControlError(f"SendInput отправил {sent} из {len(events)} событий "
                               f"(код ошибки {ctypes.get_last_error()})")

    _SCANCODES = {0x0D: 0x1C, 0x09: 0x0F}

    def _key_event(vk: int, up: bool = False, extended: bool = False) -> INPUT:
        flags = KEYEVENTF_KEYUP if up else 0
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        return INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=KEYBDINPUT(vk, 0, flags, 0, 0)))

    def _unicode_events(text: str) -> List[INPUT]:
        """Символы — через KEYEVENTF_UNICODE, Enter/Tab — настоящими нажатиями клавиш."""
        events: List[INPUT] = []
        for kind, value in text_tokens(text):
            if kind == "unicode":
                events.append(INPUT(type=INPUT_KEYBOARD,
                                    u=_INPUTunion(ki=KEYBDINPUT(0, value, KEYEVENTF_UNICODE, 0, 0))))
                events.append(INPUT(type=INPUT_KEYBOARD,
                                    u=_INPUTunion(ki=KEYBDINPUT(0, value, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0))))
            else:
                vk, extended = _resolve_key(str(value))
                events.append(_key_event(vk, False, extended))
                events.append(_key_event(vk, True, extended))
        return events

    def _resolve_key(key: str) -> Tuple[int, bool]:
        k = (key or "").strip().lower()
        if k in VK:
            return VK[k], k in _EXTENDED_KEYS
        if len(k) == 1:
            code = ord(k)
            if code <= 0x7F:
                return code, False
            raise ControlError(
                f"Клавишу «{key}» нельзя нажать напрямую. Для ввода символов используйте type_text.")
        raise ControlError(f"Неизвестная клавиша: «{key}». Примеры: ctrl, alt, shift, enter, esc, tab, f5, win, a-z, 0-9")


def _utf16_units(char: str) -> List[int]:
    """Кодовые единицы UTF-16 (эмодзи и прочие не-BMP дают суррогатную пару)."""
    data = char.encode("utf-16-le")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


def text_tokens(text: str) -> List[Tuple[str, object]]:
    """Разбить текст на токены ввода.

    Возвращает список ("unicode", кодовая_единица) и ("key", имя клавиши).
    Перевод строки и табуляция НЕ печатаются как символы (иначе в блокноте и
    многих редакторах они теряются, и весь текст склеивается в одну строку),
    а нажимаются настоящими клавишами Enter и Tab.
    """
    tokens: List[Tuple[str, object]] = []
    index = 0
    length = len(text or "")
    while index < length:
        char = text[index]
        if char == "\r":
            # CRLF — один перевод строки; одиночный CR тоже считаем переводом
            if index + 1 < length and text[index + 1] == "\n":
                index += 1
            tokens.append(("key", "enter"))
        elif char == "\n":
            tokens.append(("key", "enter"))
        elif char == "\t":
            tokens.append(("key", "tab"))
        else:
            for unit in _utf16_units(char):
                tokens.append(("unicode", unit))
        index += 1
    return tokens


def _sleep(seconds: float) -> None:
    time.sleep(max(0.0, seconds))


# ======================================================================
#  Клавиатура
# ======================================================================
def type_text(text: str, interval: float = 0.0) -> str:
    """Напечатать произвольный текст (включая кириллицу) в активное окно."""
    if not text:
        return "Пустой текст — печатать нечего."
    if IS_WINDOWS:
        events = _unicode_events(text)
        # отправляем пачками, чтобы не упереться в лимиты буфера ввода
        chunk = 64
        for i in range(0, len(events), chunk):
            _send_inputs(events[i:i + chunk])
            if interval:
                _sleep(interval * 16)
        return f"Напечатано символов: {len(text)}"
    if IS_LINUX:
        if not shutil.which("xdotool"):
            raise UnsupportedError("Для ввода текста на Linux установите xdotool: sudo apt install xdotool")
        # текст печатаем частями, переводы строк — отдельными нажатиями Return
        for line_index, chunk in enumerate(text.replace("\r\n", "\n").split("\n")):
            if chunk:
                subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", str(int(interval * 1000)), chunk],
                               check=True, capture_output=True, timeout=120)
            if line_index < len(text.replace("\r\n", "\n").split("\n")) - 1:
                subprocess.run(["xdotool", "key", "Return"], check=True, capture_output=True, timeout=30)
        return f"Напечатано символов: {len(text)}"
    if IS_MAC:
        script = f'tell application "System Events" to keystroke {_osa_quote(text)}'
        _run_osascript(script)
        return f"Напечатано символов: {len(text)}"
    raise UnsupportedError("Ввод текста не поддерживается на этой платформе")


def press_key(key: str, presses: int = 1, interval: float = 0.05) -> str:
    """Нажать одну клавишу (например, enter, esc, tab, f5)."""
    presses = max(1, min(int(presses or 1), 50))
    if IS_WINDOWS:
        vk, ext = _resolve_key(key)
        events: List[INPUT] = []
        for _ in range(presses):
            events.append(_key_event(vk, False, ext))
            events.append(_key_event(vk, True, ext))
        _send_inputs(events)
        return f"Нажата клавиша «{key}» ×{presses}"
    if IS_LINUX:
        if not shutil.which("xdotool"):
            raise UnsupportedError("Для нажатий клавиш на Linux установите xdotool")
        for _ in range(presses):
            subprocess.run(["xdotool", "key", _xdo_key(key)], check=True, capture_output=True, timeout=30)
            _sleep(interval)
        return f"Нажата клавиша «{key}» ×{presses}"
    if IS_MAC:
        code = _MAC_KEYCODES.get(key.strip().lower())
        if not code:
            raise ControlError(f"Неизвестная клавиша для macOS: {key}")
        for _ in range(presses):
            _run_osascript(f'tell application "System Events" to key code {code}')
        return f"Нажата клавиша «{key}» ×{presses}"
    raise UnsupportedError("Нажатия клавиш не поддерживаются")


def hotkey(keys: Iterable[str]) -> str:
    """Комбинация клавиш: hotkey(["ctrl", "shift", "esc"])."""
    seq = [k for k in (keys or []) if str(k).strip()]
    if not seq:
        raise ControlError("Пустая комбинация клавиш")
    if IS_WINDOWS:
        resolved: List[Tuple[int, bool]] = [_resolve_key(k) for k in seq]
        events: List[INPUT] = []
        for vk, ext in resolved:
            events.append(_key_event(vk, False, ext))
        for vk, ext in reversed(resolved):
            events.append(_key_event(vk, True, ext))
        _send_inputs(events)
        return "Комбинация: " + "+".join(seq)
    if IS_LINUX:
        if not shutil.which("xdotool"):
            raise UnsupportedError("Для горячих клавиш на Linux установите xdotool")
        combo = "+".join(_xdo_key(k) for k in seq)
        subprocess.run(["xdotool", "key", "--clearmodifiers", combo], check=True, capture_output=True, timeout=30)
        return "Комбинация: " + "+".join(seq)
    if IS_MAC:
        mods = {"ctrl": "control down", "control": "control down", "alt": "option down",
                "option": "option down", "shift": "shift down", "cmd": "command down",
                "win": "command down", "command": "command down"}
        parts = [mods[k.strip().lower()] for k in seq[:-1] if k.strip().lower() in mods]
        key = seq[-1].strip().lower()
        code = _MAC_KEYCODES.get(key)
        inner = ("key code " + str(code)) if code else f"keystroke {_osa_quote(seq[-1])}"
        script = 'tell application "System Events" to ' + inner
        if parts:
            script += " using {" + ", ".join(parts) + "}"
        _run_osascript(script)
        return "Комбинация: " + "+".join(seq)
    raise UnsupportedError("Горячие клавиши не поддерживаются")


# ======================================================================
#  Мышь
# ======================================================================
def screen_size() -> Tuple[int, int]:
    if IS_WINDOWS:
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    if IS_LINUX and shutil.which("xdotool"):
        out = subprocess.run(["xdotool", "getdisplaygeometry"], capture_output=True, text=True, timeout=15)
        try:
            w, h = out.stdout.split()
            return int(w), int(h)
        except ValueError:
            pass
    try:
        from PIL import ImageGrab  # type: ignore

        img = ImageGrab.grab()
        return img.size
    except Exception:
        raise UnsupportedError("Не удалось определить разрешение экрана")


def mouse_move(x: int, y: int) -> str:
    x, y = int(x), int(y)
    if IS_WINDOWS:
        if not user32.SetCursorPos(x, y):
            raise ControlError(f"SetCursorPos({x}, {y}) не удался")
        return f"Курсор в ({x}, {y})"
    if IS_LINUX:
        if not shutil.which("xdotool"):
            raise UnsupportedError("Для мыши на Linux установите xdotool")
        subprocess.run(["xdotool", "mousemove", str(x), str(y)], check=True, capture_output=True, timeout=15)
        return f"Курсор в ({x}, {y})"
    raise UnsupportedError("Перемещение мыши не поддерживается")


def mouse_click(button: str = "left", clicks: int = 1, x: Optional[int] = None, y: Optional[int] = None) -> str:
    button = (button or "left").lower()
    clicks = max(1, min(int(clicks or 1), 5))
    if x is not None and y is not None:
        mouse_move(x, y)
        _sleep(0.05)
    if IS_WINDOWS:
        down, up = {
            "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        }.get(button, (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
        for _ in range(clicks):
            _send_inputs([
                INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(0, 0, 0, down, 0, 0))),
                INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(0, 0, 0, up, 0, 0))),
            ])
            _sleep(0.06)
        return f"Клик ({button}) ×{clicks}" + (f" в точке ({x}, {y})" if x is not None else "")
    if IS_LINUX:
        if not shutil.which("xdotool"):
            raise UnsupportedError("Для мыши на Linux установите xdotool")
        btn = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
        for _ in range(clicks):
            subprocess.run(["xdotool", "click", "--repeat", "1", btn], check=True, capture_output=True, timeout=15)
        return f"Клик ({button}) ×{clicks}"
    raise UnsupportedError("Клик мыши не поддерживается")


def mouse_scroll(amount: int) -> str:
    """amount > 0 — вверх, < 0 — вниз."""
    amount = int(amount)
    if IS_WINDOWS:
        delta = 120 * max(-20, min(20, amount))
        _send_inputs([INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(0, 0, delta & 0xFFFFFFFF, MOUSEEVENTF_WHEEL, 0, 0)))])
        return f"Прокрутка на {amount} щелчков"
    if IS_LINUX:
        if not shutil.which("xdotool"):
            raise UnsupportedError("Для прокрутки на Linux установите xdotool")
        btn = "4" if amount > 0 else "5"
        subprocess.run(["xdotool", "click", "--repeat", str(abs(amount)), "--delay", "20", btn],
                       check=True, capture_output=True, timeout=30)
        return f"Прокрутка на {amount} щелчков"
    raise UnsupportedError("Прокрутка не поддерживается")


# ======================================================================
#  Система: громкость, медиа, питание, блокировка
# ======================================================================
def volume(action: str) -> str:
    """action: up | down | mute | play_pause | next | prev | stop | unmute"""
    action = (action or "up").lower()
    if action == "unmute":
        action = "mute"
    if IS_WINDOWS:
        mapping = {
            "up": 0xAF, "down": 0xAE, "mute": 0xAD,
            "play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
        }
        if action not in mapping:
            raise ControlError(f"Неизвестное действие громкости: {action}")
        vk = mapping[action]
        steps = 3 if action in ("up", "down") else 1
        events: List[INPUT] = []
        for _ in range(steps):
            events.append(_key_event(vk, False, True))
            events.append(_key_event(vk, True, True))
        _send_inputs(events)
        names = {"up": "громкость выше", "down": "громкость ниже", "mute": "выключен/включён звук",
                 "play_pause": "пауза/воспроизведение", "next": "следующий трек",
                 "prev": "предыдущий трек", "stop": "стоп"}
        return f"Медиа-клавиша: {names.get(action, action)}"
    if IS_LINUX and shutil.which("amixer"):
        cmd = {"up": ["amixer", "-q", "sset", "Master", "5%+"],
               "down": ["amixer", "-q", "sset", "Master", "5%-"],
               "mute": ["amixer", "-q", "sset", "Master", "toggle"]}.get(action)
        if cmd:
            subprocess.run(cmd, check=False, capture_output=True, timeout=15)
            return f"Громкость: {action}"
        if action in ("play_pause", "next", "prev") and shutil.which("playerctl"):
            subprocess.run(["playerctl", {"play_pause": "play-pause", "next": "next", "prev": "previous"}[action]],
                           check=False, capture_output=True, timeout=15)
            return f"Медиа: {action}"
    if IS_MAC:
        if action in ("up", "down", "mute"):
            _run_osascript(f"set volume output volume (output volume of (get volume settings) "
                           f"{'+' if action == 'up' else '-' if action == 'down' else ''}6)")
            return f"Громкость: {action}"
    raise UnsupportedError("Управление громкостью не поддерживается на этой платформе")


def lock_screen() -> str:
    if IS_WINDOWS:
        subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
        return "Экран заблокирован"
    if IS_MAC:
        _run_osascript('tell application "System Events" to keystroke "q" using {command down, control down}')
        return "Экран заблокирован"
    if IS_LINUX:
        for cmd in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"], ["gnome-screensaver-command", "-l"]):
            if shutil.which(cmd[0]):
                subprocess.run(cmd, check=False, capture_output=True, timeout=15)
                return "Экран заблокирован"
    raise UnsupportedError("Блокировка экрана не поддерживается")


def power(action: str, delay_s: int = 0) -> str:
    """action: shutdown | restart | sleep | logoff | cancel"""
    action = (action or "").lower()
    delay_s = max(0, min(int(delay_s or 0), 3600))
    if IS_WINDOWS:
        if action == "cancel":
            subprocess.run(["shutdown", "/a"], check=False, capture_output=True, timeout=15)
            return "Запланированное выключение отменено"
        flags = {"shutdown": "/s", "restart": "/r", "logoff": "/l", "sleep": None}
        if action == "sleep":
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
            return "Переход в спящий режим"
        if action not in flags or flags[action] is None:
            raise ControlError(f"Неизвестное действие питания: {action}")
        subprocess.run(["shutdown", flags[action], "/t", str(delay_s)], check=False, capture_output=True, timeout=15)
        names = {"shutdown": "Выключение", "restart": "Перезагрузка", "logoff": "Выход из системы"}
        return f"{names[action]} через {delay_s} с"
    if IS_LINUX:
        mapping = {"shutdown": ["systemctl", "poweroff"], "restart": ["systemctl", "reboot"],
                   "sleep": ["systemctl", "suspend"], "logoff": ["loginctl", "terminate-user", os.environ.get("USER", "")]}
        cmd = mapping.get(action)
        if not cmd:
            raise ControlError(f"Неизвестное действие питания: {action}")
        subprocess.Popen(cmd)
        return f"Выполняется: {action}"
    raise UnsupportedError("Управление питанием не поддерживается")


def media_key(action: str) -> str:
    return volume(action)


# ======================================================================
#  Буфер обмена
# ======================================================================
def clipboard_set(text: str) -> str:
    if IS_WINDOWS:
        # через PowerShell: надёжно и без сторонних библиотек
        script = "$in = [Console]::In.ReadToEnd(); Set-Clipboard -Value $in"
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            input=text, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        if proc.returncode != 0:
            raise ControlError(f"Не удалось записать в буфер обмена: {proc.stderr.strip()[:200]}")
        return f"В буфер обмена записано {len(text)} символов"
    if IS_MAC:
        proc = subprocess.run(["pbcopy"], input=text, text=True, encoding="utf-8", timeout=15)
        if proc.returncode != 0:
            raise ControlError("pbcopy вернул ошибку")
        return f"В буфер обмена записано {len(text)} символов"
    if IS_LINUX:
        for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["wl-copy"]):
            if shutil.which(cmd[0]):
                subprocess.run(cmd, input=text, text=True, encoding="utf-8", timeout=15)
                return f"В буфер обмена записано {len(text)} символов"
        raise UnsupportedError("Установите xclip, xsel или wl-copy")
    raise UnsupportedError("Буфер обмена не поддерживается")


def clipboard_get() -> str:
    if IS_WINDOWS:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        return (proc.stdout or "").rstrip("\r\n")
    if IS_MAC:
        return subprocess.run(["pbpaste"], capture_output=True, text=True, encoding="utf-8", timeout=15).stdout
    if IS_LINUX:
        for cmd in (["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"], ["wl-paste"]):
            if shutil.which(cmd[0]):
                return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=15).stdout
        raise UnsupportedError("Установите xclip, xsel или wl-paste")
    raise UnsupportedError("Буфер обмена не поддерживается")


# ======================================================================
#  Запуск приложений / URL / файлов
# ======================================================================
APP_ALIASES: Dict[str, List[str]] = {
    "chrome": ["chrome", "google chrome", "хром"],
    "edge": ["edge", "microsoft edge"],
    "firefox": ["firefox", "мозила", "mozilla"],
    "opera": ["opera"],
    "notepad": ["notepad", "блокнот"],
    "calculator": ["calc", "calculator", "калькулятор"],
    "explorer": ["explorer", "проводник", "files"],
    "paint": ["mspaint", "paint", "пейнт"],
    "cmd": ["cmd", "command prompt", "командная строка"],
    "powershell": ["powershell", "pwsh"],
    "terminal": ["wt", "windows terminal", "терминал"],
    "taskmgr": ["taskmgr", "диспетчер задач"],
    "control": ["control", "панель управления"],
    "settings": ["ms-settings:", "параметры", "настройки"],
    "vscode": ["code", "vscode", "vs code"],
    "word": ["winword", "word"],
    "excel": ["excel"],
    "powerpoint": ["powerpnt", "powerpoint"],
    "telegram": ["telegram", "телеграм"],
    "discord": ["discord"],
    "spotify": ["spotify"],
    "steam": ["steam"],
    "obs": ["obs64", "obs"],
    "vlc": ["vlc"],
    "snipping": ["snippingtool", "ножницы"],
}


def _normalize_app(name: str) -> str:
    key = (name or "").strip().lower()
    for canonical, aliases in APP_ALIASES.items():
        if key == canonical or key in aliases:
            return canonical
    return key


def open_target(target: str) -> str:
    """Открыть URL, файл, папку или приложение средствами ОС."""
    target = (target or "").strip().strip('"')
    if not target:
        raise ControlError("Пустая цель для открытия")
    if IS_WINDOWS:
        if target.startswith(("http://", "https://")):
            os.startfile(target)  # type: ignore[attr-defined]
            return f"Открыт адрес: {target}"
        path = Path(os.path.expandvars(os.path.expanduser(target)))
        if path.exists() or target.startswith("ms-settings:") or target.startswith("shell:"):
            os.startfile(str(path if path.exists() else target))  # type: ignore[attr-defined]
            return f"Открыто: {target}"
        return launch_app(target)
    if IS_MAC:
        subprocess.Popen(["open", target])
        return f"Открыто: {target}"
    if IS_LINUX:
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Открыто: {target}"
        return launch_app(target)
    raise UnsupportedError("Открытие ссылок не поддерживается")


def launch_app(name: str, args: Optional[Sequence[str]] = None) -> str:
    """Запустить приложение по имени/алиасу. Ищет в PATH, известных путях, меню «Пуск»."""
    raw = (name or "").strip().strip('"')
    if not raw:
        raise ControlError("Не указано приложение")
    canonical = _normalize_app(raw)
    args = list(args or [])

    if IS_WINDOWS:
        # 1) шорткаты вида winword / code / calc лежат в PATH
        exe = shutil.which(canonical) or shutil.which(raw)
        if exe:
            subprocess.Popen([exe, *args], close_fds=True)
            return f"Запущено: {exe}"

        # 2) известные ярлыки меню «Пуск»
        lnk = _find_start_menu_shortcut(canonical) or _find_start_menu_shortcut(raw)
        if lnk:
            os.startfile(str(lnk))  # type: ignore[attr-defined]
            return f"Запущено: {lnk.name}"

        # 3) просим оболочку открыть по ассоциации
        try:
            subprocess.Popen(["cmd", "/c", "start", "", canonical, *args], close_fds=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return f"Команда запуска отправлена: {canonical}"
        except Exception:
            pass

        # 4) последняя попытка — поиск в меню «Пуск» через Win+typed
        try:
            press_key("win")
            _sleep(0.45)
            type_text(canonical)
            _sleep(0.6)
            press_key("enter")
            return f"Ищу и запускаю через меню «Пуск»: {canonical}"
        except Exception as exc:
            raise ControlError(f"Не удалось запустить «{name}»: {exc}")

    if IS_MAC:
        subprocess.Popen(["open", "-a", raw, *args])
        return f"Запущено: {raw}"

    if IS_LINUX:
        exe = shutil.which(raw) or shutil.which(canonical)
        if exe:
            subprocess.Popen([exe, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Запущено: {exe}"
        try:
            subprocess.Popen([raw, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Запущено: {raw}"
        except FileNotFoundError:
            raise ControlError(f"Приложение «{name}» не найдено")
    raise UnsupportedError("Запуск приложений не поддерживается")


def _find_start_menu_shortcut(name: str) -> Optional[Path]:
    """Ищем .lnk в меню «Пуск» (общее и пользовательское)."""
    bases = []
    for env in ("ProgramData", "APPDATA"):
        root = os.environ.get(env)
        if root:
            bases.append(Path(root) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    needle = name.lower()
    best: Optional[Path] = None
    for base in bases:
        if not base.exists():
            continue
        for lnk in base.rglob("*.lnk"):
            stem = lnk.stem.lower()
            if stem == needle:
                return lnk
            if needle in stem and best is None:
                best = lnk
    return best


# ======================================================================
#  Скриншот
# ======================================================================
def screenshot(path: Path, region: Optional[Tuple[int, int, int, int]] = None) -> Tuple[str, Tuple[int, int]]:
    try:
        from PIL import ImageGrab  # type: ignore
    except ImportError as exc:
        raise ControlError("Для скриншотов установите Pillow: pip install Pillow") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if IS_WINDOWS:
            img = ImageGrab.grab(all_screens=True) if region is None else ImageGrab.grab(bbox=region, all_screens=True)
        else:
            img = ImageGrab.grab() if region is None else ImageGrab.grab(bbox=region)
    except Exception as exc:
        raise ControlError(f"Не удалось сделать скриншот: {exc}") from exc
    img.save(path)
    return str(path), img.size


# ======================================================================
#  Прочее
# ======================================================================
def _osa_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_osascript(script: str) -> str:
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise ControlError(f"osascript: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


_MAC_KEYCODES = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "esc": 53, "escape": 53, "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
}


def _xdo_key(key: str) -> str:
    mapping = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
               "win": "super", "enter": "Return", "return": "Return", "esc": "Escape",
               "escape": "Escape", "tab": "Tab", "space": "space", "backspace": "BackSpace",
               "delete": "Delete", "del": "Delete"}
    return mapping.get(key.strip().lower(), key.strip())


def system_info() -> Dict[str, object]:
    info: Dict[str, object] = {
        "os": _pyplatform.system(),
        "os_release": _pyplatform.release(),
        "os_version": _pyplatform.version(),
        "machine": _pyplatform.machine(),
        "python": _pyplatform.python_version(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER") or "",
        "hostname": _pyplatform.node(),
        "home": str(Path.home()),
        "cwd": os.getcwd(),
    }
    try:
        w, h = screen_size()
        info["screen"] = f"{w}x{h}"
    except Exception:
        info["screen"] = "неизвестно"
    try:
        import psutil  # type: ignore

        info["cpu_percent"] = psutil.cpu_percent(interval=0.2)
        info["ram_percent"] = psutil.virtual_memory().percent
        info["battery"] = getattr(psutil.sensors_battery(), "percent", None) if hasattr(psutil, "sensors_battery") else None
        info["uptime_hours"] = round((time.time() - psutil.boot_time()) / 3600, 1)
    except Exception:
        pass
    return info


def list_visible_windows() -> List[str]:
    """Заголовки видимых окон (Windows). Помогает ИИ понять контекст."""
    if not IS_WINDOWS:
        return []
    titles: List[str] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value.strip():
            titles.append(buf.value.strip())
        return True

    enum_proc = EnumWindowsProc(callback)
    try:
        user32.EnumWindows.argtypes = (EnumWindowsProc, wintypes.LPARAM)
        user32.EnumWindows.restype = wintypes.BOOL
    except Exception:
        pass
    try:
        user32.EnumWindows(enum_proc, 0)
    except Exception:
        return []
    return titles[:40]
