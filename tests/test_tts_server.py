"""Серверная часть озвучки: события в UI, диагностика, /api/tts/*.

Проверяем главное для жалобы «голосовой ввод работает, а ответ не озвучивается»:
любой исход озвучки доходит до интерфейса событиями, а не молчанием.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRA_DATA_DIR", "/tmp/astra_pytest_data")

from fastapi.testclient import TestClient  # noqa: E402

from astra.server import app as app_mod  # noqa: E402


class FakeConn:
    def __init__(self) -> None:
        self.events = []
        self.levels = []

    def emit(self, event):
        self.events.append(event)

    def emit_level(self, kind, value):
        self.levels.append((kind, value))

    def types(self):
        return [e["type"] for e in self.events]

    def first(self, type_name):
        return next((e for e in self.events if e["type"] == type_name), None)


@pytest.fixture()
def enabled_tts(monkeypatch):
    monkeypatch.setitem(app_mod.config._data["voice"], "tts_enabled", True)
    monkeypatch.setitem(app_mod.config._data["voice"], "tts_engine", "auto")
    return app_mod.config


# ======================================================================
#  speak_text: успех, фолбэк, полный отказ
# ======================================================================
def test_speak_text_success_emits_tts_event(enabled_tts, monkeypatch):
    called = {}

    async def fake_speak(text, **kwargs):
        called.update(kwargs, text=text)
        return {"ok": True, "engine": "edge-tts", "engine_label": "edge-tts · ru-RU-DmitryNeural",
                "duration": 1.2, "fallback_from": "", "error": ""}

    monkeypatch.setattr(app_mod.tts, "speak", fake_speak)
    conn = FakeConn()
    asyncio.run(app_mod.speak_text(conn, "Привет! **как дела**", "voice"))

    assert called["text"] == "Привет! как дела"        # markdown вычищен
    assert called["engine"] == "auto"
    states = [e.get("state") for e in conn.events if e["type"] == "state"]
    assert states == ["speaking", "idle"]  # анимация кружка всегда возвращается в покой
    ok = conn.first("tts")
    assert ok["ok"] is True and ok["engine"] == "edge-tts"
    assert "status" in ok
    assert conn.first("tts_error") is None


def test_speak_text_offline_fallback_reports_toast(enabled_tts, monkeypatch):
    async def fake_speak(text, **kwargs):
        return {"ok": True, "engine": "sapi", "engine_label": "офлайн-голос Windows (SAPI)",
                "duration": 0.8, "fallback_from": "edge-tts", "error": "edge-tts: нет сети"}

    monkeypatch.setattr(app_mod.tts, "speak", fake_speak)
    conn = FakeConn()
    asyncio.run(app_mod.speak_text(conn, "Привет", "voice"))

    toast = conn.first("toast")
    assert toast is not None
    assert "офлайн" in toast["message"].lower()
    assert conn.first("tts")["engine"] == "sapi"


def test_speak_text_failure_explains_reason(enabled_tts, monkeypatch):
    async def fake_speak(text, **kwargs):
        return {"ok": False, "engine": "", "engine_label": "", "duration": 0.0,
                "fallback_from": "", "error": "Не удалось открыть устройство вывода звука: boom"}

    monkeypatch.setattr(app_mod.tts, "speak", fake_speak)
    conn = FakeConn()
    asyncio.run(app_mod.speak_text(conn, "Привет", "voice"))

    assert conn.first("tts")["ok"] is False
    err = conn.first("tts_error")
    assert err is not None
    assert "устройство вывода" in err["message"].lower()
    assert "boom" in err["detail"]
    states = [e.get("state") for e in conn.events if e["type"] == "state"]
    assert states == ["speaking", "idle"]  # не залипаем в speaking


def test_speak_text_never_raises(enabled_tts, monkeypatch):
    async def boom(text, **kwargs):
        raise RuntimeError("неожиданный сбой движка")

    monkeypatch.setattr(app_mod.tts, "speak", boom)
    conn = FakeConn()
    asyncio.run(app_mod.speak_text(conn, "Привет", "voice"))
    assert conn.first("tts")["ok"] is False
    assert "неожиданный сбой" in conn.first("tts_error")["detail"]


def test_speak_text_skips_empty(enabled_tts, monkeypatch):
    async def fake_speak(text, **kwargs):  # pragma: no cover
        raise AssertionError("не должно вызываться")

    monkeypatch.setattr(app_mod.tts, "speak", fake_speak)
    conn = FakeConn()
    asyncio.run(app_mod.speak_text(conn, "   \n  ", "voice"))
    assert conn.events == []


@pytest.mark.parametrize(
    "error, expected",
    [
        ("Модуль edge-tts не установлен: pip install edge-tts", "pip install edge-tts"),
        ("Не удалось открыть устройство вывода звука: PortAudio", "устройство вывода"),
        ("Не удалось синтезировать речь (проверьте интернет и голос)", "офлайн"),
        ("что-то странное", "Настройки"),
    ],
)
def test_tts_hint_mapping(error, expected):
    assert expected.lower() in app_mod.tts_hint(error).lower()


def test_tts_hint_no_audio_is_not_device_error():
    """«No audio was received» от edge-tts — это сеть/сервис, а не проблема колонок."""
    hint = app_mod.tts_hint("edge-tts: No audio was received. Please verify that your parameters are correct.")
    assert "устройство вывода" not in hint.lower()
    assert "офлайн" in hint.lower()


# ======================================================================
#  REST: /api/tts/status и /api/tts/test
# ======================================================================
def test_tts_status_endpoint(enabled_tts, monkeypatch):
    monkeypatch.setattr(app_mod.tts, "available_engines",
                        lambda: {"edge-tts": True, "sapi": True, "pyttsx3": False})
    monkeypatch.setattr(app_mod, "list_devices", lambda: [{"index": 1, "name": "Колонки"}])
    client = TestClient(app_mod.app)
    data = client.get("/api/tts/status").json()

    assert data["engines"] == {"edge-tts": True, "sapi": True, "pyttsx3": False}
    assert data["any_available"] is True
    assert data["offline_capable"] is True
    assert data["audio_output_ok"] is True
    assert data["engine_setting"] == "auto"
    assert data["enabled"] is True


def test_tts_status_reports_missing_audio_device(enabled_tts, monkeypatch):
    def no_audio():
        raise app_mod.AudioUnavailable("sounddevice не видит аудиоустройства")

    monkeypatch.setattr(app_mod, "list_devices", no_audio)
    client = TestClient(app_mod.app)
    data = client.get("/api/tts/status").json()
    assert data["audio_output_ok"] is False
    assert "sounddevice" in data["audio_error"]


def test_tts_test_endpoint_success(enabled_tts, monkeypatch):
    async def fake_speak(text, **kwargs):
        assert "Проверка голоса" in text
        return {"ok": True, "engine": "edge-tts", "engine_label": "edge-tts · ru-RU-DmitryNeural",
                "duration": 1.0, "fallback_from": "", "error": ""}

    monkeypatch.setattr(app_mod.tts, "speak", fake_speak)
    client = TestClient(app_mod.app)
    data = client.post("/api/tts/test").json()
    assert data["ok"] is True
    assert data["engine"] == "edge-tts"
    assert data["label"]
    assert "status" in data


def test_tts_test_endpoint_failure_has_hint(enabled_tts, monkeypatch):
    async def fake_speak(text, **kwargs):
        return {"ok": False, "engine": "", "engine_label": "", "duration": 0.0,
                "fallback_from": "", "error": "Не удалось синтезировать речь (проверьте интернет и голос)"}

    monkeypatch.setattr(app_mod.tts, "speak", fake_speak)
    client = TestClient(app_mod.app)
    data = client.post("/api/tts/test").json()
    assert data["ok"] is False
    assert "офлайн" in data["hint"].lower()
    assert data["error"]


def test_health_contains_tts_block(enabled_tts, monkeypatch):
    monkeypatch.setattr(app_mod.tts, "available_engines",
                        lambda: {"edge-tts": True, "sapi": False, "pyttsx3": False})
    with TestClient(app_mod.app) as client:  # lifespan создаёт БД
        data = client.get("/api/health").json()
    tts = data["tts"]
    assert tts["engine"] == "auto"
    assert tts["enabled"] is True
    assert tts["engines"]["edge-tts"] is True
