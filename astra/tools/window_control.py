"""Работа с окнами: поиск, активация, геометрия, клики внутри окна.

Позволяет ассистенту работать не «вслепую» с системой, а внутри конкретного
окна: активировать браузер, нажать Ctrl+L, ввести адрес, кликнуть по элементу
внутри окна по относительным координатам, сделать снимок только этого окна.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


class WindowError(RuntimeError):
    """Окно не найдено или с ним не удалось взаимодействовать."""


@dataclass
class WindowInfo:
    wid: str                      # hwnd в hex (Windows) или id xdotool
    title: str
    app: str = ""                 # имя процесса / класса
    pid: int = 0
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0
    client_left: int = 0
    client_top: int = 0
    client_width: int = 0
    client_height: int = 0
    minimized: bool = False
    maximized: bool = False
    extra: Dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        d = {
            "id": self.wid,
            "title": self.title,
            "app": self.app,
            "pid": self.pid,
            "position": [self.left, self.top],
            "size": [self.width, self.height],
            "client_size": [self.client_width, self.client_height],
            "minimized": self.minimized,
            "maximized": self.maximized,
        }
        d.update(self.extra)
        return d

    def short(self) -> str:
        flags = []
        if self.minimized:
            flags.append("свёрнуто")
        if self.maximized:
            flags.append("развёрнуто")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        return f"{self.title} ({self.app}, id={self.wid}, {self.width}×{self.height}){suffix}"


# ======================================================================
#  Windows (ctypes)
# ======================================================================
if IS_WINDOWS:  # pragma: no cover - только Windows
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    SW_HIDE = 0
    SW_SHOW = 5
    SW_MINIMIZE = 6
    SW_RESTORE = 9
    SW_MAXIMIZE = 3
    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    PW_RENDERFULLCONTENT = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    DIB_RGB_COLORS = 0

    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsZoomed.argtypes = (wintypes.HWND,)
    user32.IsZoomed.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
    user32.ClientToScreen.restype = wintypes.BOOL
    user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = (wintypes.HWND,)
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint)
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.PostMessageW.restype = wintypes.BOOL
    user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
    user32.PrintWindow.restype = wintypes.BOOL
    user32.keybd_event.argtypes = (wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t)
    user32.keybd_event.restype = None
    user32.GetWindowDC.argtypes = (wintypes.HWND,)
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    user32.ReleaseDC.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = (wintypes.HDC,)
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = (wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT)
    gdi32.GetDIBits.restype = ctypes.c_int

    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                                    wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    def _process_name(pid: int) -> str:
        if not pid:
            return ""
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return Path(buffer.value).name
        finally:
            kernel32.CloseHandle(handle)
        return ""

    def _window_info(hwnd) -> Optional[WindowInfo]:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return None
        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        title = title_buf.value.strip()
        if not title:
            return None

        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, 256)

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        client = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(client))
        origin = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(origin))

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        return WindowInfo(
            wid=f"{int(hwnd):x}",
            title=title,
            app=_process_name(pid.value) or class_buf.value,
            pid=int(pid.value),
            left=rect.left, top=rect.top,
            width=max(0, rect.right - rect.left), height=max(0, rect.bottom - rect.top),
            client_left=origin.x, client_top=origin.y,
            client_width=max(0, client.right - client.left), client_height=max(0, client.bottom - client.top),
            minimized=bool(user32.IsIconic(hwnd)),
            maximized=bool(user32.IsZoomed(hwnd)),
            extra={"class": class_buf.value},
        )

    def list_windows(include_hidden: bool = False) -> List[WindowInfo]:
        windows: List[WindowInfo] = []
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            try:
                if not include_hidden and not user32.IsWindowVisible(hwnd):
                    return True
                # пропускаем служебные окна без владельца-приложения
                ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                if ex_style & WS_EX_TOOLWINDOW and not ex_style & WS_EX_APPWINDOW:
                    return True
                info = _window_info(hwnd)
                if info is not None:
                    windows.append(info)
            except Exception:
                pass
            return True

        proc = EnumProc(callback)
        user32.EnumWindows.argtypes = (EnumProc, wintypes.LPARAM)
        user32.EnumWindows.restype = wintypes.BOOL
        user32.EnumWindows(proc, 0)
        windows.sort(key=lambda w: (w.minimized, w.app.lower(), w.title.lower()))
        return windows

    def _hwnd_of(window: WindowInfo):
        return int(window.wid, 16)

    def _restore(hwnd) -> None:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.25)

    def _force_foreground(hwnd) -> bool:
        """SetForegroundWindow блокируется Windows, если окно неактивно — обходим."""
        _restore(hwnd)
        try:
            # трюк с Alt снимает блокировку фокуса
            user32.keybd_event(0x12, 0, 0, 0)      # Alt down
            user32.keybd_event(0x12, 0, 2, 0)      # Alt up
        except Exception:
            pass
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(fg, None)
        tid_target = user32.GetWindowThreadProcessId(hwnd, None)
        attached = False
        if tid_fg and tid_target and tid_fg != tid_target:
            attached = bool(user32.AttachThreadInput(tid_fg, tid_target, True))
        try:
            user32.BringWindowToTop(hwnd)
            ok = bool(user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(tid_fg, tid_target, False)
        if not ok:
            # ещё одна попытка: topmost на мгновение
            HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
            SWP_NOMOVE, SWP_NOSIZE = 0x0002, 0x0001
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
            user32.SetForegroundWindow(hwnd)
            ok = user32.GetForegroundWindow() == hwnd
        return ok or user32.GetForegroundWindow() == hwnd

    def set_window_state(window: WindowInfo, action: str) -> str:
        hwnd = _hwnd_of(window)
        if action == "maximize":
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
        elif action == "minimize":
            user32.ShowWindow(hwnd, SW_MINIMIZE)
        elif action in ("restore", "normal"):
            user32.ShowWindow(hwnd, SW_RESTORE)
        elif action == "close":
            WM_CLOSE = 0x0010
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        else:
            raise WindowError(f"Неизвестное действие с окном: {action}")
        updated = _window_info(hwnd)
        return updated.short() if updated else f"Действие «{action}» выполнено"


# ======================================================================
#  Linux (X11 / xdotool)
# ======================================================================
def _have(cmd: str) -> bool:
    from shutil import which

    return which(cmd) is not None


if IS_LINUX:  # pragma: no cover - только Linux
    def _xdo(*args: str) -> str:
        if not _have("xdotool"):
            raise WindowError("Для управления окнами на Linux установите xdotool: sudo apt install xdotool")
        proc = subprocess.run(["xdotool", *args], capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            raise WindowError(f"xdotool {args[0]}: {proc.stderr.strip()[:200]}")
        return proc.stdout.strip()

    def _window_info_by_id(wid: str) -> Optional[WindowInfo]:
        try:
            name = _xdo("getwindowname", wid)
        except WindowError:
            return None
        if not name.strip():
            return None
        geom = {}
        try:
            raw = _xdo("getwindowgeometry", "--shell", wid)
            for line in raw.splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    geom[key.strip()] = value.strip()
        except WindowError:
            pass
        return WindowInfo(
            wid=str(wid),
            title=name.strip(),
            app="",
            pid=0,
            left=int(geom.get("X", 0) or 0),
            top=int(geom.get("Y", 0) or 0),
            width=int(geom.get("WIDTH", 0) or 0),
            height=int(geom.get("HEIGHT", 0) or 0),
            client_left=int(geom.get("X", 0) or 0),
            client_top=int(geom.get("Y", 0) or 0),
            client_width=int(geom.get("WIDTH", 0) or 0),
            client_height=int(geom.get("HEIGHT", 0) or 0),
        )

    def list_windows(include_hidden: bool = False) -> List[WindowInfo]:
        if not _have("xdotool"):
            return []
        try:
            ids = _xdo("search", "--onlyvisible", "--name", ".").split()
        except WindowError:
            return []
        windows = [w for w in (_window_info_by_id(i) for i in ids) if w is not None]
        windows.sort(key=lambda w: w.title.lower())
        return windows

    def set_window_state(window: WindowInfo, action: str) -> str:
        mapping = {"maximize": "windowmaximize", "minimize": "windowminimize",
                   "restore": "windowmap", "normal": "windowmap"}
        if action == "close":
            _xdo("windowclose", window.wid)
            return f"Окно «{window.title}» закрыто"
        cmd = mapping.get(action)
        if not cmd:
            raise WindowError(f"Неизвестное действие с окном: {action}")
        _xdo(cmd, window.wid)
        return f"Окно «{window.title}»: {action}"


if IS_MAC:  # pragma: no cover - только macOS
    def list_windows(include_hidden: bool = False) -> List[WindowInfo]:
        script = ('tell application "System Events" to get name of every process '
                  'whose background only is false')
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        names = [n.strip() for n in (proc.stdout or "").split(",") if n.strip()]
        return [WindowInfo(wid=name, title=name, app=name) for name in names]

    def set_window_state(window: WindowInfo, action: str) -> str:
        app = window.app or window.title
        if action == "minimize":
            script = f'tell application "{app}" to set miniaturized of window 1 to true'
        elif action == "close":
            script = f'tell application "{app}" to close window 1'
        elif action in ("maximize", "restore", "normal"):
            script = f'tell application "{app}" to activate'
        else:
            raise WindowError(f"Неизвестное действие с окном: {action}")
        subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        return f"{app}: {action}"


if not (IS_WINDOWS or IS_LINUX or IS_MAC):
    def list_windows(include_hidden: bool = False) -> List[WindowInfo]:
        return []

    def set_window_state(window: WindowInfo, action: str) -> str:
        raise WindowError("Управление окнами не поддерживается на этой платформе")


# ======================================================================
#  Поиск нужного окна
# ======================================================================
BROWSER_APPS = ("chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe",
                "vivaldi.exe", "yandex.exe", "chromium.exe", "browser.exe",
                "chrome", "firefox", "chromium", "opera")
BROWSER_HINTS = ("chrome", "edge", "firefox", "opera", "brave", "vivaldi", "yandex",
                 "браузер", "browser", "хром")


def _norm(text: str) -> str:
    return (text or "").strip().lower()


def _score(window: WindowInfo, query: str) -> int:
    """Чем больше — тем лучше совпадение."""
    q = _norm(query)
    title = _norm(window.title)
    app = _norm(window.app)
    if not q:
        return 0
    score = 0
    if q == app or q == title:
        score += 100
    if q in app:
        score += 60
    if q in title:
        score += 40
    if app and app.startswith(q):
        score += 20
    # частичное совпадение по словам («яндекс» → «Яндекс Браузер»)
    for word in q.replace("-", " ").split():
        if len(word) > 2 and (word in title or word in app):
            score += 15
    return score


def find_window(query: str = "", index: int = 0) -> WindowInfo:
    """Найти окно по части заголовка/имени процесса. Пустой запрос — активное окно."""
    if IS_WINDOWS:
        hwnd = user32.GetForegroundWindow()
        if not query.strip():
            info = _window_info(hwnd) if hwnd else None
            if info:
                return info
            raise WindowError("Не удалось определить активное окно")

    windows = list_windows()
    if not windows:
        raise WindowError("Список окон пуст или недоступен на этой платформе")

    if not query.strip():
        return windows[0]

    scored = sorted(((_score(w, query), w) for w in windows), key=lambda pair: -pair[0])
    matches = [w for s, w in scored if s > 0]
    if not matches:
        available = ", ".join(f"«{w.title}»" for w in windows[:6])
        raise WindowError(f"Окно по запросу «{query}» не найдено. Открытые окна: {available}")
    # сначала не свёрнутые
    matches.sort(key=lambda w: w.minimized)
    idx = max(0, min(int(index or 0), len(matches) - 1))
    return matches[idx]


def find_windows(query: str) -> List[WindowInfo]:
    windows = list_windows()
    if not query.strip():
        return windows
    return [w for w in sorted(windows, key=lambda w: -_score(w, query)) if _score(w, query) > 0]


# ======================================================================
#  Публичные действия
# ======================================================================
def format_windows(limit: int = 40) -> str:
    windows = list_windows()
    if not windows:
        return "Список окон недоступен (нет доступа к оконной системе)."
    lines = [f"Открытых окон: {len(windows)}"]
    for window in windows[:limit]:
        lines.append(f"- {window.short()}")
    if len(windows) > limit:
        lines.append(f"… и ещё {len(windows) - limit}")
    return "\n".join(lines)


def focus_window(query: str = "", index: int = 0) -> WindowInfo:
    """Активировать окно и вернуть его геометрию."""
    window = find_window(query, index)
    if IS_WINDOWS:
        hwnd = _hwnd_of(window)
        if not _force_foreground(hwnd):
            raise WindowError(
                f"Windows не отдала фокус окну «{window.title}». Кликните по нему один раз и повторите."
            )
        time.sleep(0.2)
        return _window_info(hwnd) or window
    if IS_LINUX:
        _xdo("windowactivate", "--sync", window.wid)
        time.sleep(0.2)
        return _window_info_by_id(window.wid) or window
    if IS_MAC:
        app = window.app or window.title
        subprocess.run(["osascript", "-e", f'tell application "{app}" to activate'],
                       capture_output=True, text=True, timeout=15)
        return window
    raise WindowError("Активация окон не поддерживается")


def window_action(action: str, query: str = "") -> str:
    window = find_window(query)
    return set_window_state(window, action)


def client_point(window: WindowInfo, x: int, y: int) -> Tuple[int, int]:
    """Координаты внутри окна → координаты экрана."""
    return window.client_left + int(x), window.client_top + int(y)


def window_rect(window: WindowInfo) -> Tuple[int, int, int, int]:
    return window.left, window.top, window.left + window.width, window.top + window.height


def capture_window(window: WindowInfo, path: Path) -> Tuple[str, Tuple[int, int]]:
    """Снимок конкретного окна (Windows — PrintWindow, иначе по границам)."""
    try:
        from PIL import Image, ImageGrab  # type: ignore
    except ImportError as exc:
        raise WindowError("Для снимков окна установите Pillow: pip install Pillow") from exc

    path.parent.mkdir(parents=True, exist_ok=True)

    if IS_WINDOWS:
        hwnd = _hwnd_of(window)
        try:
            img = _print_window(hwnd, window.width, window.height)
            if img is not None:
                img.save(path)
                return str(path), img.size
        except Exception as exc:  # noqa: BLE001
            print(f"[window] PrintWindow не сработал ({exc}), снимаю по границам")
        try:
            img = ImageGrab.grab(bbox=window_rect(window), all_screens=True)
            img.save(path)
            return str(path), img.size
        except Exception as exc:  # noqa: BLE001
            raise WindowError(f"Не удалось снять окно: {exc}") from exc

    try:
        img = ImageGrab.grab(bbox=window_rect(window))
        img.save(path)
        return str(path), img.size
    except Exception as exc:  # noqa: BLE001
        raise WindowError(f"Не удалось снять окно: {exc}") from exc


if IS_WINDOWS:  # pragma: no cover
    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
        ]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    def _print_window(hwnd, width: int, height: int):
        """Снимок окна даже если оно перекрыто другими."""
        from PIL import Image  # type: ignore

        if width <= 0 or height <= 0:
            return None
        window_dc = user32.GetWindowDC(hwnd)
        if not window_dc:
            return None
        mem_dc = None
        bitmap = None
        try:
            mem_dc = gdi32.CreateCompatibleDC(window_dc)
            bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
            gdi32.SelectObject(mem_dc, bitmap)
            if not user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT):
                if not user32.PrintWindow(hwnd, mem_dc, 0):
                    return None
            info = _BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height       # сверху вниз
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0
            buffer = ctypes.create_string_buffer(width * height * 4)
            gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS)
            return Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1).convert("RGB")
        finally:
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(hwnd, window_dc)


# ======================================================================
#  Специальные сценарии
# ======================================================================
def open_in_browser(url: str, browser: str = "") -> str:
    """Открыть адрес в уже запущенном браузере (или запустить его)."""
    from . import os_control

    target = (url or "").strip()
    if not target:
        raise WindowError("Пустой адрес")
    if not target.startswith(("http://", "https://")):
        target = "https://" + target

    window: Optional[WindowInfo] = None
    candidates: List[str] = [browser] if browser else list(BROWSER_HINTS)
    for hint in candidates:
        if not hint:
            continue
        for found in find_windows(hint):
            if found.app.lower() in BROWSER_APPS or any(h in found.app.lower() for h in BROWSER_HINTS):
                window = found
                break
        if window:
            break

    if window is None:
        name = browser or "chrome"
        os_control.launch_app(name)
        time.sleep(2.2)
        for _ in range(10):
            try:
                window = find_window(browser or "chrome")
                break
            except WindowError:
                time.sleep(0.5)

    if window is None:
        os_control.open_target(target)
        return f"Браузер не найден — адрес открыт программой по умолчанию: {target}"

    focus_window(window.app or window.title)
    time.sleep(0.35)
    os_control.hotkey(["ctrl", "l"])
    time.sleep(0.15)
    os_control.type_text(target)
    time.sleep(0.15)
    os_control.press_key("enter")
    return f"В окне «{window.title}» открыт адрес {target}"
