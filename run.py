#!/usr/bin/env python3
"""Запуск Astra.

Использование:
    python run.py                 # сервер + браузер, спросит режим (чат или голосовой)
    python run.py --mode voice    # сразу голосовой режим со сферой
    python run.py --mode text     # сразу обычный чат
    python run.py --window        # отдельное нативное окно (нужен pywebview)
    python run.py --no-browser    # только сервер
    python run.py --port 9000     # другой порт
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def free_port(host: str, preferred: int) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 21)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def check_dependencies() -> list[str]:
    missing = []
    for module, package in (
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn[standard]"),
        ("httpx", "httpx"),
        ("numpy", "numpy"),
    ):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    return missing


OPTIONAL_HINTS = {
    "sounddevice": "голосовой ввод и озвучка (pip install sounddevice)",
    "soundfile": "декодирование mp3 для озвучки (pip install soundfile)",
    "faster_whisper": "распознавание речи (pip install faster-whisper)",
    "edge_tts": ("нейроголос (pip install edge-tts). Без него озвучка пойдёт "
                 "офлайн-голосом Windows — он не требует пакетов"),
    "PIL": "скриншоты (pip install Pillow)",
}


def print_banner(url: str) -> None:
    print(r"""
   ___         __
  / _ | ___ __/ /_____ _
 / __ |(_-</ _  / _ `/ _ `
/_/ |_/___/\_,_/\_,_/\_,_/   локальный ИИ-ассистент
""")
    print(f"  Интерфейс:  {url}")
    print("  Остановка:  Ctrl+C")
    print("  LM Studio:  Developer → Local Server → Start Server")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Astra — ИИ-ассистент с управлением компьютером")
    parser.add_argument("--host", default=None, help="Адрес (по умолчанию 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Порт (по умолчанию 8756)")
    parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер")
    parser.add_argument("--window", action="store_true", help="Открыть в отдельном окне (pywebview)")
    parser.add_argument("--reload", action="store_true", help="Автоперезапуск при изменении кода")
    parser.add_argument("--data-dir", default=None, help="Каталог данных (по умолчанию ./data)")
    parser.add_argument("--mode", choices=["text", "voice"], default=None,
                        help="Режим запуска: text — обычный чат, voice — голосовой режим со сферой "
                             "(без флага режим выбирается на экране приветствия)")
    parser.add_argument("--lm-url", default=None,
                        help="Приоритетный адрес LM Studio, например 192.168.0.32:1234 "
                             "(добавляется первым в список; автовыбор остаётся включённым)")
    args = parser.parse_args()

    if args.data_dir:
        os.environ["ASTRA_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())

    missing = check_dependencies()
    if missing:
        print("Не хватает обязательных пакетов:", ", ".join(missing))
        print("Установите: pip install -r requirements.txt")
        return 1

    from astra.config import config
    from astra.endpoints import normalize_url
    from astra.paths import DATA_DIR, ensure_dirs

    ensure_dirs()

    if args.lm_url:
        url = normalize_url(args.lm_url)
        candidates = [url] + [c for c in (config.get("lmstudio.candidates") or []) if normalize_url(c) != url]
        config.patch({"lmstudio": {"candidates": candidates, "auto_detect": True}})
        print(f"  Приоритетный сервер LM Studio: {url}")
    if args.mode:
        config.patch({"ui": {"mode": args.mode, "last_mode": args.mode, "ask_mode": False}})
        print(f"  Режим запуска: {'голосовой (сфера)' if args.mode == 'voice' else 'обычный чат'}")

    # какой экран открыть сразу: явный флаг → сохранённый выбор → спросить в интерфейсе
    start_mode = args.mode or ""
    if not start_mode:
        if config.get("ui.ask_mode", True):
            start_mode = ""                                  # спросим на экране выбора
        else:
            start_mode = config.get("ui.mode") or config.get("ui.last_mode") or "text"

    host = args.host or config.get("server.host") or "127.0.0.1"
    preferred = args.port or int(config.get("server.port") or 8756)
    port = free_port(host, preferred)
    if port != preferred:
        print(f"Порт {preferred} занят — использую {port}")
    config.set("server.port", port)

    base_url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    url = base_url + (f"/?mode={start_mode}" if start_mode else "/")
    print_banner(base_url)
    if not start_mode:
        print("  Режим: спрошу при открытии — обычный чат или голосовой (сфера)")

    # подсказки по необязательным пакетам
    hints = []
    for module, hint in OPTIONAL_HINTS.items():
        try:
            __import__(module)
        except ImportError:
            hints.append(f"  • {hint}")
    if hints:
        print("  Не установлено (часть функций будет недоступна):")
        print("\n".join(hints))
        print()

    print(f"  Данные: {DATA_DIR}")
    candidates = config.get("lmstudio.candidates") or []
    print("  Серверы LM Studio (по приоритету): " + (", ".join(candidates) or "не заданы") + "\n")

    import uvicorn

    from astra.server.app import app

    uvicorn_kwargs = dict(
        app=app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        reload=args.reload,
    )

    if args.window:
        try:
            import webview  # type: ignore
        except ImportError:
            print("pywebview не установлен — открываю в браузере. Установите: pip install pywebview")
            args.window = False
        else:
            server = uvicorn.Server(uvicorn.Config(**uvicorn_kwargs))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            time.sleep(1.6)

            # голосовой режим живёт в квадратном окне — как просил пользователь
            square = int(config.get("ui.voice_window_size") or 760)
            voice_window = (start_mode == "voice" and config.get("ui.voice_square_window", True))
            width, height = (square, square) if voice_window else (1280, 860)
            window = webview.create_window("Astra", url, width=width, height=height,
                                           min_size=(420, 420))

            from astra.server.app import app as fastapi_app
            fastapi_app.state.native_window = True      # интерфейс узнает, что окно нативное

            stop_watch = threading.Event()

            def watch_window_size() -> None:
                """Меняем размер окна, когда пользователь переключает режим в интерфейсе."""
                endpoint = base_url + "/api/window/next"
                while not stop_watch.is_set():
                    try:
                        with urllib.request.urlopen(endpoint, timeout=2.5) as resp:
                            data = json.loads(resp.read().decode("utf-8"))
                        request = data.get("request") or {}
                        w = int(request.get("width") or 0)
                        h = int(request.get("height") or 0)
                        if w >= 420 and h >= 420:
                            window.resize(w, h)
                    except Exception:
                        pass
                    stop_watch.wait(0.8)

            threading.Thread(target=watch_window_size, daemon=True).start()
            webview.start()
            stop_watch.set()
            server.should_exit = True
            thread.join(timeout=5)
            return 0

    if not args.no_browser and config.get("server.open_browser", True):
        def open_later() -> None:
            time.sleep(1.4)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=open_later, daemon=True).start()

    try:
        uvicorn.run(**uvicorn_kwargs)
    except KeyboardInterrupt:
        print("\nДо встречи!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
