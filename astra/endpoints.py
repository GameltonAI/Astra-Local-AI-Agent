"""Выбор сервера LM Studio: сначала локальная сеть, затем этот компьютер.

Логика:
1. При старте и далее периодически проверяем кандидатов по порядку
   (`lmstudio.candidates`, по умолчанию LAN → localhost).
2. Первый ответивший сервер становится активным; модель, если её нет на новом
   сервере, автоматически заменяется на первую доступную.
3. Если активный сервер перестал отвечать (например, выключили компьютер в
   локальной сети), запрос автоматически повторяется на следующем кандидате —
   приложению не нужно перезапускаться.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from .config import config

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


# ----------------------------------------------------------------------
#  Утилиты
# ----------------------------------------------------------------------
def normalize_url(url: str) -> str:
    """'192.168.0.32:1234' → 'http://192.168.0.32:1234/v1'."""
    url = (url or "").strip().strip("/")
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    parsed = urlparse(url)
    path = (parsed.path or "").rstrip("/")
    if not path:
        path = "/v1"
    elif not path.endswith("/v1"):
        path = path + "/v1"
    netloc = parsed.netloc
    return f"{parsed.scheme}://{netloc}{path}"


def url_kind(url: str) -> str:
    """'local' — этот компьютер, 'lan' — другой узел сети."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        host = ""
    return "local" if host in LOCAL_HOSTS else "lan"


def kind_label(url: str) -> str:
    return "этот компьютер" if url_kind(url) == "local" else "локальная сеть"


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectTimeout):
        return "нет соединения (таймаут)"
    if isinstance(exc, httpx.ReadTimeout):
        return "сервер не ответил вовремя"
    if isinstance(exc, httpx.ConnectError):
        return "нет соединения"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return f"{type(exc).__name__}: {exc}"


def _default_candidates() -> List[str]:
    raw = config.get("lmstudio.candidates")
    if not isinstance(raw, list) or not raw:
        raw = ["http://192.168.0.32:1234/v1", "http://127.0.0.1:1234/v1"]
    result: List[str] = []
    for item in raw:
        url = normalize_url(str(item))
        if url and url not in result:
            result.append(url)
    return result


def _auth_headers() -> Dict[str, str]:
    key = config.get("lmstudio.api_key")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _parse_models(payload: Any) -> List[Dict[str, Any]]:
    items = payload.get("data") if isinstance(payload, dict) else payload
    models: List[Dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, str):
            models.append({"id": item, "name": item})
        elif isinstance(item, dict):
            mid = item.get("id") or item.get("model") or item.get("name")
            if not mid:
                continue
            models.append({
                "id": mid,
                "name": item.get("name") or mid,
                "owned_by": item.get("owned_by", ""),
                "context": (item.get("max_context_length") or item.get("context_length")
                            or item.get("loaded_context_length")),
                "arch": item.get("arch", ""),
            })
    models.sort(key=lambda m: m["id"].lower())
    return models


# ----------------------------------------------------------------------
#  Менеджер эндпоинтов
# ----------------------------------------------------------------------
class EndpointManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active: Optional[str] = None
        self._models: List[Dict[str, Any]] = []
        self._checked_at: float = 0.0
        self._probes: List[Dict[str, Any]] = []
        self._error: str = ""
        self._switched_from: Optional[str] = None
        self._failed_url: Optional[str] = None
        self._listener: Optional[Callable[[Dict[str, Any]], None]] = None
        self._last_signature: Tuple[str, bool] = ("", False)

    def set_listener(self, listener: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        """Колбэк, вызываемый при смене активного сервера (для уведомления интерфейса)."""
        self._listener = listener

    # --- проверка --------------------------------------------------------
    async def probe(self, url: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        url = normalize_url(url)
        if not url:
            return {"url": "", "ok": False, "error": "пустой адрес", "models": [],
                    "kind": "unknown", "latency_ms": 0}
        limit = float(timeout if timeout is not None else (config.get("lmstudio.probe_timeout") or 2.5))
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(limit, connect=limit), headers=_auth_headers(), follow_redirects=True,
            ) as client:
                resp = await client.get(url + "/models")
                latency = int((time.perf_counter() - started) * 1000)
                if resp.status_code >= 400:
                    return {"url": url, "ok": False, "models": [], "kind": url_kind(url),
                            "latency_ms": latency,
                            "error": f"HTTP {resp.status_code}: {resp.text[:160]}"}
                try:
                    payload = resp.json()
                except ValueError:
                    return {"url": url, "ok": False, "models": [], "kind": url_kind(url),
                            "latency_ms": latency, "error": "ответ не является JSON"}
                models = _parse_models(payload)
                return {"url": url, "ok": True, "models": models, "kind": url_kind(url),
                        "latency_ms": latency, "error": "" if models else "сервер доступен, но моделей нет"}
        except Exception as exc:  # noqa: BLE001
            return {"url": url, "ok": False, "models": [], "kind": url_kind(url),
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "error": _friendly_error(exc)}

    # --- выбор активного сервера -----------------------------------------
    async def resolve(self, force: bool = False, probe_all: bool = False) -> Dict[str, Any]:
        """Вернуть активный сервер, при необходимости перепроверив кандидатов.

        probe_all=True проверяет все адреса (нужно для экрана настроек),
        иначе проверка останавливается на первом рабочем — это быстрее.
        """
        ttl = float(config.get("lmstudio.endpoint_ttl") or 30)
        async with self._lock:
            fresh = self._active and (time.time() - self._checked_at) < ttl
            if fresh and not force:
                return self._snapshot()

            auto = config.get("lmstudio.auto_detect", True)
            if auto:
                candidates = _default_candidates()
            else:
                candidates = [normalize_url(config.get("lmstudio.base_url") or "")]

            chosen: Optional[Dict[str, Any]] = None
            probes: List[Dict[str, Any]] = []
            for url in candidates:
                if not url:
                    continue
                probe = await self.probe(url)
                probes.append(probe)
                if probe["ok"] and chosen is None:
                    chosen = probe
                    if not probe_all:
                        break   # нашли ближайший рабочий — остальных не дёргаем

            previous = self._active or self._failed_url
            self._checked_at = time.time()
            self._probes = probes
            if chosen:
                self._active = chosen["url"]
                self._models = chosen["models"]
                self._error = ""
                self._switched_from = previous if previous and previous != chosen["url"] else None
                self._failed_url = None
                self._sync_model(chosen)
                self._notify_if_changed()
            else:
                self._active = None
                self._models = []
                errors = "; ".join(f"{p['url']} — {p['error']}" for p in probes if p.get("url"))
                self._error = ("LM Studio недоступен ни по одному адресу: " + (errors or "адреса не заданы")
                               + ". Запустите LM Studio и включите Developer → Local Server.")
                self._switched_from = previous
                self._notify_if_changed()
            return self._snapshot()

    def _notify_if_changed(self) -> None:
        signature = (self._active or "", bool(self._active))
        if signature == self._last_signature or self._listener is None:
            self._last_signature = signature
            return
        self._last_signature = signature
        try:
            self._listener(self._snapshot())
        except Exception as exc:  # noqa: BLE001
            print(f"[endpoints] ошибка уведомления: {exc}")

    def _sync_model(self, chosen: Dict[str, Any]) -> None:
        """Если выбранной модели нет на этом сервере — берём первую доступную."""
        ids = [m["id"] for m in chosen.get("models") or []]
        if not ids:
            return
        current = (config.get("lmstudio.model") or "").strip()
        if current in ids:
            return
        config.set("lmstudio.model", ids[0])
        chosen["model_switched"] = {"from": current, "to": ids[0]}

    def mark_failed(self, url: str) -> None:
        """Сервер перестал отвечать — следующий resolve выберет адрес заново.

        Упавший адрес запоминается, чтобы интерфейс мог показать, откуда
        произошло переключение.
        """
        if url:
            self._failed_url = url
        self._active = None
        self._checked_at = 0.0

    def invalidate(self) -> None:
        self._checked_at = 0.0

    # --- доступ к результату ---------------------------------------------
    def _snapshot(self) -> Dict[str, Any]:
        return {
            "ok": bool(self._active),
            "url": self._active or "",
            "kind": url_kind(self._active) if self._active else "unknown",
            "kind_label": kind_label(self._active) if self._active else "",
            "model": config.get("lmstudio.model") or "",
            "models": list(self._models),
            "probes": list(self._probes),
            "error": self._error,
            "checked_at": self._checked_at,
            "switched_from": self._switched_from,
            "auto_detect": bool(config.get("lmstudio.auto_detect", True)),
            "candidates": _default_candidates(),
        }

    async def status(self, refresh: bool = False, probe_all: bool = False) -> Dict[str, Any]:
        return await self.resolve(force=refresh, probe_all=probe_all)

    async def active_url(self, force: bool = False) -> str:
        """URL активного сервера или понятная ошибка."""
        result = await self.resolve(force=force)
        if result["ok"]:
            return result["url"]
        raise EndpointUnavailable(result["error"], result)

    async def active_models(self) -> Tuple[str, List[Dict[str, Any]]]:
        result = await self.resolve()
        if not result["ok"]:
            raise EndpointUnavailable(result["error"], result)
        return result["url"], result["models"]


class EndpointUnavailable(RuntimeError):
    def __init__(self, message: str, info: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.info = info or {}


endpoints = EndpointManager()
