"""Соединение с одним клиентом (вкладкой интерфейса)."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from fastapi import WebSocket

from ..speech import Recorder, tts


class ClientConnection:
    """Изолирует состояние одного клиента: очередь событий, запуск агента, запись голоса."""

    def __init__(self, websocket: WebSocket) -> None:
        self.ws = websocket
        self.loop = asyncio.get_event_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sender_task: Optional[asyncio.Task] = None
        self.run_task: Optional[asyncio.Task] = None
        self.cancel_event: Optional[asyncio.Event] = None
        self.pending_confirms: Dict[str, asyncio.Future] = {}
        self.recorder: Optional[Recorder] = None
        self.record_task: Optional[asyncio.Task] = None
        self.speech_task: Optional[asyncio.Task] = None
        self.closed = False
        self.session_id: Optional[str] = None
        # голосовой режим: после ответа и озвучки снова включаем микрофон
        self.voice_mode = False
        # защита от повторной отправки одной и той же фразы (шум, эхо колонок)
        self.last_voice_text = ""
        self.last_voice_at = 0.0
        self._last_level_emit = 0.0

    # --- отправка событий ------------------------------------------------
    def emit(self, event: Dict[str, Any]) -> None:
        """Можно вызывать из любого потока."""
        if self.closed:
            return
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, event)
        except RuntimeError:
            self.closed = True

    def emit_level(self, kind: str, value: float, min_interval: float = 0.045) -> None:
        now = time.monotonic()
        if now - self._last_level_emit < min_interval:
            return
        self._last_level_emit = now
        event = {"type": "level", "kind": kind, "value": round(max(0.0, min(1.0, value)), 3)}
        self.emit(event)

    async def sender(self) -> None:
        while not self.closed:
            event = await self.queue.get()
            if event is None:
                break
            try:
                await self.ws.send_json(event)
            except Exception:
                self.closed = True
                break

    # --- состояние --------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.run_task is not None and not self.run_task.done()

    @property
    def recording(self) -> bool:
        return self.recorder is not None

    def cancel_run(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
        tts.stop()

    async def resolve_confirmation(self, confirm_id: str, approved: bool) -> None:
        future = self.pending_confirms.get(confirm_id)
        if future and not future.done():
            future.set_result(bool(approved))

    async def request_confirmation(self, request: Dict[str, Any], timeout: float = 180.0) -> bool:
        confirm_id = str(request.get("id") or "")
        future: asyncio.Future = self.loop.create_future()
        self.pending_confirms[confirm_id] = future
        payload = {"type": "confirm_request", **request, "id": confirm_id}
        self.emit(payload)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self.emit({"type": "confirm_timeout", "id": confirm_id})
            return False
        finally:
            self.pending_confirms.pop(confirm_id, None)

    async def shutdown(self) -> None:
        self.closed = True
        tts.stop()
        if self.recorder is not None:
            try:
                self.recorder.cancel()
            except Exception:
                pass
            self.recorder = None
        for task in (self.record_task, self.run_task, self.speech_task):
            if task and not task.done():
                task.cancel()
        for future in list(self.pending_confirms.values()):
            if not future.done():
                future.cancel()
        self.pending_confirms.clear()
        try:
            self.queue.put_nowait(None)
        except Exception:
            pass
