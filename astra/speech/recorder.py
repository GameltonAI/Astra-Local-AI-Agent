"""Запись звука с микрофона + простое определение речи (VAD)."""
from __future__ import annotations

import math
import threading
import time
from typing import Callable, List, Optional

import numpy as np

SAMPLE_RATE = 16000
BLOCK_MS = 30
BLOCK_SIZE = int(SAMPLE_RATE * BLOCK_MS / 1000)


class AudioUnavailable(RuntimeError):
    """Нет звуковой подсистемы / устройства."""


def _import_sounddevice():
    try:
        import sounddevice as sd  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise AudioUnavailable(
            "Модуль sounddevice не установлен или не видит аудиоустройства. "
            "Установите зависимости: pip install sounddevice numpy (и проверьте, что PortAudio доступен)."
        ) from exc
    return sd


def list_devices() -> List[dict]:
    sd = _import_sounddevice()
    devices = []
    try:
        default_in = sd.default.device[0]
        default_out = sd.default.device[1]
    except Exception:
        default_in = default_out = -1
    for idx, dev in enumerate(sd.query_devices()):
        devices.append({
            "index": idx,
            "name": dev.get("name", f"Устройство {idx}"),
            "channels_in": dev.get("max_input_channels", 0),
            "channels_out": dev.get("max_output_channels", 0),
            "samplerate": dev.get("default_samplerate"),
            "default_input": idx == default_in,
            "default_output": idx == default_out,
        })
    return devices


class Recorder:
    """Пишет с микрофона в фоновом потоке. Позволяет следить за уровнем и тишиной."""

    def __init__(self, device: Optional[int] = None, on_level: Optional[Callable[[float], None]] = None):
        self.device = device
        self._on_level = on_level
        self._stream = None
        self._frames: List[np.ndarray] = []
        self._lock = threading.Lock()
        self._recording = False
        self._level = 0.0
        self._peak = 0.0
        self._last_voice_at: Optional[float] = None
        self._started_at = 0.0

    # ------------------------------------------------------------------
    def start(self) -> None:
        sd = _import_sounddevice()
        with self._lock:
            self._frames = []
            self._peak = 0.0
            self._level = 0.0
            self._last_voice_at = None
            self._started_at = time.monotonic()
            self._recording = True
        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK_SIZE,
                channels=1,
                dtype="float32",
                device=self.device,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            self._recording = False
            raise AudioUnavailable(f"Не удалось открыть микрофон: {exc}") from exc

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        if not self._recording:
            return
        block = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
        rms = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
        with self._lock:
            self._frames.append(block)
            self._level = rms
            if rms > self._peak:
                self._peak = rms
            if rms > 0.012:
                self._last_voice_at = time.monotonic()
        if self._on_level is not None:
            try:
                self._on_level(min(1.0, rms * 6.0))
            except Exception:
                pass

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            silence = None
            if self._last_voice_at is not None:
                silence = now - self._last_voice_at
            return {
                "level": self._level,
                "peak": self._peak,
                "voiced": self._last_voice_at is not None,
                "silence": silence,
                "duration": now - self._started_at,
                "samples": sum(f.size for f in self._frames),
            }

    def stop(self) -> np.ndarray:
        with self._lock:
            self._recording = False
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        with self._lock:
            frames, self._frames = self._frames, []
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(frames).astype(np.float32)

    def cancel(self) -> None:
        self.stop()


def normalize_audio(audio: np.ndarray) -> np.ndarray:
    """Убираем DC-смещение и нормализуем громкость — whisper так стабильнее."""
    if audio.size == 0:
        return audio
    audio = audio - float(np.mean(audio))
    peak = float(np.max(np.abs(audio)))
    if peak > 1e-4:
        target = 0.85
        audio = audio * (target / peak)
    return np.clip(audio, -1.0, 1.0).astype(np.float32)


def rms_level(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    return float(math.sqrt(float(np.mean(np.square(block)))))
