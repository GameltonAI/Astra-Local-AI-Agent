"""FastAPI-приложение: REST API + WebSocket для интерфейса."""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..agent import Agent
from ..config import config
from ..endpoints import endpoints
from ..llm import LLMError, llm
from ..vision import build_test_image, vision_state
from ..vision import looks_like_vision_error as vision_looks_broken
from ..paths import STATIC_DIR, ensure_dirs
from ..speech import (AudioUnavailable, STTError, Recorder, available_models, cuda_state,
                      is_cuda_library_error, list_devices, model_label, reset_cuda_state,
                      tts, whisper_stt)
from ..speech import stt as stt_mod
from ..storage import sessions as store
from ..storage.db import init_db
from ..tools import get_registry
from ..tools import os_control
from .connection import ClientConnection

# ======================================================================
#  Служебные функции
# ======================================================================
from ..textutils import clean_for_speech  # noqa: E402  (используется при озвучке)


def resolve_device(kind: str) -> Optional[int]:
    """Имя/индекс устройства из настроек → индекс для sounddevice."""
    raw = config.get(f"voice.{kind}_device")
    if raw in (None, "", "default"):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        try:
            devices = list_devices()
        except AudioUnavailable:
            return None
        needle = str(raw).lower()
        for dev in devices:
            if needle in dev["name"].lower():
                if kind == "input" and dev["channels_in"] > 0:
                    return dev["index"]
                if kind == "output" and dev["channels_out"] > 0:
                    return dev["index"]
    return None


# ======================================================================
#  Реестр подключений (для широковещательных событий)
# ======================================================================
CONNECTIONS: set["ClientConnection"] = set()


def broadcast(event: Dict[str, Any]) -> None:
    for conn in list(CONNECTIONS):
        try:
            conn.emit(event)
        except Exception:
            CONNECTIONS.discard(conn)


def endpoint_public(result: Dict[str, Any], with_probes: bool = False) -> Dict[str, Any]:
    """Короткое описание активного сервера для интерфейса."""
    data = {
        "ok": bool(result.get("ok")),
        "url": result.get("url") or "",
        "kind": result.get("kind") or "unknown",
        "kind_label": result.get("kind_label") or "",
        "model": config.get("lmstudio.model") or "",
        "models_count": len(result.get("models") or []),
        "candidates": result.get("candidates") or [],
        "auto_detect": bool(result.get("auto_detect", True)),
        "error": result.get("error") or "",
        "checked_at": result.get("checked_at") or 0,
        "switched_from": result.get("switched_from"),
    }
    if with_probes:
        data["probes"] = [
            {
                "url": probe.get("url") or "",
                "ok": bool(probe.get("ok")),
                "kind": probe.get("kind") or "unknown",
                "latency_ms": probe.get("latency_ms") or 0,
                "error": probe.get("error") or "",
                "models_count": len(probe.get("models") or []),
            }
            for probe in result.get("probes") or []
        ]
    return data


# ======================================================================
#  Приложение
# ======================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    init_db()
    get_registry()

    # определяем, какой сервер LM Studio доступен (сначала локальная сеть)
    resolved = await endpoints.resolve(force=True)
    if resolved["ok"]:
        print(f"[startup] LM Studio: {resolved['url']} ({resolved['kind_label']}), "
              f"моделей: {len(resolved['models'])}, выбрана: {config.get('lmstudio.model')}")
    else:
        print(f"[startup] {resolved['error']}")
        print("[startup] приложение запустится, но запросы к модели будут возвращать ошибку "
              "до появления доступного сервера")

    # мгновенно уведомляем интерфейс, если сервер переключился во время запроса
    endpoints.set_listener(
        lambda snapshot: broadcast({"type": "endpoint", **endpoint_public(snapshot, with_probes=True)})
    )
    monitor_task = asyncio.create_task(endpoint_monitor(resolved))

    # прогреваем whisper в фоне, чтобы первый голосовой ввод не ждал
    async def warmup() -> None:
        try:
            await asyncio.to_thread(
                whisper_stt.load,
                config.get("voice.whisper_model", "large-v3"),
                config.get("voice.whisper_device", "auto"),
                config.get("voice.whisper_compute_type", "auto"),
            )
            print("[startup] модель распознавания речи готова")
        except Exception as exc:  # noqa: BLE001
            print(f"[startup] распознавание речи недоступно: {exc}")

    asyncio.create_task(warmup())
    yield
    monitor_task.cancel()
    await llm.close()


async def endpoint_monitor(initial: Dict[str, Any]) -> None:
    """Следит за доступностью сервера LM Studio и переключает резерв при обрыве."""
    last_signature = (initial.get("url"), bool(initial.get("ok")))
    try:
        while True:
            await asyncio.sleep(20)
            try:
                result = await endpoints.resolve(force=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[endpoints] ошибка проверки: {exc}")
                continue
            signature = (result.get("url"), bool(result.get("ok")))
            if signature == last_signature:
                continue
            last_signature = signature
            public = endpoint_public(result, with_probes=True)
            print(f"[endpoints] активный сервер: {public['url'] or 'нет'} ({public['error'] or 'ок'})")
            broadcast({"type": "endpoint", **public})
    except asyncio.CancelledError:
        return


app = FastAPI(title="Astra", version=__version__, lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> Any:
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Astra</h1><p>Интерфейс не найден (static/index.html).</p>", status_code=500)
    return FileResponse(index_file)


@app.get("/favicon.ico")
async def favicon() -> Any:
    icon = STATIC_DIR / "favicon.svg"
    if icon.exists():
        return FileResponse(icon, media_type="image/svg+xml")
    return JSONResponse({}, status_code=204)


# ======================================================================
#  REST API
# ======================================================================
class ConfigPatch(BaseModel):
    patch: Dict[str, Any]


class SessionCreate(BaseModel):
    title: Optional[str] = None


class SessionUpdate(BaseModel):
    title: Optional[str] = None
    pinned: Optional[bool] = None
    archived: Optional[bool] = None


class ModelsRequest(BaseModel):
    base_url: Optional[str] = None


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    stt_info = {"engine": config.get("voice.stt_engine"), "model": config.get("voice.whisper_model"),
                "label": model_label(config.get("voice.whisper_model") or ""),
                "ready": whisper_stt.ready, "loading": whisper_stt.loading,
                "loaded": whisper_stt.model_name,
                "device": whisper_stt.device or "",
                "compute_type": whisper_stt.compute_type or "",
                "cuda": cuda_state(),
                "fallback_note": whisper_stt.fallback_note or "",
                "error": whisper_stt.last_error or None}
    audio_ok, audio_error = True, None
    try:
        list_devices()
    except AudioUnavailable as exc:
        audio_ok, audio_error = False, str(exc)
    endpoint_info = endpoint_public(await endpoints.status(probe_all=True), with_probes=True)
    return {
        "ok": True,
        "version": __version__,
        "platform": os_control.system_info().get("os"),
        "endpoint": endpoint_info,
        "model": config.get("lmstudio.model"),
        "stt": stt_info,
        "audio": {"ok": audio_ok, "error": audio_error},
        "tts": {
            "engine": config.get("voice.tts_engine"),
            "voice": config.get("voice.tts_voice"),
            "enabled": bool(config.get("voice.tts_enabled")),
            "always": bool(config.get("voice.tts_always")),
            **tts.status(),
        },
        "vision": {
            **vision_state.status(),
            "auto_window_screenshot": bool(config.get("agent.auto_window_screenshot", True)),
            "max_images": config.get("lmstudio.vision_max_images"),
            "max_px": config.get("lmstudio.vision_max_px"),
        },
        "stats": store.stats(),
    }


@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    return {"config": config.public()}


@app.post("/api/config")
async def set_config(patch: ConfigPatch) -> Dict[str, Any]:
    updated = config.patch(patch.patch)
    get_registry()
    if isinstance(patch.patch.get("lmstudio"), dict):
        # адреса/автовыбор изменились — перепроверим серверы
        endpoints.invalidate()
        asyncio.create_task(_recheck_endpoints())
    updated.get("lmstudio", {}).pop("api_key", None)
    return {"config": updated, "ok": True}


async def _recheck_endpoints() -> None:
    try:
        result = await endpoints.resolve(force=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[endpoints] {exc}")
        return
    broadcast({"type": "endpoint", **endpoint_public(result, with_probes=True)})


@app.get("/api/models")
async def get_models(base_url: Optional[str] = None) -> Dict[str, Any]:
    info = endpoint_public(await endpoints.status())
    try:
        models = await llm.list_models(base_url)
    except LLMError as exc:
        return {"ok": False, "error": str(exc), "models": [], "current": config.get("lmstudio.model"),
                "endpoint": info}
    if base_url:
        info = {**info, "url": base_url}
    return {"ok": True, "models": models, "current": config.get("lmstudio.model"),
            "base_url": info.get("url"), "endpoint": info}


@app.post("/api/models")
async def post_models(req: ModelsRequest) -> Dict[str, Any]:
    try:
        models = await llm.list_models(req.base_url)
    except LLMError as exc:
        return {"ok": False, "error": str(exc), "models": []}
    return {"ok": True, "models": models}


@app.get("/api/endpoints")
async def get_endpoints(check: bool = False) -> Dict[str, Any]:
    """Список серверов LM Studio, их доступность и активный адрес."""
    result = await endpoints.resolve(force=check, probe_all=check)
    return {"endpoint": endpoint_public(result, with_probes=True)}


class EndpointSelect(BaseModel):
    url: str
    pin: bool = False


@app.post("/api/endpoints/select")
async def select_endpoint(req: EndpointSelect) -> Dict[str, Any]:
    """Проверить конкретный адрес; pin=True — закрепить его и выключить автовыбор."""
    probe = await endpoints.probe(req.url)
    if not probe["ok"]:
        return {"ok": False, "error": f"{probe['url']}: {probe['error']}",
                "probe": {"url": probe["url"], "error": probe["error"]}}
    patch: Dict[str, Any] = {"lmstudio": {"base_url": probe["url"]}}
    if req.pin:
        patch["lmstudio"]["auto_detect"] = False
    candidates = list(config.get("lmstudio.candidates") or [])
    if probe["url"] not in [c.rstrip("/") for c in candidates]:
        candidates.insert(0, probe["url"])
        patch["lmstudio"]["candidates"] = candidates
    config.patch(patch)
    endpoints.invalidate()
    result = await endpoints.resolve(force=True)
    public = endpoint_public(result, with_probes=True)
    broadcast({"type": "endpoint", **public})
    return {"ok": True, "endpoint": public}


@app.get("/api/tools")
async def get_tools() -> Dict[str, Any]:
    registry = get_registry()
    return {
        "tools": registry.public_catalog(),
        "allowed_without_confirmation": config.get("safety.allowed_tools") or [],
        "safety": config.get("safety"),
    }


class ToolPermission(BaseModel):
    name: str
    allowed: bool


@app.post("/api/tools/permission")
async def set_tool_permission(req: ToolPermission) -> Dict[str, Any]:
    if get_registry().get(req.name) is None:
        raise HTTPException(404, f"Инструмент {req.name} не найден")
    allowed = set(config.get("safety.allowed_tools") or [])
    if req.allowed:
        allowed.add(req.name)
    else:
        allowed.discard(req.name)
    config.set("safety.allowed_tools", sorted(allowed))
    return {"ok": True, "allowed_without_confirmation": sorted(allowed)}


@app.get("/api/sessions")
async def get_sessions(q: str = "", archived: bool = False) -> Dict[str, Any]:
    return {"sessions": store.list_sessions(include_archived=archived, query=q)}


@app.post("/api/sessions")
async def create_session(req: SessionCreate) -> Dict[str, Any]:
    session = store.create_session(title=req.title or "Новый чат", model=config.get("lmstudio.model") or "")
    return {"session": session}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> Dict[str, Any]:
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, "Сессия не найдена")
    return {"session": session, "messages": store.list_messages(session_id, include_hidden=False)}


@app.patch("/api/sessions/{session_id}")
async def patch_session(session_id: str, req: SessionUpdate) -> Dict[str, Any]:
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    session = store.update_session(session_id, **fields)
    if not session:
        raise HTTPException(404, "Сессия не найдена")
    return {"session": session}


@app.delete("/api/sessions/{session_id}")
async def remove_session(session_id: str) -> Dict[str, Any]:
    store.delete_session(session_id)
    return {"ok": True}


@app.delete("/api/sessions/{session_id}/messages/{message_id}")
async def remove_message(session_id: str, message_id: int) -> Dict[str, Any]:
    store.delete_message(message_id)
    return {"ok": True}


@app.get("/api/sessions/{session_id}/export", response_class=PlainTextResponse)
async def export_session(session_id: str) -> Any:
    markdown = store.export_session_markdown(session_id)
    if not markdown:
        raise HTTPException(404, "Сессия не найдена")
    session = store.get_session(session_id) or {}
    title = (session.get("title") or "chat").strip() or "chat"
    ascii_name = re.sub(r"[^\w\-]+", "_", title, flags=re.ASCII).strip("_")[:40] or "chat"
    # RFC 5987: кириллицу передаём в filename*, ASCII-вариант — в filename
    utf8_name = urllib.parse.quote(f"{title[:40]}.md")
    disposition = f'attachment; filename="{ascii_name}.md"; filename*=UTF-8\'\'{utf8_name}'
    return PlainTextResponse(markdown, headers={"Content-Disposition": disposition})


@app.get("/api/tts/status")
async def tts_status() -> Dict[str, Any]:
    status = tts.status()
    audio_ok, audio_error = True, None
    try:
        list_devices()
    except AudioUnavailable as exc:
        audio_ok, audio_error = False, str(exc)
    return {
        **status,
        "audio_output_ok": audio_ok,
        "audio_error": audio_error,
        "engine_setting": config.get("voice.tts_engine"),
        "voice": config.get("voice.tts_voice"),
        "sapi_voice": config.get("voice.sapi_voice"),
        "enabled": bool(config.get("voice.tts_enabled")),
        "always": bool(config.get("voice.tts_always")),
    }


@app.post("/api/tts/test")
async def tts_test() -> Dict[str, Any]:
    """Синтезировать и проиграть тестовую фразу, вернуть движок и ошибку."""
    phrase = "Привет! Проверка голоса Астра. Раз, два, три."
    result = await tts.speak(
        phrase,
        voice=config.get("voice.tts_voice") or "ru-RU-DmitryNeural",
        rate=config.get("voice.tts_rate") or "+0%",
        volume=config.get("voice.tts_volume") or "+0%",
        device=resolve_device("output"),
        engine=config.get("voice.tts_engine") or "auto",
        sapi_voice=config.get("voice.sapi_voice") or "",
    )
    if result.get("ok"):
        return {"ok": True, "engine": result.get("engine"), "label": result.get("engine_label"),
                "fallback_from": result.get("fallback_from"), "status": tts.status()}
    return {"ok": False, "error": result.get("error"), "hint": tts_hint(str(result.get("error") or "")),
            "status": tts.status()}


@app.post("/api/vision/test")
async def vision_test(force: bool = True) -> Dict[str, Any]:
    """Проверить, видит ли модель изображения: отправляем тестовую картинку."""
    if not config.get("lmstudio.model"):
        return {"ok": False, "error": "Модель не выбрана"}
    image = build_test_image()
    if image is None:
        return {"ok": False, "error": "Не установлен Pillow (pip install Pillow)"}
    if force:
        vision_state.reset()
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Что на этой картинке? Ответь одним коротким предложением."},
            image,
        ],
    }]
    try:
        reply = await llm.chat(messages, temperature=0.1, max_tokens=120)
    except LLMError as exc:
        error = str(exc)
        if vision_looks_broken(error):
            vision_state.mark_unsupported(error)
            return {"ok": False, "supported": False, "error": error,
                    "hint": "Модель не принимает изображения. Загрузите vision-модель "
                            "(Qwen2.5-VL, Gemma 3, LLaVA, MiniCPM-V) или оставьте режим «auto»."}
        return {"ok": False, "supported": None, "error": error}
    answer = (reply.get("content") or "").strip()
    vision_state.mark_sent()
    return {"ok": True, "supported": True, "answer": answer, "model": reply.get("model"),
            "endpoint": reply.get("endpoint"), "status": vision_state.status()}


@app.get("/api/voices")
async def get_voices(locale: str = "") -> Dict[str, Any]:
    voices = await tts.list_voices(locale)
    return {"voices": voices, "current": config.get("voice.tts_voice")}


@app.get("/api/audio/devices")
async def get_audio_devices() -> Dict[str, Any]:
    try:
        devices = list_devices()
    except AudioUnavailable as exc:
        return {"ok": False, "error": str(exc), "input": [], "output": []}
    return {
        "ok": True,
        "input": [d for d in devices if d["channels_in"] > 0],
        "output": [d for d in devices if d["channels_out"] > 0],
    }


@app.get("/api/stt/models")
async def get_stt_models() -> Dict[str, Any]:
    """Модели распознавания речи: что можно выбрать и что загружено сейчас."""
    current = config.get("voice.whisper_model") or "large-v3"
    return {
        "ok": True,
        "models": available_models(),
        "current": current,
        "current_label": model_label(current),
        "loaded": whisper_stt.model_name,
        "ready": whisper_stt.ready,
        "loading": whisper_stt.loading,
        "error": whisper_stt.last_error or "",
        # и настройка, и что реально используется сейчас
        "device_setting": config.get("voice.whisper_device") or "auto",
        "device": whisper_stt.device or "",
        "compute_type": whisper_stt.compute_type or "",
        "on_cpu": bool(whisper_stt.device and whisper_stt.device != "cuda"),
        "fallback_note": whisper_stt.fallback_note or "",
        "cuda": cuda_state(),
        "compute_type_setting": config.get("voice.whisper_compute_type") or "auto",
    }


class SttModelRequest(BaseModel):
    model: str
    reload: bool = True


@app.post("/api/stt/model")
async def set_stt_model(req: SttModelRequest) -> Dict[str, Any]:
    """Сменить модель распознавания речи и (по умолчанию) сразу её загрузить."""
    model_id = (req.model or "").strip()
    known = {item["id"] for item in available_models()}
    if model_id not in known and not re.match(r"^[\w./-]{2,80}$", model_id):
        raise HTTPException(status_code=400, detail="Некорректное имя модели")
    if not model_id:
        raise HTTPException(status_code=400, detail="Не указана модель")
    config.patch({"voice": {"whisper_model": model_id}})

    if not req.reload:
        return {"ok": True, "model": model_id, "label": model_label(model_id), "loading": False}

    async def reload_model() -> None:
        # новая модель — заново проверяем CUDA (вдруг библиотеки уже установили)
        reset_cuda_state()
        whisper_stt.unload()
        broadcast({"type": "stt_model", "model": model_id, "label": model_label(model_id),
                   "ready": False, "loading": True})
        try:
            await asyncio.to_thread(
                whisper_stt.load,
                model_id,
                config.get("voice.whisper_device", "auto"),
                config.get("voice.whisper_compute_type", "auto"),
            )
            print(f"[stt] модель {model_id} загружена")
            broadcast({"type": "stt_model", "model": model_id, "label": model_label(model_id),
                       "ready": True, "loading": False, "error": ""})
        except STTError as exc:
            print(f"[stt] {exc}")
            broadcast({"type": "stt_model", "model": model_id, "label": model_label(model_id),
                       "ready": False, "loading": False, "error": str(exc)})
            broadcast({"type": "toast", "message": str(exc), "level": "warn"})

    asyncio.create_task(reload_model())
    return {"ok": True, "model": model_id, "label": model_label(model_id), "loading": True}


# --------------------------------------------------------------- CUDA ---
CUDA_INSTALL: Dict[str, Any] = {"running": False, "output": "", "ok": None, "started_at": 0.0}
CUDA_PACKAGES = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]


def cuda_report() -> Dict[str, Any]:
    """Что мешает считать на видеокарте и как это исправить."""
    info = stt_mod.prepare_cuda_dlls(refresh=True)
    usable, error = stt_mod.cuda_usable()
    note = ""
    if not usable:
        if info.get("cublas13") and not info.get("cublas12"):
            note = ("В системе найден cuBLAS от CUDA 13, а движку распознавания нужен cuBLAS 12 "
                    "(это разные библиотеки, они не взаимозаменяемы). Поставьте CUDA 12-библиотеки "
                    "рядом — CUDA 13 останется на месте.")
        elif not info.get("cublas12"):
            note = "Библиотеки CUDA 12 (cuBLAS/cuDNN) не установлены."
        else:
            note = error
    return {
        "ok": usable,
        "usable": usable,
        "error": error,
        "note": note,
        "device": whisper_stt.device or "",
        "device_setting": config.get("voice.whisper_device") or "auto",
        "dirs": info.get("dirs") or [],
        "loaded": info.get("loaded") or [],
        "cublas12": bool(info.get("cublas12")),
        "cublas13": info.get("cublas13") or "",
        "install_command": "pip install " + " ".join(CUDA_PACKAGES),
        "install_running": bool(CUDA_INSTALL["running"]),
        "install_ok": CUDA_INSTALL["ok"],
        "install_output": (CUDA_INSTALL["output"] or "")[-1200:],
    }


@app.get("/api/stt/cuda")
async def cuda_status() -> Dict[str, Any]:
    return await asyncio.to_thread(cuda_report)


@app.post("/api/stt/cuda/recheck")
async def cuda_recheck(reload: bool = True) -> Dict[str, Any]:
    """Заново проверить CUDA (например, после установки библиотек)."""
    stt_mod.reset_cuda_state()
    stt_mod.find_cuda_dll_dirs(refresh=True)
    await asyncio.to_thread(stt_mod.prepare_cuda_dlls, True)
    report = await asyncio.to_thread(cuda_report)
    if reload:
        asyncio.create_task(_reload_whisper())
    broadcast({"type": "cuda", **report})
    return report


async def _reload_whisper() -> None:
    """Перезагрузить модель распознавания с текущими настройками."""
    model = config.get("voice.whisper_model", "large-v3")
    whisper_stt.unload()
    broadcast({"type": "stt_model", "model": model, "label": model_label(model),
               "ready": False, "loading": True})
    try:
        await asyncio.to_thread(
            whisper_stt.load,
            model,
            config.get("voice.whisper_device", "auto"),
            config.get("voice.whisper_compute_type", "auto"),
        )
        print(f"[stt] модель {model} перезагружена на {whisper_stt.device}")
        broadcast({"type": "stt_model", "model": model, "label": model_label(model),
                   "ready": True, "loading": False, "device": whisper_stt.device,
                   "fallback_note": whisper_stt.fallback_note})
    except STTError as exc:
        broadcast({"type": "stt_model", "model": model, "label": model_label(model),
                   "ready": False, "loading": False, "error": str(exc)})


@app.post("/api/stt/cuda/install")
async def cuda_install() -> Dict[str, Any]:
    """Поставить библиотеки CUDA 12 (cuBLAS + cuDNN) из PyPI — без CUDA Toolkit.

    Это ровно те пакеты, которые нужны движку распознавания. Они не конфликтуют
    с установленной CUDA 13: Astra сама прописывает их каталоги в PATH.
    """
    if CUDA_INSTALL["running"]:
        return {"ok": False, "error": "Установка уже идёт", **cuda_report()}

    CUDA_INSTALL.update(running=True, output="", ok=None, started_at=time.time())
    broadcast({"type": "cuda_install", "running": True})

    def run() -> None:
        import subprocess
        import sys as _sys

        cmd = [_sys.executable, "-m", "pip", "install", "--upgrade", *CUDA_PACKAGES]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            tail = ((proc.stdout or "") + (proc.stderr or ""))[-3000:]
            CUDA_INSTALL.update(ok=proc.returncode == 0, output=tail)
        except Exception as exc:  # noqa: BLE001
            CUDA_INSTALL.update(ok=False, output=f"{type(exc).__name__}: {exc}")
        finally:
            CUDA_INSTALL["running"] = False
            stt_mod.reset_cuda_state()
            stt_mod.find_cuda_dll_dirs(refresh=True)

        report_ready = CUDA_INSTALL["ok"]
        broadcast({"type": "cuda_install", "running": False, "ok": report_ready,
                   "output": (CUDA_INSTALL["output"] or "")[-800:]})
        if report_ready:
            broadcast({"type": "toast",
                       "message": "Библиотеки CUDA 12 установлены — проверяю видеокарту",
                       "level": "info"})
        else:
            broadcast({"type": "toast",
                       "message": "Установка не удалась — ниже вывод pip. Распознавание "
                                  "продолжает работать на процессоре.",
                       "level": "warn"})
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(lambda: asyncio.create_task(_reload_whisper()))
        except Exception:
            pass

    asyncio.create_task(asyncio.to_thread(run))
    return {"ok": True, "running": True, **cuda_report()}


@app.post("/api/stt/reload")
async def reload_stt(reset_cuda: bool = True) -> Dict[str, Any]:
    """Перезагрузить модель распознавания (и заново проверить CUDA)."""
    if reset_cuda:
        reset_cuda_state()
        whisper_stt.unload()
    try:
        await asyncio.to_thread(
            whisper_stt.load,
            config.get("voice.whisper_model", "large-v3"),
            config.get("voice.whisper_device", "auto"),
            config.get("voice.whisper_compute_type", "auto"),
        )
        return {"ok": True, "model": config.get("voice.whisper_model")}
    except STTError as exc:
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------- режим ---
# Запрос на изменение размера окна. В браузере окно не тронуть, поэтому запрос
# забирает run.py, если приложение запущено в отдельном нативном окне (pywebview).
PENDING_WINDOW: Optional[Dict[str, Any]] = None


def request_window_size(mode: str) -> Dict[str, Any]:
    global PENDING_WINDOW
    size = int(config.get("ui.voice_window_size") or 760)
    if mode == "voice" and config.get("ui.voice_square_window", True):
        PENDING_WINDOW = {"mode": "voice", "width": size, "height": size}
    elif mode == "text":
        PENDING_WINDOW = {"mode": "text", "width": 1280, "height": 860}
    else:
        PENDING_WINDOW = None
    return PENDING_WINDOW or {}


class ModeRequest(BaseModel):
    mode: str
    remember: bool = True
    ask: Optional[bool] = None      # спрашивать ли режим при следующем запуске


@app.post("/api/ui/mode")
async def set_ui_mode(req: ModeRequest) -> Dict[str, Any]:
    """Запомнить выбранный режим работы и попросить окно нужного размера."""
    mode = (req.mode or "").strip().lower()
    if mode not in ("text", "voice"):
        raise HTTPException(status_code=400, detail="Режим должен быть text или voice")
    patch: Dict[str, Any] = {"ui": {"last_mode": mode}}
    if req.remember:
        patch["ui"]["mode"] = mode
    else:
        patch["ui"]["mode"] = ""
    if req.ask is not None:
        patch["ui"]["ask_mode"] = bool(req.ask)
    config.patch(patch)
    request = request_window_size(mode)
    return {
        "ok": True,
        "mode": mode,
        "saved": bool(req.remember),
        "window": request,
        "native_window": bool(getattr(app.state, "native_window", False)),
    }


class WindowSize(BaseModel):
    width: int
    height: int


@app.post("/api/window/size")
async def set_window_size(req: WindowSize) -> Dict[str, Any]:
    """Попросить нативное окно (pywebview) принять нужный размер."""
    global PENDING_WINDOW
    width = max(420, min(3000, int(req.width)))
    height = max(420, min(2400, int(req.height)))
    PENDING_WINDOW = {"mode": "custom", "width": width, "height": height}
    return {"ok": True, "request": PENDING_WINDOW,
            "native_window": bool(getattr(app.state, "native_window", False))}


@app.get("/api/window/next")
async def next_window_request() -> Dict[str, Any]:
    """Забирает ожидающий запрос на размер окна (его опрашивает run.py)."""
    global PENDING_WINDOW
    request, PENDING_WINDOW = PENDING_WINDOW, None
    return {"request": request, "native": bool(getattr(app.state, "native_window", False))}


@app.get("/api/status/lines")
async def get_status_lines() -> Dict[str, Any]:
    """Примеры статусов для подсказки в интерфейсе (что показывает блок под сферой)."""
    from ..status import describe_action

    samples = [
        describe_action("open_app", {"app": "notepad"}),
        describe_action("scroll_in_window", {"amount": -3}),
        describe_action("see_window", {"title": "Chrome"}),
        describe_action("type_text", {"text": "привет"}),
    ]
    return {"ok": True, "samples": samples}


@app.get("/api/system")
async def get_system() -> Dict[str, Any]:
    return {"info": os_control.system_info(), "windows": os_control.list_visible_windows()[:20]}


# ======================================================================
#  WebSocket
# ======================================================================
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    conn = ClientConnection(ws)
    CONNECTIONS.add(conn)
    conn.sender_task = asyncio.create_task(conn.sender())
    conn.emit({
        "type": "hello",
        "version": __version__,
        "model": config.get("lmstudio.model"),
        "tools": len(get_registry().all()),
        "config": config.public(),
        "endpoint": endpoint_public(await endpoints.status()),
        "stt": {"model": config.get("voice.whisper_model"),
                "label": model_label(config.get("voice.whisper_model") or ""),
                "ready": whisper_stt.ready},
        "native_window": bool(getattr(app.state, "native_window", False)),
    })
    if config.get("voice.listen_on_start"):
        asyncio.create_task(start_recording(conn))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await handle_event(conn, event)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"[ws] ошибка соединения: {exc}")
    finally:
        CONNECTIONS.discard(conn)
        await conn.shutdown()


async def handle_event(conn: ClientConnection, event: Dict[str, Any]) -> None:
    kind = event.get("type")

    if kind == "ping":
        conn.emit({"type": "pong", "t": time.time()})
        return

    if kind == "message":
        text = (event.get("text") or "").strip()
        if not text:
            return
        await start_agent_run(conn, text, event.get("session_id"), event.get("modality") or "text")
        return

    if kind == "session_select":
        conn.session_id = event.get("session_id") or None
        return

    if kind == "cancel":
        conn.cancel_run()
        if conn.busy:
            conn.emit({"type": "state", "state": "idle"})
        return

    if kind == "stop_speech":
        tts.stop()
        conn.emit({"type": "state", "state": "idle"})
        return

    if kind == "regenerate":
        asyncio.create_task(start_regenerate(conn, event.get("session_id")))
        return

    if kind == "confirm_response":
        await conn.resolve_confirmation(str(event.get("id") or ""), bool(event.get("approved")))
        return

    if kind == "mode":
        await set_client_mode(conn, str(event.get("mode") or "text"))
        return

    if kind == "record_start":
        await start_recording(conn)
        return

    if kind == "record_stop":
        await finish_recording(conn, auto_send=bool(event.get("auto_send", True)))
        return

    if kind == "record_cancel":
        await cancel_recording(conn)
        return

    if kind == "speak":
        text = (event.get("text") or "").strip()
        if text:
            asyncio.create_task(speak_text(conn, text, "voice"))
        return

    print(f"[ws] неизвестное событие: {kind}")


# ---------------------------------------------------------------- агент ---
async def start_agent_run(conn: ClientConnection, text: str, session_id: Optional[str], modality: str) -> None:
    if conn.recorder is not None:
        # «перестаём слушать», пока выполняется задача
        await cancel_recording(conn)
    if not config.get("lmstudio.model"):
        conn.emit({"type": "error",
                   "message": "Модель не выбрана. Откройте «Настройки» → «Модель LM Studio» и выберите модель "
                              "(или проверьте, что сервер LM Studio запущен)."})
        return

    if conn.busy:
        conn.cancel_run()
        try:
            await asyncio.wait_for(asyncio.shield(conn.run_task), timeout=3)
        except Exception:
            pass

    session = store.ensure_session(session_id, config.get("lmstudio.model") or "")
    conn.session_id = session["id"]
    history = store.list_messages(session["id"], include_hidden=False)
    user_message = store.add_message(session["id"], "user", text, meta={"modality": modality})
    conn.emit({"type": "user_message", "message": user_message, "session": session})

    conn.cancel_event = asyncio.Event()
    task = asyncio.create_task(
        run_agent(conn, session, text, modality, history, conn.cancel_event)
    )
    conn.run_task = task


async def start_regenerate(conn: ClientConnection, session_id: Optional[str]) -> None:
    """Повторить последний ответ: удаляем ответ ассистента и запускаем агента заново."""
    session = store.get_session(session_id or conn.session_id or "")
    if not session:
        conn.emit({"type": "error", "message": "Чат не найден — нечего повторять."})
        return
    if conn.busy:
        conn.emit({"type": "error", "message": "Дождитесь завершения текущей задачи."})
        return
    if not config.get("lmstudio.model"):
        conn.emit({"type": "error", "message": "Модель не выбрана."})
        return

    messages = store.list_messages(session["id"], include_hidden=False)
    last_user_index = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "user":
            last_user_index = i
            break
    if last_user_index is None:
        conn.emit({"type": "error", "message": "В этом чате ещё нет сообщений пользователя."})
        return

    last_user = messages[last_user_index]
    history = messages[:last_user_index]
    store.truncate_after(session["id"], last_user["id"])

    modality = (last_user.get("meta") or {}).get("modality", "text")
    conn.emit({"type": "regenerate_start", "session": session, "message": last_user})
    conn.cancel_event = asyncio.Event()
    conn.run_task = asyncio.create_task(
        run_agent(conn, session, last_user["content"], modality, history, conn.cancel_event)
    )


async def run_agent(conn: ClientConnection, session: Dict[str, Any], text: str, modality: str,
                    history: List[Dict[str, Any]], cancel_event: asyncio.Event) -> None:
    async def emit(event: Dict[str, Any]) -> None:
        conn.emit(event)

    agent = Agent(emit=emit, confirm=conn.request_confirmation)
    started = time.time()
    conn.emit({"type": "state", "state": "thinking"})
    try:
        result = await agent.run(text, history=history, modality=modality, cancel_event=cancel_event)
    except Exception as exc:  # noqa: BLE001
        print(f"[agent] необработанная ошибка: {exc}")
        conn.emit({"type": "error", "message": f"Внутренняя ошибка агента: {exc}"})
        conn.emit({"type": "state", "state": "idle"})
        return

    elapsed = round(time.time() - started, 1)

    if result.error:
        conn.emit({"type": "error", "message": result.error, "retry": True})
        conn.emit({"type": "state", "state": "idle"})
        return

    if result.cancelled and not result.text:
        note = store.add_message(session["id"], "note", "Остановлено пользователем",
                                 meta={"kind": "cancelled", "actions": result.actions})
        conn.emit({"type": "note_message", "message": note})
        conn.emit({"type": "state", "state": "idle"})
        return

    meta = {
        "actions": result.actions,
        "model": result.model,
        "modality": modality,
        "steps": result.steps,
        "elapsed": elapsed,
        "cancelled": result.cancelled,
    }
    assistant_message = store.add_message(session["id"], "assistant", result.text, meta=meta)
    conn.emit({"type": "assistant_message", "message": assistant_message, "elapsed": elapsed})

    # автоназвание сессии
    if config.get("agent.auto_title", True):
        current = store.get_session(session["id"])
        if current and (current.get("title") in ("", "Новый чат") or current.get("title") is None):
            asyncio.create_task(auto_title(conn, current["id"], text, result.text))

    if config.get("voice.tts_enabled"):
        if modality == "voice" or config.get("voice.tts_always"):
            await speak_text(conn, result.text, modality)

    conn.emit({"type": "state", "state": "idle"})
    await resume_listening(conn)


async def auto_title(conn: ClientConnection, session_id: str, user_text: str, answer: str) -> None:
    try:
        agent = Agent(emit=lambda _e: asyncio.sleep(0))
        title = await agent.generate_title(user_text, answer)
        if title:
            session = store.update_session(session_id, title=title[:60])
            if session:
                conn.emit({"type": "session_updated", "session": session})
    except Exception as exc:  # noqa: BLE001
        print(f"[title] не удалось создать название: {exc}")


def stt_error_hint(exc: BaseException | str) -> str:
    """Понятное объяснение ошибки распознавания + что делать."""
    detail = str(exc or "")
    if is_cuda_library_error(detail):
        return ("Видеокарта есть, но библиотеки CUDA 12 не установлены "
                "(cublas64_12.dll / cudnn). Astra уже переключилась на процессор — "
                "попробуйте сказать ещё раз. Чтобы считать на GPU: pip install "
                "nvidia-cublas-cu12 nvidia-cudnn-cu12 и добавьте их папки bin в PATH, "
                "либо оставьте «Устройство: процессор» в Настройки → Голос.")
    if "cuda" in detail.lower() or "gpu" in detail.lower():
        return ("Ошибка GPU при распознавании речи. В Настройки → Голос выберите "
                "«Устройство распознавания: процессор» — это всегда работает. "
                f"Подробности: {detail}")
    if "faster-whisper" in detail.lower() or "ctranslate2" in detail.lower():
        return f"Не установлен движок распознавания. Выполните: pip install faster-whisper. ({detail})"
    if "memory" in detail.lower() or "alloc" in detail.lower():
        return ("Не хватает памяти для модели распознавания. Выберите модель поменьше "
                f"(large-v3-turbo или medium) в Настройки → Голос. ({detail})")
    return f"Не удалось распознать речь: {detail}"


def tts_hint(error: str) -> str:
    """Понятная подсказка, почему нет голоса. Порядок проверок важен."""
    text = (error or "").lower()

    # 1) движков нет совсем
    if "pip install edge-tts" in text or "нет доступных движков" in text:
        return ("Не установлен ни один движок озвучки. Установите edge-tts: pip install edge-tts "
                "или включите офлайн-голос Windows в Параметры → Время и язык → Речь.")

    # 2) именно устройство вывода (не путать с «No audio was received» от edge-tts)
    device_markers = ("устройство вывода", "portaudio", "output stream", "sounddevice",
                      "device unavailable", "invalid device")
    if any(marker in text for marker in device_markers):
        return ("Не удалось открыть устройство вывода звука. Проверьте колонки/наушники и выберите "
                "устройство в Настройки → Голос (или в Параметрах Windows).")

    # 3) онлайн-сервис не отдал звук — сработает офлайн-резерв
    online_markers = ("no audio was received", "edge-tts", "edge", "интернет", "сети",
                      "connection", "timed out", "timeout", "ssl", "403", "401")
    if any(marker in text for marker in online_markers):
        return ("Онлайн-голос (edge-tts) недоступен: нет интернета или сервис Microsoft ограничил "
                "запросы. Astra озвучит офлайн-голосом Windows — проверьте кнопкой «Проверить озвучку». "
                "Если офлайн тоже не работает, включите голос в Параметры → Время и язык → Речь.")
    return ("Озвучка не удалась. Откройте Настройки → Голос и нажмите «Проверить озвучку», "
            "чтобы увидеть точную причину.")


async def speak_text(conn: ClientConnection, text: str, modality: str) -> None:
    clean = clean_for_speech(text)
    if not clean:
        return
    conn.emit({"type": "state", "state": "speaking"})
    try:
        result = await tts.speak(
            clean,
            voice=config.get("voice.tts_voice") or "ru-RU-DmitryNeural",
            rate=config.get("voice.tts_rate") or "+0%",
            volume=config.get("voice.tts_volume") or "+0%",
            on_level=lambda level: conn.emit_level("speech", level),
            device=resolve_device("output"),
            engine=config.get("voice.tts_engine") or "auto",
            sapi_voice=config.get("voice.sapi_voice") or "",
        )
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.emit({"type": "state", "state": "idle"})

    # сигнал клиенту: озвучка закончилась, микрофон можно включать снова
    conn.emit({"type": "tts_done", "ok": bool(isinstance(result, dict) and result.get("ok"))})

    if isinstance(result, dict) and result.get("ok"):
        conn.emit({"type": "tts", "ok": True, "engine": result.get("engine"),
                   "label": result.get("engine_label"), "status": tts.status()})
        if result.get("fallback_from"):
            failed = result.get("fallback_from")
            message = (
                f"Онлайн-голос недоступен — озвучил офлайн: {result.get('engine_label')}"
                if failed == "edge-tts"
                else f"Движок {failed} не сработал — озвучил через {result.get('engine_label')}"
            )
            conn.emit({"type": "toast", "message": message, "level": "info"})
        return

    error = (result or {}).get("error") if isinstance(result, dict) else str(result)
    hint = tts_hint(str(error or ""))
    print(f"[tts] озвучка не удалась: {error}")
    conn.emit({"type": "tts", "ok": False, "error": error, "hint": hint, "status": tts.status()})
    conn.emit({"type": "tts_error", "message": hint, "detail": str(error or ""), "level": "warn"})


# ---------------------------------------------------------------- голос ---
async def set_client_mode(conn: ClientConnection, mode: str) -> None:
    """Переключить режим вкладки. В голосовом сразу включаем прослушивание."""
    conn.voice_mode = mode == "voice"
    if conn.voice_mode:
        if conn.recorder is None and not conn.busy:
            await start_recording(conn)
    elif conn.recorder is not None:
        await cancel_recording(conn)


async def resume_listening(conn: ClientConnection) -> None:
    """Голосовой режим: после ответа снова слушаем пользователя."""
    if not conn.voice_mode or not config.get("voice.continuous_listen", True):
        return
    await asyncio.sleep(0.35)          # озвучка успевает отпустить устройство вывода
    if conn.recorder is None:
        # force=True: эту функцию вызывает сам агент, поэтому conn.busy ещё True
        await start_recording(conn, force=True)


async def start_recording(conn: ClientConnection, force: bool = False) -> None:
    if conn.recorder is not None:
        return
    if conn.busy and not force:
        # агент занят: сначала доделываем работу, микрофон включим после ответа
        conn.emit({"type": "toast", "message": "Сначала дождусь завершения текущей задачи",
                   "level": "info"})
        return
    tts.stop()
    try:
        recorder = Recorder(device=resolve_device("input"))
        recorder.start()
    except AudioUnavailable as exc:
        conn.emit({"type": "error", "message": str(exc)})
        conn.emit({"type": "state", "state": "idle"})
        return
    conn.recorder = recorder
    conn.emit({"type": "state", "state": "listening"})
    conn.record_task = asyncio.create_task(monitor_recording(conn, recorder))


async def monitor_recording(conn: ClientConnection, recorder: Recorder) -> None:
    silence_limit = float(config.get("voice.silence_ms") or 1100) / 1000.0
    max_duration = float(config.get("voice.max_record_s") or 30)
    try:
        while conn.recorder is recorder:
            snap = recorder.snapshot()
            conn.emit_level("mic", min(1.0, snap["level"] * 7.0))
            if snap["voiced"] and snap["silence"] is not None and snap["silence"] > silence_limit:
                break
            if snap["duration"] > max_duration:
                break
            await asyncio.sleep(0.07)
    except asyncio.CancelledError:
        return
    if conn.recorder is recorder:
        await finish_recording(conn, auto_send=bool(config.get("voice.auto_send_voice", True)))


async def finish_recording(conn: ClientConnection, auto_send: bool = True) -> None:
    recorder = conn.recorder
    if recorder is None:
        return
    conn.recorder = None
    if conn.record_task and not conn.record_task.done() and conn.record_task is not asyncio.current_task():
        conn.record_task.cancel()
    audio = recorder.stop()
    conn.record_task = None
    conn.emit({"type": "state", "state": "transcribing"})
    conn.emit_level("mic", 0.0, min_interval=0)

    engine = config.get("voice.stt_engine") or "faster-whisper"
    try:
        if engine == "google":
            from ..speech import transcribe_google

            text = await asyncio.to_thread(transcribe_google, audio, "ru-RU")
        else:
            text = await asyncio.to_thread(
                whisper_stt.transcribe,
                audio,
                config.get("voice.stt_language", "ru"),
                config.get("voice.whisper_model", "large-v3"),
                config.get("voice.whisper_device", "auto"),
                config.get("voice.whisper_compute_type", "auto"),
                lambda msg: conn.emit({"type": "toast", "message": msg, "level": "info"}),
                int(config.get("voice.whisper_beam_size") or 5),
                str(config.get("voice.whisper_initial_prompt") or ""),
            )
    except Exception as exc:  # noqa: BLE001
        message = stt_error_hint(exc)
        print(f"[stt] ошибка распознавания: {exc}")
        conn.emit({"type": "stt_error", "message": message, "detail": str(exc),
                   "level": "warn", "status": whisper_stt.status()})
        conn.emit({"type": "state", "state": "idle"})
        if conn.voice_mode:
            await resume_listening(conn)
        return

    if whisper_stt.fallback_note and not getattr(conn, "_stt_note_shown", False):
        conn._stt_note_shown = True
        conn.emit({"type": "stt_info", "message": whisper_stt.fallback_note,
                   "status": whisper_stt.status(), "level": "info"})

    text = (text or "").strip()
    if not text:
        conn.emit({"type": "stt_result", "text": "", "auto_sent": False})
        conn.emit({"type": "toast", "message": "Речь не распознана — попробуйте ещё раз", "level": "warn"})
        conn.emit({"type": "state", "state": "idle"})
        return

    # одна и та же фраза подряд (шум, эхо колонок) не должна отправляться повторно
    now = time.time()
    if (auto_send and text == conn.last_voice_text and now - conn.last_voice_at < 6.0):
        print(f"[stt] повтор фразы проигнорирован: {text!r}")
        conn.emit({"type": "stt_result", "text": text, "auto_sent": False, "duplicate": True})
        conn.emit({"type": "state", "state": "idle"})
        if conn.voice_mode:
            await resume_listening(conn)
        return
    conn.last_voice_text, conn.last_voice_at = text, now

    conn.emit({"type": "stt_result", "text": text, "auto_sent": auto_send})
    if auto_send:
        await start_agent_run(conn, text, conn.session_id, "voice")
    else:
        conn.emit({"type": "state", "state": "idle"})


async def cancel_recording(conn: ClientConnection) -> None:
    recorder = conn.recorder
    if recorder is None:
        return
    conn.recorder = None
    if conn.record_task and not conn.record_task.done():
        conn.record_task.cancel()
    conn.record_task = None
    recorder.cancel()
    conn.emit({"type": "state", "state": "idle"})
    conn.emit_level("mic", 0.0, min_interval=0)
