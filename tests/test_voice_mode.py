"""Голосовой режим и режимы запуска.

Проверяем то, о чём просил пользователь:
* при запуске есть выбор «обычный / голосовой»;
* в голосовом режиме блок статусов заполняется человеческими фразами (их даёт ИИ,
  а если забыл — собираем сами из инструмента и аргументов);
* распознавание речи по умолчанию — Whisper large-v3, и модель можно переключить;
* микрофон слушает постоянно и выключается на время ответа и действий.
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

from astra import status as status_mod  # noqa: E402
from astra.config import DEFAULTS, Config  # noqa: E402
from astra.llm import parse_decision  # noqa: E402
from astra.speech.stt import available_models, model_label  # noqa: E402


# ======================================================================
#  Человеческие статусы действий
# ======================================================================
def test_describe_open_app_uses_russian_name():
    assert status_mod.describe_action("open_app", {"app": "notepad"}) == "Открываю блокнот"
    assert status_mod.describe_action("open_app", {"app": "chrome"}) == "Открываю Chrome"
    assert "блокнот" in status_mod.describe_action("open_app", {"path": r"C:\Windows\notepad.exe"})


def test_describe_frequent_actions():
    assert status_mod.describe_action("scroll_in_window", {"amount": -3}) == "Листаю"
    assert status_mod.describe_action("scroll_until_end", {}) == "Листаю"
    assert "текст" in status_mod.describe_action("type_text", {"text": "привет"})
    assert status_mod.describe_action("see_window", {"title": "Chrome"}) == "Разглядываю окно и элементы"
    assert status_mod.describe_action("open_url", {"url": "https://www.youtube.com/watch?v=1"}).endswith("youtube.com")


def test_describe_hotkey_and_unknown_tool():
    assert status_mod.describe_action("hotkey", {"keys": ["ctrl", "s"]}) == "Нажимаю Ctrl + S"
    assert status_mod.describe_action("press_key", {"key": "enter"}) == "Нажимаю Enter"
    assert "some_future_tool" in status_mod.describe_action("some_future_tool", {})


def test_clean_status_strips_noise():
    assert status_mod.clean_status("  «открываю блокнот».  ") == "Открываю блокнот"
    assert status_mod.clean_status('{"tool": "open_app"}') == ""
    assert status_mod.clean_status("") == ""
    long_text = "а" * 200
    assert len(status_mod.clean_status(long_text)) <= status_mod.MAX_STATUS_LEN


def test_status_for_prefers_model_hint():
    assert status_mod.status_for("scroll_in_window", {}, "Читаю отзывы") == "Читаю отзывы"
    assert status_mod.status_for("scroll_in_window", {}, "   ") == "Листаю"


# ======================================================================
#  Разбор плана: статус не должен попасть в аргументы инструмента
# ======================================================================
def test_parse_decision_extracts_status():
    raw = ('{"thought": "надо открыть", "actions": ['
           '{"tool": "open_app", "args": {"app": "notepad"}, "status": "Открываю блокнот"}]}')
    decision = parse_decision(raw)
    action = decision["actions"][0]
    assert action["status"] == "Открываю блокнот"
    assert action["args"] == {"app": "notepad"}


def test_parse_decision_status_without_args_does_not_leak():
    decision = parse_decision('{"actions": [{"tool": "scroll_in_window", "status": "Листаю отзывы"}]}')
    action = decision["actions"][0]
    assert action["args"] == {}
    assert action["status"] == "Листаю отзывы"


# ======================================================================
#  Настройки: режим запуска и большая модель распознавания
# ======================================================================
def test_defaults_have_mode_and_large_whisper():
    assert DEFAULTS["voice"]["whisper_model"] == "large-v3"
    assert DEFAULTS["voice"]["continuous_listen"] is True
    assert DEFAULTS["ui"]["ask_mode"] is True
    assert DEFAULTS["ui"]["mode"] == ""
    assert DEFAULTS["ui"]["voice_square_window"] is True


def test_stt_models_include_large_and_are_sorted_by_quality():
    models = available_models()
    ids = [m["id"] for m in models]
    assert ids[0] == "large-v3"
    assert models[0]["recommended"] is True
    assert "large-v3-turbo" in ids and "small" in ids
    assert "максимальная" in model_label("large-v3")


def test_migration_upgrades_old_small_model(tmp_path):
    cfg = Config(path=tmp_path / "config.json")
    cfg.patch({"voice": {"whisper_model": "small"}}, save=True)
    # имитируем конфиг, сохранённый прошлой версией (без флага миграции)
    import json

    raw = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    raw["voice"].pop("stt_model_upgraded", None)
    (tmp_path / "config.json").write_text(json.dumps(raw), encoding="utf-8")

    reloaded = Config(path=tmp_path / "config.json")
    assert reloaded.get("voice.whisper_model") == "large-v3"
    assert reloaded.get("voice.stt_model_upgraded") is True


def test_migration_keeps_explicit_choice(tmp_path):
    cfg = Config(path=tmp_path / "config.json")
    cfg.patch({"voice": {"whisper_model": "medium"}}, save=True)
    import json

    raw = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    raw["voice"].pop("stt_model_upgraded", None)
    (tmp_path / "config.json").write_text(json.dumps(raw), encoding="utf-8")

    reloaded = Config(path=tmp_path / "config.json")
    assert reloaded.get("voice.whisper_model") == "medium"


# ======================================================================
#  Агент присылает статусы (их показывает блок под сферой)
# ======================================================================
@pytest.mark.asyncio
async def test_agent_emits_status_events(monkeypatch):
    from astra import agent as agent_module
    from astra.config import config

    monkeypatch.setattr(config, "get", lambda key, default=None: (
        "fake-model" if key == "lmstudio.model"
        else 3 if key == "agent.max_steps"
        else True if key == "agent.stream_thoughts"
        else default))

    answers = [
        '{"thought": "жду", "actions": [{"tool": "wait", "args": {"seconds": 0.01}, '
        '"status": "Жду загрузки страницы"}]}',
        '{"thought": "готово", "action": "final"}',
    ]

    async def fake_chat(messages, **kwargs):
        return {"content": answers[min(len(calls), len(answers) - 1)] if calls else answers[0]}

    calls = []

    async def chat(messages, **kwargs):
        calls.append(1)
        return {"content": answers[0] if len(calls) == 1 else answers[1]}

    async def chat_stream(messages, **kwargs):
        for piece in ("Готово", "."):
            yield piece

    monkeypatch.setattr(agent_module.llm, "chat", chat)
    monkeypatch.setattr(agent_module.llm, "chat_stream", chat_stream)

    events = []

    async def emit(event):
        events.append(event)

    ag = agent_module.Agent(emit=emit)
    result = await ag.run("открой и подожди", history=[], modality="voice")
    assert result.text.startswith("Готово")

    statuses = [e["text"] for e in events if e.get("type") == "status"]
    assert "Думаю, как выполнить задачу" in statuses
    assert "Жду загрузки страницы" in statuses          # фраза от модели
    assert statuses[-1] == "Готово ответ" or statuses[-1] == "Готовлю ответ"
    # статус есть и в карточке действия
    starts = [e for e in events if e.get("type") == "action_start"]
    assert starts and starts[0].get("status") == "Жду загрузки страницы"


@pytest.mark.asyncio
async def test_agent_status_falls_back_to_tool_description(monkeypatch):
    from astra import agent as agent_module
    from astra.config import config

    monkeypatch.setattr(config, "get", lambda key, default=None: (
        "fake-model" if key == "lmstudio.model"
        else 2 if key == "agent.max_steps"
        else True if key == "agent.stream_thoughts"
        else default))

    async def chat(messages, **kwargs):
        return {"content": '{"actions": [{"tool": "wait", "args": {"seconds": 0.01}}]}'}

    async def chat_stream(messages, **kwargs):
        yield "ок"

    monkeypatch.setattr(agent_module.llm, "chat", chat)
    monkeypatch.setattr(agent_module.llm, "chat_stream", chat_stream)

    events = []

    async def emit(event):
        events.append(event)

    ag = agent_module.Agent(emit=emit)
    await ag.run("подожди", history=[], modality="text")
    statuses = [e["text"] for e in events if e.get("type") == "status"]
    assert "Жду" in statuses      # описание из status.describe_action


# ======================================================================
#  Серверные ручки режима и моделей распознавания
# ======================================================================
@pytest.fixture()
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from astra.server import app as app_mod

    monkeypatch.setenv("ASTRA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_mod.config, "patch", lambda values, save=True: app_mod.config.as_dict())
    return TestClient(app_mod.app), app_mod


def test_stt_models_endpoint(client):
    http, app_mod = client
    resp = http.get("/api/stt/models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["models"][0]["id"] == "large-v3"
    assert "current" in data


def test_set_ui_mode_saves_mode_and_requests_square_window(client, monkeypatch):
    http, app_mod = client
    saved = {}

    def fake_patch(values, save=True):
        saved.update(values.get("ui") or {})
        return app_mod.config.as_dict()

    monkeypatch.setattr(app_mod.config, "patch", fake_patch)
    monkeypatch.setattr(app_mod.config, "get", lambda key, default=None: (
        760 if key == "ui.voice_window_size" else True if key == "ui.voice_square_window" else default))

    resp = http.post("/api/ui/mode", json={"mode": "voice", "remember": True, "ask": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "voice"
    assert data["window"]["width"] == data["window"]["height"] == 760   # квадратное окно
    assert saved["mode"] == "voice"
    assert saved["ask_mode"] is False

    pending = http.get("/api/window/next").json()
    assert pending["request"]["mode"] == "voice"


def test_set_ui_mode_ask_keeps_empty_mode(client, monkeypatch):
    http, app_mod = client
    saved = {}

    def fake_patch(values, save=True):
        saved.update(values.get("ui") or {})
        return app_mod.config.as_dict()

    monkeypatch.setattr(app_mod.config, "patch", fake_patch)

    resp = http.post("/api/ui/mode", json={"mode": "text", "remember": False, "ask": True})
    assert resp.status_code == 200
    assert saved["mode"] == ""
    assert saved["ask_mode"] is True
    assert saved["last_mode"] == "text"


def test_set_ui_mode_rejects_unknown(client):
    http, _ = client
    assert http.post("/api/ui/mode", json={"mode": "telepathy"}).status_code == 400


@pytest.mark.asyncio
async def test_voice_mode_listens_again_after_answer(monkeypatch):
    """Голосовой режим: после ответа микрофон включается снова сам."""
    from astra.server import app as app_mod

    started = []

    async def fake_start(conn, force=False):
        started.append((conn, force))

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(app_mod, "start_recording", fake_start)
    monkeypatch.setattr(app_mod.asyncio, "sleep", fast_sleep)

    class Conn:
        voice_mode = True
        recorder = None
        busy = False
        voice_mode_on = True

    conn = Conn()
    await app_mod.resume_listening(conn)
    # force=True обязателен: агент в этот момент всё ещё считается занятым
    assert started == [(conn, True)]

    conn.voice_mode = False
    started.clear()
    await app_mod.resume_listening(conn)
    assert started == []      # в обычном чате микрофон не включаем


# ======================================================================
#  CUDA: «Library cublas64_12.dll is not found» — не падать, считать на CPU
# ======================================================================
CUBLAS_ERROR = "Library cublas64_12.dll is not found or cannot be loaded"


def _fake_modules(monkeypatch, *, cuda_count=1, cuda_ok=True, fail_on_load=False, fail_on_transcribe=False):
    """Подставить ctranslate2 и faster-whisper с нужным поведением."""
    import types

    ctranslate2 = types.ModuleType("ctranslate2")

    def get_cuda_device_count():
        return cuda_count

    def get_supported_compute_types(device):
        if not cuda_ok:
            raise RuntimeError(CUBLAS_ERROR)
        return {"float16", "int8"}

    ctranslate2.get_cuda_device_count = get_cuda_device_count
    ctranslate2.get_supported_compute_types = get_supported_compute_types
    monkeypatch.setitem(__import__("sys").modules, "ctranslate2", ctranslate2)

    faster_whisper = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, model_size_or_path, device="cpu", compute_type="int8", **kwargs):
            self.device = device
            self.compute_type = compute_type
            self.calls = 0
            if device == "cuda" and fail_on_load:
                raise RuntimeError(CUBLAS_ERROR)

        def transcribe(self, audio, **kwargs):
            self.calls += 1
            if self.device == "cuda" and fail_on_transcribe:
                raise RuntimeError(CUBLAS_ERROR)

            class Segment:
                text = "открой блокнот"

            class Info:
                language = "ru"

            return [Segment()], Info()

    faster_whisper.WhisperModel = WhisperModel
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", faster_whisper)
    return WhisperModel


def _fresh_stt():
    from astra.speech import stt as stt_mod

    stt_mod.reset_cuda_state()
    engine = stt_mod.WhisperSTT()
    return stt_mod, engine


def _audio():
    import numpy as np

    return (np.random.randn(16000) * 0.05).astype("float32")


def test_is_cuda_library_error_detects_cublas_message():
    from astra.speech.stt import is_cuda_library_error

    assert is_cuda_library_error(CUBLAS_ERROR) is True
    assert is_cuda_library_error(RuntimeError("Could not load library libcudnn_ops.so.9")) is True
    assert is_cuda_library_error(RuntimeError("CUDA driver version is insufficient")) is True
    assert is_cuda_library_error(RuntimeError("Модель не найдена в кэше")) is False
    assert is_cuda_library_error("") is False


def test_cuda_usable_reports_missing_libraries(monkeypatch):
    stt_mod, _ = _fresh_stt()
    _fake_modules(monkeypatch, cuda_count=1, cuda_ok=False)
    usable, error = stt_mod.cuda_usable()
    assert usable is False
    assert "cublas64_12" in error
    # второй вызов не перепроверяет (кэш), но состояние то же
    assert stt_mod.cuda_usable()[0] is False
    stt_mod.reset_cuda_state()
    assert stt_mod.cuda_state() == {"usable": None, "error": ""}


def test_load_falls_back_to_cpu_when_cuda_libraries_missing(monkeypatch):
    """auto + есть видеокарта, но нет cublas → грузимся на процессоре, не падаем."""
    stt_mod, engine = _fresh_stt()
    _fake_modules(monkeypatch, cuda_count=1, cuda_ok=False)

    engine.load("large-v3", "auto", "auto")
    assert engine.ready is True
    assert engine.device == "cpu"
    assert engine.compute_type == "int8"
    assert "процессор" in engine.fallback_note
    status = engine.status()
    assert status["on_cpu"] is True
    assert "cublas64_12" in status["cuda"]["error"]


def test_load_falls_back_when_cuda_breaks_on_load(monkeypatch):
    """CUDA «есть», но падает при создании модели — вторая попытка на CPU."""
    stt_mod, engine = _fresh_stt()
    _fake_modules(monkeypatch, cuda_count=1, cuda_ok=True, fail_on_load=True)

    engine.load("medium", "cuda", "auto")
    assert engine.ready is True
    assert engine.device == "cpu"
    assert stt_mod.cuda_state()["usable"] is False


def test_transcribe_retries_on_cpu_when_cublas_missing_late(monkeypatch):
    """ctranslate2 грузит cublas лениво: ошибка всплывает на первой фразе."""
    stt_mod, engine = _fresh_stt()
    _fake_modules(monkeypatch, cuda_count=1, cuda_ok=True, fail_on_transcribe=True)

    engine.load("large-v3", "auto", "auto")
    assert engine.device == "cuda"          # загрузка прошла
    text = engine.transcribe(_audio(), "ru", "large-v3", "auto", "auto")
    assert text == "открой блокнот"          # расшифровка всё равно получилась
    assert engine.device == "cpu"            # и теперь считаем на процессоре
    assert stt_mod.cuda_state()["usable"] is False
    # повторная фраза уже не тратит время на неудачную попытку
    assert engine.transcribe(_audio(), "ru", "large-v3", "auto", "auto") == "открой блокнот"


def test_stt_error_hint_explains_cublas():
    from astra.server.app import stt_error_hint

    message = stt_error_hint(RuntimeError(CUBLAS_ERROR))
    assert "процессор" in message
    assert "nvidia-cublas-cu12" in message
    assert "Не удалось распознать речь" not in message


def test_stt_models_reports_device_and_cuda(client, monkeypatch):
    http, app_mod = client
    monkeypatch.setattr(app_mod.whisper_stt, "device", "cpu")
    monkeypatch.setattr(app_mod.whisper_stt, "compute_type", "int8")
    monkeypatch.setattr(app_mod.whisper_stt, "fallback_note", "нет библиотек CUDA; считаю на процессоре")
    monkeypatch.setattr(app_mod, "cuda_state", lambda: {"usable": False, "error": CUBLAS_ERROR})

    data = http.get("/api/stt/models").json()
    assert data["device"] == "cpu"
    assert data["on_cpu"] is True
    assert data["cuda"]["usable"] is False
    assert "процессор" in data["fallback_note"]
    assert data["device_setting"] in ("auto", "cpu", "cuda")


# ======================================================================
#  Автоподключение библиотек CUDA 12 (cuBLAS из pip) — CUDA 13 не мешает
# ======================================================================
def test_find_cuda_dll_dirs_sees_pip_nvidia_packages(tmp_path, monkeypatch):
    """Пакеты nvidia-cublas-cu12 кладут DLL в site-packages/nvidia/cublas/bin."""
    from astra.speech import stt as stt_mod

    bin_dir = tmp_path / "nvidia" / "cublas" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "cublas64_12.dll").write_bytes(b"dll")
    cudnn_dir = tmp_path / "nvidia" / "cudnn" / "bin"
    cudnn_dir.mkdir(parents=True)
    (cudnn_dir / "cudnn64_9.dll").write_bytes(b"dll")

    monkeypatch.setattr(stt_mod, "_nvidia_roots", lambda: [str(tmp_path)])
    dirs = stt_mod.find_cuda_dll_dirs(refresh=True)
    assert str(bin_dir) in dirs
    assert str(cudnn_dir) in dirs


def test_prepare_cuda_dlls_finds_cublas_and_adds_to_path(tmp_path, monkeypatch):
    from astra.speech import stt as stt_mod

    bin_dir = tmp_path / "nvidia" / "cublas" / "bin"
    bin_dir.mkdir(parents=True)
    lib_name = "cublas64_12.dll" if os.name == "nt" else "libcublas.so.12"
    (bin_dir / lib_name).write_bytes(b"lib")

    monkeypatch.setattr(stt_mod, "_nvidia_roots", lambda: [str(tmp_path)])
    monkeypatch.setattr(stt_mod, "_CUDA_HANDLES", [])
    monkeypatch.setenv("PATH", "/usr/bin")
    result = stt_mod.prepare_cuda_dlls(refresh=True)

    assert result["cublas12"] is True
    assert str(bin_dir) in os.environ["PATH"].split(os.pathsep)   # ctranslate2 ищет по PATH


def test_cuda_explanation_mentions_cuda13_conflict():
    from astra.speech import stt as stt_mod

    stt_mod._CUDA_STATE.update(cublas13=r"C:\Program Files\NVIDIA\bin\cublas64_13.dll", cublas12=False)
    message = stt_mod.cuda_explanation(CUBLAS_ERROR)
    assert "CUDA 13" in message
    assert "nvidia-cublas-cu12" in message
    # обычная ошибка не должна превращаться в лекцию про CUDA
    assert stt_mod.cuda_explanation("модель не найдена") == "модель не найдена"


def test_cuda_endpoints_report_state(client, monkeypatch):
    http, app_mod = client
    monkeypatch.setattr(app_mod.stt_mod, "prepare_cuda_dlls",
                        lambda refresh=False: {"dirs": ["/x"], "cublas12": True, "loaded": ["cublas64_12.dll"],
                                               "cublas13": ""})
    monkeypatch.setattr(app_mod.stt_mod, "cuda_usable", lambda: (True, ""))

    data = http.get("/api/stt/cuda").json()
    assert data["usable"] is True
    assert data["cublas12"] is True
    assert "pip install" in data["install_command"]
    assert data["install_running"] is False


def test_cuda_endpoints_explain_cuda13(client, monkeypatch):
    http, app_mod = client
    monkeypatch.setattr(app_mod.stt_mod, "prepare_cuda_dlls",
                        lambda refresh=False: {"dirs": [], "cublas12": False, "loaded": [],
                                               "cublas13": r"C:\Program Files\NVIDIA\bin\cublas64_13.dll"})
    monkeypatch.setattr(app_mod.stt_mod, "cuda_usable", lambda: (False, CUBLAS_ERROR))

    data = http.get("/api/stt/cuda").json()
    assert data["usable"] is False
    assert data["cublas13"].endswith("cublas64_13.dll")
    assert "CUDA 13" in data["note"]
    assert "взаимозаменяемы" in data["note"] or "12" in data["note"]
