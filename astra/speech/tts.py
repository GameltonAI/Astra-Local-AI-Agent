"""Синтез и воспроизведение речи: edge-tts (основной) + pyttsx3 (офлайн-резерв)."""
from __future__ import annotations

import asyncio
import hashlib
import math
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np

from ..paths import TTS_CACHE_DIR, ensure_dirs
from .recorder import _import_sounddevice

FALLBACK_VOICES: List[Dict[str, str]] = [
    {"id": "ru-RU-DmitryNeural", "name": "Дмитрий (мужской)", "locale": "ru-RU", "gender": "Male"},
    {"id": "ru-RU-SvetlanaNeural", "name": "Светлана (женский)", "locale": "ru-RU", "gender": "Female"},
    {"id": "ru-RU-DariyaNeural", "name": "Дария (женский)", "locale": "ru-RU", "gender": "Female"},
    {"id": "en-US-AriaNeural", "name": "Aria (English, female)", "locale": "en-US", "gender": "Female"},
    {"id": "en-US-GuyNeural", "name": "Guy (English, male)", "locale": "en-US", "gender": "Male"},
    {"id": "de-DE-KatjaNeural", "name": "Katja (Deutsch)", "locale": "de-DE", "gender": "Female"},
]


# PowerShell + System.Speech: единственный офлайн-голос, который есть в Windows
# «из коробки». Текст передаём файлом, чтобы кавычки и переводы строк не ломали скрипт.
# (строку НЕ гоняем через .format(): в скрипте есть фигурные скобки PowerShell)
SAPI_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voiceName = '@@VOICE@@'
if ($voiceName -ne '') {
  try { $synth.SelectVoice($voiceName) } catch { }
}
$synth.Rate = @@RATE@@
$synth.Volume = @@VOLUME@@
$text = [IO.File]::ReadAllText('@@TEXTFILE@@', [Text.Encoding]::UTF8)
$synth.SetOutputToWaveFile('@@OUT@@')
try {
  $synth.Speak($text)
} finally {
  $synth.Dispose()
}
"""


# Последний резерв: пусть говорит сам Windows — звук идёт через системный стек,
# sounddevice/PortAudio не нужен вообще.
SAPI_DIRECT_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voiceName = '@@VOICE@@'
if ($voiceName -ne '') {
  try { $synth.SelectVoice($voiceName) } catch { }
}
$synth.Rate = @@RATE@@
$synth.Volume = @@VOLUME@@
$text = [IO.File]::ReadAllText('@@TEXTFILE@@', [Text.Encoding]::UTF8)
try {
  $synth.Speak($text)
} finally {
  $synth.Dispose()
}
"""


class TTSError(RuntimeError):
    pass


def _decode_audio(path: Path) -> tuple[np.ndarray, int]:
    """mp3 → numpy float32. Сначала soundfile, затем ffmpeg."""
    try:
        import soundfile as sf  # type: ignore

        data, samplerate = sf.read(str(path), dtype="float32", always_2d=True)
        mono = data.mean(axis=1) if data.ndim > 1 else data
        return mono.astype(np.float32), int(samplerate)
    except Exception as exc:  # noqa: BLE001
        first_error = exc
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(path), str(wav_path)],
                           check=True, capture_output=True, timeout=60)
            import soundfile as sf  # type: ignore

            data, samplerate = sf.read(str(wav_path), dtype="float32", always_2d=True)
            mono = data.mean(axis=1) if data.ndim > 1 else data
            return mono.astype(np.float32), int(samplerate)
        finally:
            wav_path.unlink(missing_ok=True)
    raise TTSError(
        "Не удалось декодировать mp3. Установите ffmpeg или soundfile с поддержкой mp3 "
        f"(подробности: {first_error})"
    )


class TextToSpeech:
    """Синтез + воспроизведение с колбэком уровня громкости (для анимации кружка)."""

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._lock = asyncio.Lock()
        self._voices: Optional[List[Dict[str, str]]] = None
        self._speaking = False
        self._current_text = ""
        self.last_engine = ""          # каким движком озвучено в последний раз
        self.last_error = ""           # последняя ошибка (для диагностики)
        self.last_error_engine = ""

    # --- голоса ---------------------------------------------------------
    async def list_voices(self, locale_prefix: str = "") -> List[Dict[str, str]]:
        if self._voices is None:
            try:
                import edge_tts  # type: ignore

                raw = await asyncio.wait_for(edge_tts.list_voices(), timeout=15)
                voices = [
                    {
                        "id": item.get("ShortName", ""),
                        "name": item.get("FriendlyName", item.get("ShortName", "")),
                        "locale": item.get("Locale", ""),
                        "gender": item.get("Gender", ""),
                    }
                    for item in raw
                    if item.get("ShortName")
                ]
                # русские — первыми
                voices.sort(key=lambda v: (not v["locale"].startswith("ru"), v["locale"], v["name"]))
                self._voices = voices or list(FALLBACK_VOICES)
            except Exception as exc:  # noqa: BLE001
                print(f"[tts] список голосов недоступен ({exc}), использую локальный набор")
                self._voices = list(FALLBACK_VOICES)
        if locale_prefix:
            return [v for v in self._voices if v["locale"].lower().startswith(locale_prefix.lower())]
        return list(self._voices)

    # --- офлайн-движки (Windows System.Speech) ---------------------------
    @staticmethod
    def _powershell_exe() -> str:
        """Путь к PowerShell: из PATH, иначе стандартные места Windows."""
        found = shutil.which("powershell") or shutil.which("pwsh")
        if found:
            return found
        if sys.platform.startswith("win"):
            for candidate in (
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                r"C:\Program Files\PowerShell\7\pwsh.exe",
            ):
                if Path(candidate).exists():
                    return candidate
        return ""

    @classmethod
    def sapi_available(cls) -> bool:
        return bool(cls._powershell_exe())

    def _sapi_cache_path(self, text: str, voice: str, rate: int, volume: int) -> Path:
        key = hashlib.sha256(f"sapi|{voice}|{rate}|{volume}|{text}".encode("utf-8")).hexdigest()[:24]
        return TTS_CACHE_DIR / f"{key}.wav"

    async def synthesize_sapi(self, text: str, voice: str = "", rate_percent: int = 0,
                              volume_percent: int = 100) -> Path:
        """Офлайн-синтез голосом Windows (System.Speech) сразу в WAV-файл."""
        exe = self._powershell_exe()
        if not exe:
            raise TTSError("Офлайн-движок Windows SAPI недоступен (нет PowerShell)")
        ensure_dirs()
        text = (text or "").strip()
        if not text:
            raise TTSError("Пустой текст для озвучки")
        rate_percent = max(-10, min(10, int(rate_percent)))
        volume_percent = max(0, min(100, int(volume_percent)))
        cache = self._sapi_cache_path(text, voice, rate_percent, volume_percent)
        if cache.exists() and cache.stat().st_size > 512:
            return cache

        tmp = cache.with_suffix(".part.wav")
        tmp.unlink(missing_ok=True)
        text_file = cache.with_suffix(".part.txt")

        def _ps(value: str) -> str:
            return str(value).replace("'", "''")

        script = (
            SAPI_SCRIPT.replace("@@OUT@@", _ps(tmp))
            .replace("@@TEXTFILE@@", _ps(text_file))
            .replace("@@VOICE@@", _ps(voice or ""))
            .replace("@@RATE@@", str(rate_percent))
            .replace("@@VOLUME@@", str(volume_percent))
        )
        # PowerShell 5.1 читает без BOM как UTF-8 не всегда — пишем с BOM
        text_file.write_text(text, encoding="utf-8-sig")
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [exe, "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
            )
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"Офлайн-синтез не запустился: {exc}") from exc
        finally:
            text_file.unlink(missing_ok=True)
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 512:
            tail = (proc.stderr or proc.stdout or "").strip()[:300]
            tmp.unlink(missing_ok=True)
            raise TTSError(f"Офлайн-синтез Windows не сработал: {tail or 'нет файла на выходе'}")
        tmp.replace(cache)
        return cache

    async def speak_sapi_direct(self, text: str, voice: str = "", rate_percent: int = 0,
                               volume_percent: int = 100) -> None:
        """Озвучить текст силами Windows — без WAV-файла и без sounddevice."""
        exe = self._powershell_exe()
        if not exe:
            raise TTSError("Офлайн-движок Windows SAPI недоступен (нет PowerShell)")
        ensure_dirs()
        text = (text or "").strip()
        if not text:
            raise TTSError("Пустой текст для озвучки")
        rate_percent = max(-10, min(10, int(rate_percent)))
        volume_percent = max(0, min(100, int(volume_percent)))
        tmp_text = TTS_CACHE_DIR / f"direct_{abs(hash((text, voice, rate_percent))) % (10 ** 12)}.txt"
        script = (
            SAPI_DIRECT_SCRIPT.replace("@@TEXTFILE@@", str(tmp_text).replace("'", "''"))
            .replace("@@VOICE@@", str(voice or "").replace("'", "''"))
            .replace("@@RATE@@", str(rate_percent))
            .replace("@@VOLUME@@", str(volume_percent))
        )
        tmp_text.write_text(text, encoding="utf-8-sig")
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [exe, "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
            )
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"Прямая озвучка Windows не запустилась: {exc}") from exc
        finally:
            tmp_text.unlink(missing_ok=True)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise TTSError(f"Прямая озвучка Windows не сработала: {tail or 'код возврата ' + str(proc.returncode)}")

    @staticmethod
    def pyttsx3_available() -> bool:
        try:
            import pyttsx3  # type: ignore  # noqa: F401

            return True
        except Exception:
            return False

    def available_engines(self) -> Dict[str, bool]:
        edge_ok = True
        try:
            import edge_tts  # type: ignore  # noqa: F401
        except Exception:
            edge_ok = False
        return {"edge-tts": edge_ok, "sapi": self.sapi_available(),
                "pyttsx3": self.pyttsx3_available()}

    # --- синтез ---------------------------------------------------------
    def _cache_path(self, text: str, voice: str, rate: str, volume: str) -> Path:
        key = hashlib.sha256(f"{voice}|{rate}|{volume}|{text}".encode("utf-8")).hexdigest()[:24]
        return TTS_CACHE_DIR / f"{key}.mp3"

    async def _synth_bytes(self, text: str, voice: str, rate: str, volume: str) -> bytes:
        import edge_tts  # type: ignore

        communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
        data = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                data.extend(chunk["data"])
        if len(data) < 512:
            raise TTSError("edge-tts вернул пустое аудио")
        return bytes(data)

    @staticmethod
    def _split_sentences(text: str, limit: int = 180) -> List[str]:
        """Делим длинный текст на предложения — так синтез стабильнее и начинается быстрее."""
        import re

        parts = re.split(r"(?<=[.!?…;:])\s+", text)
        chunks: List[str] = []
        current = ""
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if len(part) > limit:
                # очень длинное предложение режем по запятым
                for piece in re.split(r"(?<=,)\s+", part):
                    if len(current) + len(piece) + 1 <= limit:
                        current = f"{current} {piece}".strip()
                    else:
                        if current:
                            chunks.append(current)
                        current = piece
                continue
            if len(current) + len(part) + 1 <= limit:
                current = f"{current} {part}".strip()
            else:
                if current:
                    chunks.append(current)
                current = part
        if current:
            chunks.append(current)
        return chunks or [text]

    async def synthesize(self, text: str, voice: str, rate: str = "+0%", volume: str = "+0%") -> Path:
        ensure_dirs()
        text = (text or "").strip()
        if not text:
            raise TTSError("Пустой текст для озвучки")
        cache = self._cache_path(text, voice, rate, volume)
        if cache.exists() and cache.stat().st_size > 1024:
            return cache
        try:
            import edge_tts  # type: ignore  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            raise TTSError("Модуль edge-tts не установлен: pip install edge-tts") from exc

        tmp = cache.with_suffix(".part")
        last_error: Optional[Exception] = None

        # 1) пробуем целиком, с повторами (сервис иногда отдаёт пустой ответ)
        for attempt in range(3):
            try:
                data = await self._synth_bytes(text, voice, rate, volume)
                tmp.write_bytes(data)
                tmp.replace(cache)
                return cache
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                await asyncio.sleep(0.5 + attempt * 0.7)

        # 2) пробуем по предложениям и склеиваем mp3-потоки
        chunks = self._split_sentences(text)
        if len(chunks) > 1:
            collected = bytearray()
            failed = 0
            for chunk in chunks:
                for attempt in range(2):
                    try:
                        collected.extend(await self._synth_bytes(chunk, voice, rate, volume))
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_error = exc
                        await asyncio.sleep(0.4)
                else:
                    failed += 1
            if collected and failed < len(chunks):
                tmp.write_bytes(bytes(collected))
                tmp.replace(cache)
                return cache

        tmp.unlink(missing_ok=True)
        raise TTSError(f"Не удалось синтезировать речь (проверьте интернет и голос): {last_error}")

    # --- воспроизведение ------------------------------------------------
    @staticmethod
    def _parse_percent(value: str) -> int:
        try:
            return int(str(value or "0").replace("%", "").replace("+", "").strip())
        except (TypeError, ValueError):
            return 0

    def _engine_chain(self, engine: str) -> List[str]:
        """Порядок движков: основной, затем офлайн-резервы."""
        available = self.available_engines()
        if engine in ("edge-tts", "sapi", "pyttsx3"):
            primary = engine
        else:  # auto и любое незнакомое значение
            primary = "edge-tts"
        order = {
            "edge-tts": ["edge-tts", "sapi", "pyttsx3"],
            "sapi": ["sapi", "edge-tts", "pyttsx3"],
            "pyttsx3": ["pyttsx3", "sapi", "edge-tts"],
        }[primary]
        return [name for name in order if available.get(name)]

    async def speak(
        self,
        text: str,
        voice: str,
        rate: str = "+0%",
        volume: str = "+0%",
        on_level: Optional[Callable[[float], None]] = None,
        device: Optional[int] = None,
        engine: str = "auto",
        sapi_voice: str = "",
    ) -> Dict[str, Any]:
        """Озвучить текст, автоматически переключаясь между движками.

        Возвращает {"ok", "engine", "engine_label", "duration", "fallback_from", "error"}.
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "engine": "", "engine_label": "", "duration": 0.0,
                    "fallback_from": "", "error": "Пустой текст"}
        async with self._lock:
            self._stop_event.clear()
            self._speaking = True
            self._current_text = text
            started = time.time()
            chain = self._engine_chain(engine)
            if not chain:
                self._speaking = False
                self.last_error = ("Нет доступных движков озвучки: edge-tts не установлен, "
                                   "офлайн-голоса Windows недоступны. Установите: pip install edge-tts")
                return {"ok": False, "engine": "", "engine_label": "", "duration": 0.0,
                        "fallback_from": "", "error": self.last_error}

            errors: List[str] = []
            try:
                for index, name in enumerate(chain):
                    if self._stop_event.is_set():
                        break
                    try:
                        if name == "edge-tts":
                            path = await self.synthesize(text, voice, rate, volume)
                            audio, samplerate = await asyncio.to_thread(_decode_audio, path)
                            await asyncio.to_thread(self._play, audio, samplerate, on_level, device)
                            label = f"edge-tts · {voice}"
                        elif name == "sapi":
                            rate_percent = self._parse_percent(rate)
                            volume_percent = max(0, 100 + self._parse_percent(volume))
                            try:
                                path = await self.synthesize_sapi(
                                    text,
                                    voice=sapi_voice or "",
                                    rate_percent=rate_percent,
                                    volume_percent=volume_percent,
                                )
                                audio, samplerate = await asyncio.to_thread(_decode_audio, path)
                                await asyncio.to_thread(self._play, audio, samplerate, on_level, device)
                                label = "офлайн-голос Windows (SAPI)"
                            except Exception as playback_error:  # noqa: BLE001
                                # бывает, что sounddevice/PortAudio не видит колонки —
                                # тогда пусть говорит сам Windows, звук пойдёт мимо Python
                                print(f"[tts] SAPI через sounddevice не удался ({playback_error}), "
                                      f"говорю напрямую голосом Windows")
                                await self.speak_sapi_direct(
                                    text,
                                    voice=sapi_voice or "",
                                    rate_percent=rate_percent,
                                    volume_percent=volume_percent,
                                )
                                label = "офлайн-голос Windows (прямое воспроизведение)"
                        elif name == "pyttsx3":
                            await asyncio.to_thread(self._speak_pyttsx3, text, on_level)
                            label = "pyttsx3"
                        else:
                            continue

                        self.last_engine = name
                        self.last_error = ""
                        duration = time.time() - started
                        print(f"[tts] озвучено через {name} ({duration:.1f} с)")
                        return {
                            "ok": True,
                            "engine": name,
                            "engine_label": label,
                            "duration": round(duration, 2),
                            "fallback_from": chain[0] if index > 0 else "",
                            "error": "; ".join(errors),
                        }
                    except Exception as exc:  # noqa: BLE001
                        message = str(exc) or type(exc).__name__
                        errors.append(f"{name}: {message}")
                        self.last_error_engine = name
                        print(f"[tts] движок {name} не сработал: {message}")
                        continue

                self.last_error = "; ".join(errors) or "Озвучка не выполнена"
                return {"ok": False, "engine": "", "engine_label": "", "duration": 0.0,
                        "fallback_from": "", "error": self.last_error}
            finally:
                self._speaking = False
                self._current_text = ""
                if on_level is not None:
                    try:
                        on_level(0.0)
                    except Exception:
                        pass

    def status(self) -> Dict[str, Any]:
        engines = self.available_engines()
        return {
            "engines": engines,
            "any_available": any(engines.values()),
            "last_engine": self.last_engine,
            "last_error": self.last_error,
            "last_error_engine": self.last_error_engine,
            "speaking": self._speaking,
            "offline_capable": bool(engines.get("sapi") or engines.get("pyttsx3")),
        }

    def _play(self, audio: np.ndarray, samplerate: int,
              on_level: Optional[Callable[[float], None]], device: Optional[int]) -> None:
        sd = _import_sounddevice()
        if audio.size == 0:
            return
        block = max(256, int(samplerate * 0.05))  # 50 мс
        try:
            stream = sd.OutputStream(samplerate=samplerate, channels=1, dtype="float32", device=device)
            stream.start()
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"Не удалось открыть устройство вывода звука: {exc}") from exc
        try:
            for start in range(0, audio.size, block):
                if self._stop_event.is_set():
                    break
                chunk = audio[start:start + block]
                stream.write(chunk.astype(np.float32).reshape(-1, 1))
                if on_level is not None:
                    level = float(math.sqrt(float(np.mean(np.square(chunk))))) if chunk.size else 0.0
                    try:
                        on_level(min(1.0, level * 5.0))
                    except Exception:
                        pass
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def _speak_pyttsx3(self, text: str, on_level: Optional[Callable[[float], None]]) -> None:
        import pyttsx3  # type: ignore

        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
        if on_level:
            on_level(0.0)

    def stop(self) -> None:
        """Прервать воспроизведение (можно вызывать из другого потока)."""
        self._stop_event.set()

    @property
    def speaking(self) -> bool:
        return self._speaking


tts = TextToSpeech()
