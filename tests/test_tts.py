"""Тесты озвучки: цепочка движков, офлайн-резерв Windows (SAPI), диагностика.

Главная проверка: если edge-tts не работает (нет интернета / сервис лежит),
ответ всё равно звучит — офлайн-голосом Windows, а не молчанием.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRA_DATA_DIR", "/tmp/astra_pytest_data")

from astra.speech import tts as tts_mod  # noqa: E402

# пакет astra.speech экспортирует готовый экземпляр tts, поэтому модуль берём из sys.modules
tts_mod = sys.modules["astra.speech.tts"]


# ----------------------------------------------------------------------
#  Вспомогательные объекты
# ----------------------------------------------------------------------
class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    """Чистый экземпляр TTS с изолированным кэшем."""
    monkeypatch.setattr(tts_mod, "TTS_CACHE_DIR", tmp_path)
    monkeypatch.setattr(tts_mod, "ensure_dirs", lambda: None)
    inst = tts_mod.TextToSpeech()
    return inst


def _engines(edge: bool, sapi: bool = False, pyttsx3: bool = False):
    return {"edge-tts": edge, "sapi": sapi, "pyttsx3": pyttsx3}


# ======================================================================
#  Определение доступных движков
# ======================================================================
def test_sapi_unavailable_off_windows(engine, monkeypatch):
    monkeypatch.setattr(tts_mod.sys, "platform", "linux")
    assert engine.sapi_available() is False


def test_sapi_available_windows_with_powershell(engine, monkeypatch):
    monkeypatch.setattr(tts_mod.sys, "platform", "win32")
    monkeypatch.setattr(tts_mod.shutil, "which", lambda name: "C:/Windows/powershell.exe")
    assert engine.sapi_available() is True


def test_sapi_unavailable_windows_without_powershell(engine, monkeypatch):
    monkeypatch.setattr(tts_mod.sys, "platform", "win32")
    monkeypatch.setattr(tts_mod.shutil, "which", lambda name: None)
    assert engine.sapi_available() is False


def test_available_engines_has_three_keys(engine, monkeypatch):
    monkeypatch.setattr(engine, "sapi_available", staticmethod(lambda: True))
    monkeypatch.setattr(engine, "pyttsx3_available", staticmethod(lambda: False))
    assert engine.available_engines() == _engines(True, True, False)


# ======================================================================
#  Порядок движков (цепочка фолбэков)
# ======================================================================
@pytest.mark.parametrize(
    "setting, edge, sapi, pyttsx3, expected",
    [
        ("auto", True, True, False, ["edge-tts", "sapi"]),
        ("auto", False, True, False, ["sapi"]),
        ("auto", True, False, False, ["edge-tts"]),
        ("edge-tts", True, True, True, ["edge-tts", "sapi", "pyttsx3"]),
        ("sapi", True, True, True, ["sapi", "edge-tts", "pyttsx3"]),
        ("pyttsx3", True, True, True, ["pyttsx3", "sapi", "edge-tts"]),
        ("sapi", True, False, False, ["edge-tts"]),  # явный sapi, но его нет
        ("unknown-engine", True, True, False, ["edge-tts", "sapi"]),
        ("auto", False, False, False, []),
    ],
)
def test_engine_chain(engine, monkeypatch, setting, edge, sapi, pyttsx3, expected):
    monkeypatch.setattr(engine, "available_engines", lambda: _engines(edge, sapi, pyttsx3))
    assert engine._engine_chain(setting) == expected


def test_auto_prefers_edge(engine, monkeypatch):
    monkeypatch.setattr(engine, "available_engines", lambda: _engines(True, True, True))
    assert engine._engine_chain("auto")[0] == "edge-tts"


# ======================================================================
#  speak(): основной сценарий и фолбэк
# ======================================================================
def _patch_playback(engine, monkeypatch, record=None):
    """Подменяем декодирование и воспроизведение — в песочнице нет звука."""
    played = record if record is not None else []

    def fake_decode(path):
        return np.zeros(1200, dtype=np.float32), 24000

    def fake_play(audio, samplerate, on_level, device):
        played.append((samplerate, device))
        if on_level:
            on_level(0.5)

    monkeypatch.setattr(tts_mod, "_decode_audio", fake_decode)
    monkeypatch.setattr(engine, "_play", fake_play)
    return played


def test_speak_edge_success(engine, monkeypatch):
    """Обычный путь: edge-tts работает."""
    cache_file = tts_mod.TTS_CACHE_DIR / "x.mp3"
    cache_file.write_bytes(b"0" * 2048)

    async def fake_synth(text, voice, rate, volume):
        return cache_file

    monkeypatch.setattr(engine, "available_engines", lambda: _engines(True, True))
    monkeypatch.setattr(engine, "synthesize", fake_synth)
    _patch_playback(engine, monkeypatch)

    result = asyncio.run(engine.speak("привет", voice="ru-RU-DmitryNeural"))
    assert result["ok"] is True
    assert result["engine"] == "edge-tts"
    assert result["fallback_from"] == ""
    assert "Dmitry" in result["engine_label"]
    assert engine.status()["last_engine"] == "edge-tts"
    assert engine.status()["last_error"] == ""


def test_speak_falls_back_to_sapi_without_internet(engine, monkeypatch):
    """Нет интернета → голос всё равно звучит, офлайн-движком Windows."""
    wav = tts_mod.TTS_CACHE_DIR / "y.wav"
    wav.write_bytes(b"0" * 2048)

    async def broken_edge(text, voice, rate, volume):
        raise tts_mod.TTSError("Не удалось синтезировать речь (проверьте интернет и голос): timeout")

    async def fake_sapi(text, voice="", rate_percent=0, volume_percent=100):
        return wav

    monkeypatch.setattr(engine, "available_engines", lambda: _engines(True, True))
    monkeypatch.setattr(engine, "synthesize", broken_edge)
    monkeypatch.setattr(engine, "synthesize_sapi", fake_sapi)
    _patch_playback(engine, monkeypatch)

    result = asyncio.run(engine.speak("привет", voice="ru-RU-DmitryNeural", engine="auto"))
    assert result["ok"] is True, result
    assert result["engine"] == "sapi"
    assert result["fallback_from"] == "edge-tts"
    assert "офлайн" in result["engine_label"].lower()
    assert "edge-tts" in result["error"]  # причина сохранена для диагностики


def test_speak_offline_only_when_python_has_no_edge(engine, monkeypatch):
    """edge-tts не установлен вовсе — сразу SAPI, без попыток сети."""
    wav = tts_mod.TTS_CACHE_DIR / "z.wav"
    wav.write_bytes(b"0" * 2048)
    calls = []

    async def fake_sapi(text, voice="", rate_percent=0, volume_percent=100):
        calls.append(text)
        return wav

    monkeypatch.setattr(engine, "available_engines", lambda: _engines(False, True))
    monkeypatch.setattr(engine, "synthesize_sapi", fake_sapi)
    _patch_playback(engine, monkeypatch)

    result = asyncio.run(engine.speak("проверка", voice="ru-RU-DmitryNeural"))
    assert result["ok"] is True
    assert result["engine"] == "sapi"
    assert result["fallback_from"] == ""
    assert calls == ["проверка"]


def test_speak_explicit_sapi_first(engine, monkeypatch):
    """Пользователь выбрал офлайн-движок — он идёт первым."""
    wav = tts_mod.TTS_CACHE_DIR / "w.wav"
    wav.write_bytes(b"0" * 2048)

    async def fake_sapi(text, voice="", rate_percent=0, volume_percent=100):
        return wav

    monkeypatch.setattr(engine, "available_engines", lambda: _engines(True, True))
    monkeypatch.setattr(engine, "synthesize_sapi", fake_sapi)
    _patch_playback(engine, monkeypatch)
    monkeypatch.setattr(engine, "synthesize", None)  # edge не должен вызываться

    result = asyncio.run(engine.speak("раз", voice="ru-RU-DmitryNeural", engine="sapi"))
    assert result["ok"] is True
    assert result["engine"] == "sapi"
    assert result["fallback_from"] == ""


def test_speak_no_engines_returns_error_not_exception(engine, monkeypatch):
    monkeypatch.setattr(engine, "available_engines", lambda: _engines(False, False, False))
    result = asyncio.run(engine.speak("привет", voice="ru-RU-DmitryNeural"))
    assert result["ok"] is False
    assert "движк" in result["error"].lower()
    assert "pip install edge-tts" in result["error"]
    assert engine.status()["last_error"]


def test_speak_all_engines_fail(engine, monkeypatch):
    async def broken(text, voice, rate, volume):
        raise tts_mod.TTSError("сеть недоступна")

    async def broken_sapi(text, voice="", rate_percent=0, volume_percent=100):
        raise tts_mod.TTSError("офлайн-синтез Windows не сработал")

    monkeypatch.setattr(engine, "available_engines", lambda: _engines(True, True))
    monkeypatch.setattr(engine, "synthesize", broken)
    monkeypatch.setattr(engine, "synthesize_sapi", broken_sapi)
    _patch_playback(engine, monkeypatch)

    result = asyncio.run(engine.speak("привет", voice="ru-RU-DmitryNeural"))
    assert result["ok"] is False
    assert "edge-tts" in result["error"] and "sapi" in result["error"]
    assert engine.status()["last_error_engine"] == "sapi"


def test_speak_audio_device_error_mentions_output(engine, monkeypatch):
    """Ошибка устройства вывода не должна теряться — по ней строится подсказка."""
    cache_file = tts_mod.TTS_CACHE_DIR / "d.mp3"
    cache_file.write_bytes(b"0" * 2048)

    async def fake_synth(text, voice, rate, volume):
        return cache_file

    def bad_play(audio, samplerate, on_level, device):
        raise tts_mod.TTSError("Не удалось открыть устройство вывода звука: PortAudioError")

    monkeypatch.setattr(engine, "available_engines", lambda: _engines(True))
    monkeypatch.setattr(engine, "synthesize", fake_synth)
    monkeypatch.setattr(tts_mod, "_decode_audio", lambda p: (np.zeros(10, np.float32), 24000))
    monkeypatch.setattr(engine, "_play", bad_play)

    result = asyncio.run(engine.speak("привет", voice="ru-RU-DmitryNeural"))
    assert result["ok"] is False
    assert "устройство вывода" in result["error"]


def test_speak_empty_text(engine):
    result = asyncio.run(engine.speak("   ", voice="ru-RU-DmitryNeural"))
    assert result["ok"] is False
    assert result["error"] == "Пустой текст"


def test_speak_reports_stop_level_zero(engine, monkeypatch):
    """В конце озвучки уровень анимации сбрасывается в ноль."""
    wav = tts_mod.TTS_CACHE_DIR / "s.wav"
    wav.write_bytes(b"0" * 2048)

    async def fake_sapi(text, voice="", rate_percent=0, volume_percent=100):
        return wav

    levels = []
    monkeypatch.setattr(engine, "available_engines", lambda: _engines(False, True))
    monkeypatch.setattr(engine, "synthesize_sapi", fake_sapi)
    _patch_playback(engine, monkeypatch)
    monkeypatch.setattr(engine, "_play", lambda a, s, cb, d: cb(0.7))

    asyncio.run(engine.speak("тест", voice="ru-RU-DmitryNeural",
                             on_level=levels.append))
    assert levels[-1] == 0.0


def test_sapi_uses_direct_speech_when_audio_device_broken(engine, monkeypatch):
    """sounddevice не видит колонки — всё равно говорим, силами самого Windows."""
    wav = tts_mod.TTS_CACHE_DIR / "play_fail.wav"
    wav.write_bytes(b"0" * 2048)
    direct_calls = []

    async def fake_sapi(text, voice="", rate_percent=0, volume_percent=100):
        return wav

    async def fake_direct(text, voice="", rate_percent=0, volume_percent=100):
        direct_calls.append((text, rate_percent, volume_percent))

    def bad_play(audio, samplerate, on_level, device):
        raise tts_mod.TTSError("Не удалось открыть устройство вывода звука: PortAudioError")

    monkeypatch.setattr(engine, "available_engines", lambda: _engines(False, True))
    monkeypatch.setattr(engine, "synthesize_sapi", fake_sapi)
    monkeypatch.setattr(engine, "speak_sapi_direct", fake_direct)
    monkeypatch.setattr(tts_mod, "_decode_audio", lambda p: (np.zeros(10, np.float32), 24000))
    monkeypatch.setattr(engine, "_play", bad_play)

    result = asyncio.run(engine.speak("привет", voice="ru-RU-DmitryNeural",
                                      rate="+8%", volume="+0%"))
    assert result["ok"] is True, result
    assert result["engine"] == "sapi"
    assert "прямое" in result["engine_label"].lower()
    assert direct_calls == [("привет", 8, 100)]


def test_speak_sapi_direct_runs_powershell(engine, monkeypatch):
    monkeypatch.setattr(engine, "_powershell_exe", staticmethod(lambda: "powershell"))
    capture = {}

    def fake_run(cmd, **kwargs):
        capture["script"] = cmd[-1]
        start = cmd[-1].index("[IO.File]::ReadAllText('") + len("[IO.File]::ReadAllText('")
        text_file = cmd[-1][start:cmd[-1].index("'", start)]
        capture["text"] = Path(text_file).read_text(encoding="utf-8-sig")
        return FakeProc(0)

    monkeypatch.setattr(tts_mod.subprocess, "run", fake_run)
    asyncio.run(engine.speak_sapi_direct("Привет", rate_percent=4, volume_percent=90))
    assert "$synth.Speak($text)" in capture["script"]
    assert "SetOutputToWaveFile" not in capture["script"]  # без файла и без sounddevice
    assert capture["text"] == "Привет"


def test_speak_sapi_direct_requires_powershell(engine, monkeypatch):
    monkeypatch.setattr(engine, "_powershell_exe", staticmethod(lambda: ""))
    with pytest.raises(tts_mod.TTSError):
        asyncio.run(engine.speak_sapi_direct("Привет"))


def test_powershell_exe_prefers_path(engine, monkeypatch):
    monkeypatch.setattr(tts_mod.shutil, "which", lambda name: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    assert engine._powershell_exe().lower().endswith("powershell.exe")


# ======================================================================
#  Офлайн-синтез SAPI
# ======================================================================
def _ps_paths(script: str):
    """Достаём из PowerShell-скрипта путь WAV и путь файла с текстом."""
    out_start = script.index("SetOutputToWaveFile('") + len("SetOutputToWaveFile('")
    out = script[out_start:script.index("')", out_start)]
    text_start = script.index("[IO.File]::ReadAllText('") + len("[IO.File]::ReadAllText('")
    text = script[text_start:script.index("'", text_start)]
    return out, text


def _fake_ps(calls=None, returncode=0, stderr="", capture=None):
    def fake_run(cmd, **kwargs):
        script = cmd[-1]
        if calls is not None:
            calls.append(script)
        out, text_file = _ps_paths(script)
        if capture is not None:
            capture["script"] = script
            capture["text"] = Path(text_file).read_text(encoding="utf-8-sig")
        if returncode == 0:
            Path(out).write_bytes(b"RIFF" + b"0" * 2048)
        return FakeProc(returncode, "", stderr)

    return fake_run


def test_synthesize_sapi_writes_wav(engine, monkeypatch):
    monkeypatch.setattr(engine, "sapi_available", staticmethod(lambda: True))
    monkeypatch.setattr(tts_mod.shutil, "which", lambda name: "powershell")
    capture = {}
    monkeypatch.setattr(tts_mod.subprocess, "run", _fake_ps(capture=capture))

    path = asyncio.run(engine.synthesize_sapi("Привет", rate_percent=99, volume_percent=250))
    assert path.exists() and path.suffix == ".wav"
    script = capture["script"]
    assert "$synth.Rate = 10" in script       # ограничение -10..10
    assert "$synth.Volume = 100" in script    # ограничение 0..100
    assert "System.Speech" in script
    assert capture["text"] == "Привет"        # текст дошёл целиком


def test_synthesize_sapi_multiline_text(engine, monkeypatch):
    """Многострочный ответ не ломает скрипт: текст идёт файлом, а не в кавычки."""
    monkeypatch.setattr(engine, "sapi_available", staticmethod(lambda: True))
    monkeypatch.setattr(tts_mod.shutil, "which", lambda name: "powershell")
    capture = {}
    monkeypatch.setattr(tts_mod.subprocess, "run", _fake_ps(capture=capture))
    text = "Первая строка\nвторая строка — «кавычки» и 'апостроф'"
    asyncio.run(engine.synthesize_sapi(text))
    assert capture["text"] == text
    assert capture["script"].count("Speak") == 1


def test_synthesize_sapi_uses_cache(engine, monkeypatch):
    monkeypatch.setattr(engine, "sapi_available", staticmethod(lambda: True))
    monkeypatch.setattr(tts_mod.shutil, "which", lambda name: "powershell")
    calls = []
    monkeypatch.setattr(tts_mod.subprocess, "run", _fake_ps(calls=calls))

    first = asyncio.run(engine.synthesize_sapi("Привет"))
    second = asyncio.run(engine.synthesize_sapi("Привет"))
    assert first == second
    assert len(calls) == 1  # второй раз — из кэша


def test_synthesize_sapi_cleans_temp_files(engine, monkeypatch):
    monkeypatch.setattr(engine, "sapi_available", staticmethod(lambda: True))
    monkeypatch.setattr(tts_mod.shutil, "which", lambda name: "powershell")
    monkeypatch.setattr(tts_mod.subprocess, "run", _fake_ps())
    asyncio.run(engine.synthesize_sapi("Привет"))
    leftovers = list(tts_mod.TTS_CACHE_DIR.glob("*.part.*"))
    assert leftovers == []


def test_synthesize_sapi_powershell_failure(engine, monkeypatch):
    monkeypatch.setattr(engine, "sapi_available", staticmethod(lambda: True))
    monkeypatch.setattr(tts_mod.shutil, "which", lambda name: "powershell")
    monkeypatch.setattr(tts_mod.subprocess, "run",
                        _fake_ps(returncode=1, stderr="нет такого голоса"))
    with pytest.raises(tts_mod.TTSError) as exc:
        asyncio.run(engine.synthesize_sapi("Привет"))
    assert "офлайн" in str(exc.value).lower()
    assert "нет такого голоса" in str(exc.value)


def test_synthesize_sapi_requires_windows_bit(engine, monkeypatch):
    monkeypatch.setattr(tts_mod.sys, "platform", "linux")
    monkeypatch.setattr(tts_mod.shutil, "which", lambda name: None)
    with pytest.raises(tts_mod.TTSError):
        asyncio.run(engine.synthesize_sapi("Привет"))


# ======================================================================
#  Мелкие утилиты и статус
# ======================================================================
@pytest.mark.parametrize("value, expected", [("+8%", 8), ("-10%", -10), ("0%", 0), ("", 0), (None, 0), ("abc", 0)])
def test_parse_percent(engine, value, expected):
    assert engine._parse_percent(value) == expected


def test_status_shape(engine, monkeypatch):
    monkeypatch.setattr(engine, "available_engines", lambda: _engines(True, True))
    engine.last_engine = "sapi"
    status = engine.status()
    assert status["engines"] == {"edge-tts": True, "sapi": True, "pyttsx3": False}
    assert status["any_available"] is True
    assert status["offline_capable"] is True
    assert status["last_engine"] == "sapi"
    assert status["speaking"] is False


def test_status_offline_capable_false(engine, monkeypatch):
    monkeypatch.setattr(engine, "available_engines", lambda: _engines(True, False, False))
    assert engine.status()["offline_capable"] is False


def test_config_defaults_use_auto_engine():
    from astra import config as cfg_mod

    cfg = cfg_mod.DEFAULTS if hasattr(cfg_mod, "DEFAULTS") else cfg_mod.DEFAULT_CONFIG
    voice = cfg["voice"]
    assert voice["tts_engine"] == "auto"
    assert voice["sapi_voice"] == ""
