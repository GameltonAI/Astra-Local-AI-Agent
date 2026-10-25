"""Конфигурация приложения: значения по умолчанию + сохранение в data/config.json.

Настройки правятся из интерфейса (и частично из кода) и переживают перезапуск.
Доступ: cfg.get("voice.tts_voice"), cfg.patch({"lmstudio": {"model": "..."}}) -> save().
"""
from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any, Dict

from .paths import CONFIG_FILE, ensure_dirs

DEFAULTS: Dict[str, Any] = {
    "lmstudio": {
        # Автовыбор сервера: сначала локальная сеть, иначе этот компьютер
        "auto_detect": True,
        # Кандидаты в порядке приоритета (можно добавить свои)
        "candidates": [
            "http://192.168.0.32:1234/v1",
            "http://127.0.0.1:1234/v1",
        ],
        # Сколько секунд ждать ответа при проверке доступности
        "probe_timeout": 2.5,
        # Как часто перепроверять активный сервер (сек)
        "endpoint_ttl": 30,
        # Адрес, используемый когда auto_detect выключен
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key": "lm-studio",
        # Пусто = взять первую доступную модель при старте
        "model": "",
        "temperature": 0.35,
        "max_tokens": 1400,
        "timeout": 180,
        "context_messages": 24,
        # Зрение: auto — пробовать и откатываться на текст, on — всегда, off — никогда
        "vision": "auto",
        "vision_max_px": 1024,        # до какого размера сжимать снимок
        "vision_quality": 80,         # качество JPEG
        "vision_max_images": 2,       # сколько снимков максимум в одном запросе
        "extra_body": {},  # произвольные поля в тело запроса, например {"top_p": 0.9}
    },
    "agent": {
        "max_steps": 6,
        # После каждого действия в окне автоматически снимать это окно и отдавать модели
        "auto_window_screenshot": True,
        # Проверять результат действия: сравнить снимок до и после, честно сказать «не сработало»
        "visual_verify": True,
        "verify_min_diff": 0.0002,     # доля изменившихся пикселей, ниже которой это шум
        "grid_step": 100,              # шаг координатной сетки в see_window (0 — авто)
        # Останавливаться, если на экране капча (решает только человек)
        "stop_on_captcha": True,
        "language": "ru",
        "auto_title": True,
        "stream_thoughts": True,
    },
    "voice": {
        "stt_engine": "faster-whisper",
        # Большая модель распознаёт русскую речь заметно точнее маленькой.
        # large-v3 ~3 ГБ, скачается один раз при первом голосовом вводе.
        "whisper_model": "large-v3",
        "whisper_compute_type": "auto",
        "whisper_device": "auto",
        "whisper_beam_size": 5,
        # подсказка распознавателю: домен и язык — меньше путаницы в коротких командах
        "whisper_initial_prompt": "Команды ИИ-ассистенту на русском языке. Открой блокнот, найди отзывы, введи текст.",
        "stt_language": "ru",
        # голосовой режим: слушать непрерывно, замирая только на время ответа и действий
        "continuous_listen": True,
        "tts_enabled": True,
        "tts_always": False,
        # auto = edge-tts, при сбое офлайн-голос Windows (SAPI), затем pyttsx3
        "tts_engine": "auto",
        "tts_voice": "ru-RU-DmitryNeural",
        "sapi_voice": "",          # пусто — голос Windows по умолчанию
        "tts_rate": "+8%",
        "tts_volume": "+0%",
        "input_device": "",
        "output_device": "",
        "vad_threshold": 0.015,
        "silence_ms": 1100,
        "max_record_s": 30,
        "auto_send_voice": True,
        "listen_on_start": False,
        # сколько последних статусов показывать под сферой
        "status_history": 4,
    },
    "safety": {
        "confirm_shell": True,      # run_command
        "confirm_power": True,      # выключение/перезагрузка/блокировка
        "confirm_files": True,      # запись и удаление файлов
        "confirm_typing": True,     # ввод текста, клики мышью
        "allowed_tools": [],        # инструменты без подтверждения (выбирается в интерфейсе)
    },
    "ui": {
        "theme": "dark",
        "orbs": True,
        "send_with_enter": True,
        # Режим работы: "" — спросить при запуске, "text" — обычный, "voice" — голосовой
        "mode": "",
        "ask_mode": True,
        # голосовой режим открывается в квадратном окне (pywebview / отдельное окно)
        "voice_square_window": True,
        "voice_window_size": 760,
        "last_mode": "text",
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8756,
        "open_browser": True,
    },
}


# Одноразовые миграции настроек: (ключ-флаг, функция(data) -> изменено ли что-то)
def _migrate_stt_model(data: Dict[str, Any]) -> bool:
    """Модель распознавания по умолчанию была «small» — она часто путалась.

    Если пользователь её не менял (то есть в конфиге осталось старое значение
    по умолчанию), один раз поднимаем до large-v3. Явный выбор tiny/base/medium/large
    не трогаем.
    """
    voice = data.get("voice")
    if not isinstance(voice, dict):
        return False
    if voice.get("stt_model_upgraded"):
        return False
    voice["stt_model_upgraded"] = True
    if str(voice.get("whisper_model") or "").strip().lower() == "small":
        voice["whisper_model"] = "large-v3"
        return True
    return False


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Потокобезопасная обёртка над словарём настроек."""

    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    # --- io -------------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            data = copy.deepcopy(DEFAULTS)
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data = _deep_merge(data, raw)
                except Exception as exc:  # битый файл не должен ломать запуск
                    print(f"[config] не удалось прочитать {self.path}: {exc}")
            migrated = _migrate_stt_model(data)
            self._data = data
            # страховка от мусора в критичных полях
            if not isinstance(self._data.get("lmstudio"), dict):
                self._data["lmstudio"] = copy.deepcopy(DEFAULTS["lmstudio"])
            if migrated:
                print("[config] модель распознавания речи обновлена до large-v3")
                try:
                    self.save()
                except Exception as exc:  # noqa: BLE001
                    print(f"[config] не удалось сохранить миграцию: {exc}")

    def save(self) -> None:
        with self._lock:
            ensure_dirs()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    # --- доступ ---------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return copy.deepcopy(node)

    def set(self, dotted: str, value: Any, save: bool = True) -> None:
        with self._lock:
            parts = dotted.split(".")
            node = self._data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
                if not isinstance(node, dict):
                    raise ValueError(f"Конфликт ключей в конфиге: {dotted}")
            node[parts[-1]] = value
            if save:
                self.save()

    def patch(self, values: Dict[str, Any], save: bool = True) -> Dict[str, Any]:
        """Глубокое слияние и сохранение. Возвращает актуальные настройки."""
        with self._lock:
            self._data = _deep_merge(self._data, values or {})
            if save:
                self.save()
            return copy.deepcopy(self._data)

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def public(self) -> Dict[str, Any]:
        """Настройки для фронтенда (без потенциальных секретов)."""
        data = self.as_dict()
        data.get("lmstudio", {}).pop("api_key", None)
        return data


config = Config()
