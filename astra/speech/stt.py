"""Распознавание речи: faster-whisper (локально) и Google Speech API (онлайн, резерв)."""
from __future__ import annotations

import ctypes
import io
import os
import sys
import threading
import time
import wave
from typing import Callable, Optional

import numpy as np

from .recorder import SAMPLE_RATE, normalize_audio


class STTError(RuntimeError):
    pass


# Модели faster-whisper, из которых можно выбирать в интерфейсе.
# size — примерный размер загрузки, чтобы человек понимал цену точности.
WHISPER_MODELS = [
    {"id": "large-v3", "size": "~3.1 ГБ", "quality": "максимальная",
     "label": "large-v3 — максимальная точность", "recommended": True},
    {"id": "large-v3-turbo", "size": "~1.6 ГБ", "quality": "почти как large-v3",
     "label": "large-v3-turbo — быстрее large-v3", "recommended": False},
    {"id": "distil-large-v3", "size": "~1.5 ГБ", "quality": "высокая",
     "label": "distil-large-v3 — компромисс", "recommended": False},
    {"id": "medium", "size": "~1.5 ГБ", "quality": "средняя",
     "label": "medium — средняя точность", "recommended": False},
    {"id": "small", "size": "~460 МБ", "quality": "низкая, часто путает слова",
     "label": "small — быстро, но путается", "recommended": False},
    {"id": "base", "size": "~140 МБ", "quality": "низкая",
     "label": "base — быстро, менее точно", "recommended": False},
    {"id": "tiny", "size": "~75 МБ", "quality": "очень низкая",
     "label": "tiny — очень быстро, неточно", "recommended": False},
]


def available_models() -> list[dict]:
    """Список моделей для выпадающего списка в настройках."""
    return [dict(item) for item in WHISPER_MODELS]


def model_label(model_id: str) -> str:
    for item in WHISPER_MODELS:
        if item["id"] == model_id:
            return item["label"]
    return model_id or "не выбрана"


# ======================================================================
#  CUDA: видеокарта может быть, а библиотек CUDA 12 (cuBLAS/cuDNN) — нет.
#  Тогда ctranslate2 падает с «Library cublas64_12.dll is not found».
#  Такие ошибки распознаём и автоматически уходим на процессор.
# ======================================================================
_CUDA_STATE: dict = {"usable": None, "error": "", "dlls": [], "cublas12": False, "cublas13": ""}

# Загруженные библиотеки держим ссылками: если отпустить, Windows выгрузит DLL
# и ctranslate2 снова не сможет её найти.
_CUDA_HANDLES: list = []
_DLL_DIRS_CACHE: Optional[list] = None

# имена, которые ищет ctranslate2 (Linux — с префиксом lib)
CUDA_DLL_NAMES = ("cublas64_12.dll", "cublasLt64_12.dll", "cudart64_12.dll", "cudnn64_9.dll")
CUDA_SO_NAMES = ("libcublas.so.12", "libcublasLt.so.12", "libcudart.so.12", "libcudnn.so.9")


def _nvidia_roots() -> list:
    roots = []
    for entry in list(sys.path):
        if entry:
            roots.append(entry)
    try:
        import site as site_mod

        if hasattr(site_mod, "getsitepackages"):
            roots.extend(site_mod.getsitepackages())
        roots.append(site_mod.getusersitepackages())
    except Exception:
        pass
    try:
        import sysconfig
        roots.append(sysconfig.get_paths().get("purelib", ""))
        roots.append(sysconfig.get_paths().get("platlib", ""))
    except Exception:
        pass
    seen, unique = set(), []
    for root in roots:
        if root and root not in seen:
            seen.add(root)
            unique.append(root)
    return unique


def find_cuda_dll_dirs(refresh: bool = False) -> list:
    """Каталоги, где лежат библиотеки CUDA 12 (из pip-пакетов nvidia-*-cu12 или toolkit)."""
    global _DLL_DIRS_CACHE
    if _DLL_DIRS_CACHE is not None and not refresh:
        return list(_DLL_DIRS_CACHE)

    dirs: list = []
    # 1) явно указанный пользователем путь имеет приоритет
    for env_name in ("ASTRA_CUDA_PATH", "CUDA_PATH"):
        raw = os.environ.get(env_name) or ""
        for part in raw.split(os.pathsep):
            if part and os.path.isdir(part):
                dirs.append(part)
    # 2) pip-пакеты nvidia-cublas-cu12 / nvidia-cudnn-cu12 → site-packages/nvidia/<pkg>/bin|lib
    subpackages = ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc", "cublasLt")
    leaves = ("bin", "lib", "lib/x64")
    for root in _nvidia_roots():
        base = os.path.join(root, "nvidia")
        if not os.path.isdir(base):
            continue
        for pkg in subpackages:
            for leaf in leaves:
                candidate = os.path.join(base, pkg, *leaf.split("/"))
                if os.path.isdir(candidate) and candidate not in dirs:
                    dirs.append(candidate)

    def has_libs(folder: str) -> bool:
        try:
            names = set(os.listdir(folder))
        except Exception:
            return False
        return any(name in names for name in CUDA_DLL_NAMES + CUDA_SO_NAMES)

    # 3) классический CUDA Toolkit на диске (любая версия 12.x)
    toolkit_roots = []
    for env_name in ("ProgramFiles", "ProgramW6432"):
        base = os.environ.get(env_name)
        if base:
            toolkit_roots.append(os.path.join(base, "NVIDIA GPU Computing Toolkit", "CUDA"))
    for toolkit in toolkit_roots:
        if not os.path.isdir(toolkit):
            continue
        for version in sorted(os.listdir(toolkit), reverse=True):
            bin_dir = os.path.join(toolkit, version, "bin")
            if os.path.isdir(bin_dir) and bin_dir not in dirs:
                dirs.append(bin_dir)

    _DLL_DIRS_CACHE = [d for d in dirs if os.path.isdir(d)]
    _CUDA_STATE["dlls"] = list(_DLL_DIRS_CACHE)
    return list(_DLL_DIRS_CACHE)


def _find_library(names: tuple, dirs: list) -> str:
    for folder in dirs:
        for name in names:
            path = os.path.join(folder, name)
            if os.path.exists(path):
                return path
    return ""


def find_cublas13() -> str:
    """Есть ли на машине cuBLAS от CUDA 13 (тогда подскажем, почему не подходит)."""
    candidates = []
    for env_name in ("CUDA_PATH", "PATH"):
        raw = os.environ.get(env_name) or ""
        candidates.extend([p for p in raw.split(os.pathsep) if p])
    for folder in candidates:
        if not os.path.isdir(folder):
            continue
        for name in ("cublas64_13.dll", "libcublas.so.13"):
            path = os.path.join(folder, name)
            if os.path.exists(path):
                return path
    return ""


def prepare_cuda_dlls(refresh: bool = False) -> dict:
    """Сделать библиотеки CUDA 12 видимыми для ctranslate2.

    Зачем: пакеты ``nvidia-cublas-cu12`` кладут DLL в ``site-packages/nvidia/cublas/bin``,
    а Windows этот каталог не просматривает. ctranslate2 вызывает LoadLibraryA по имени,
    поэтому мы заранее прописываем каталоги в PATH и подгружаем DLL с полным путём.
    Так GPU работает и тогда, когда в системе стоит CUDA 13 (у неё cublas64_13.dll,
    несовместимая с ctranslate2).
    """
    dirs = find_cuda_dll_dirs(refresh=refresh)
    names = CUDA_DLL_NAMES if os.name == "nt" else CUDA_SO_NAMES
    cublas = _find_library(names, dirs)
    loaded: list = []

    if dirs:
        # ctranslate2 грузит «cublas64_12.dll» без пути — он ищет по PATH
        current = os.environ.get("PATH", "")
        extra = os.pathsep.join([d for d in dirs if d not in current.split(os.pathsep)])
        if extra:
            os.environ["PATH"] = extra + os.pathsep + current if current else extra
        if hasattr(os, "add_dll_directory"):      # Windows
            for folder in dirs:
                try:
                    _CUDA_HANDLES.append(os.add_dll_directory(folder))
                except Exception:
                    pass

    # предзагрузка по полному пути — самый надёжный способ на Windows
    if cublas:
        for folder in dirs:
            for name in names:
                path = os.path.join(folder, name)
                if not os.path.exists(path):
                    continue
                try:
                    if os.name == "nt":
                        _CUDA_HANDLES.append(ctypes.WinDLL(path))
                    else:
                        _CUDA_HANDLES.append(ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL))
                    loaded.append(name)
                except Exception as exc:  # noqa: BLE001
                    print(f"[stt] не удалось предзагрузить {name}: {exc}")

    _CUDA_STATE.update(dlls=dirs, cublas12=bool(cublas), loaded=loaded,
                       cublas13=find_cublas13() if not cublas else "")
    if loaded:
        print(f"[stt] найдены библиотеки CUDA 12: {', '.join(sorted(set(loaded)))}")
    return {"dirs": dirs, "cublas12": bool(cublas), "loaded": loaded,
            "cublas13": _CUDA_STATE["cublas13"]}

_CUDA_MARKERS = (
    "cublas", "cudnn", "cudart", "nvrtc", "cuda driver", "libcuda",
    "cuda_error", "no kernel image", "nvml", "gpu",
)


def is_cuda_library_error(exc: BaseException | str) -> bool:
    """Похоже ли сообщение на «CUDA есть, но работать на ней нельзя».

    Сюда попадают и отсутствующие библиотеки (cublas64_12.dll), и старый драйвер,
    и несовпадение поколений CUDA — во всех случаях нужно уйти на процессор.
    """
    text = str(exc or "").lower()
    if not text:
        return False
    mentions_cuda = "cuda" in text or any(
        marker in text for marker in ("cublas", "cudnn", "cudart", "nvrtc", "nvidia gpu"))
    if not mentions_cuda:
        return False
    library_missing = ("not found" in text or "cannot be loaded" in text or "could not load" in text
                       or "could not be loaded" in text or "dll" in text or ".so" in text
                       or "no such file" in text or "cannot open" in text)
    other_cuda_problems = ("insufficient" in text or "driver version" in text
                           or "cuda failed" in text or "no cuda-capable device" in text
                           or "no kernel image" in text or "out of memory" in text
                           or "cublas" in text or "cudnn" in text or "cudart" in text
                           or "nvrtc" in text)
    return library_missing or other_cuda_problems


def cuda_usable() -> tuple[bool, str]:
    """Можно ли реально считать на GPU (проверяет наличие библиотек CUDA).

    Один раз запоминаем результат: если библиотек нет, больше не пытаемся —
    иначе каждая фраза тратила бы время на неудачную попытку.
    """
    if _CUDA_STATE["usable"] is not None:
        return bool(_CUDA_STATE["usable"]), str(_CUDA_STATE["error"])
    usable, error = False, ""
    # библиотеки cuBLAS 12 из pip-пакетов подключаем до первой попытки
    prepare_cuda_dlls()
    try:
        import ctranslate2  # type: ignore

        if ctranslate2.get_cuda_device_count() <= 0:
            error = "CUDA-устройства не найдены"
        else:
            try:
                # эта проверка грузит cublas/cudnn — сразу видно, установлены ли они
                ctranslate2.get_supported_compute_types("cuda")
                usable = True
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}".strip()
    except Exception as exc:  # noqa: BLE001
        error = f"ctranslate2 недоступен: {exc}"
    if not usable and error:
        _CUDA_STATE["error"] = cuda_explanation(error)
    _CUDA_STATE["usable"] = usable
    if not usable and _CUDA_STATE["error"]:
        print(f"[stt] GPU недоступен, работаю на процессоре: {_CUDA_STATE['error']}")
    return usable, _CUDA_STATE["error"]


def cuda_explanation(error: str) -> str:
    """Человеческое объяснение, почему GPU не используется, и что с этим делать."""
    text = str(error or "")
    if not is_cuda_library_error(text):
        return text
    pieces = [f"CUDA не готова: {text}"]
    if _CUDA_STATE.get("cublas13") and not _CUDA_STATE.get("cublas12"):
        pieces.append(
            "В системе есть только cuBLAS от CUDA 13, а движок распознавания (ctranslate2) "
            "собран под CUDA 12 — им нужны разные DLL, и заменить одну другой нельзя. "
            "Поставьте библиотеки CUDA 12 рядом: pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 "
            "— Astra подключит их сама, CUDA 13 при этом остаётся на месте.")
    else:
        pieces.append(
            "Поставьте библиотеки CUDA 12: pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 "
            "(Astra сама пропишет их в PATH при запуске).")
    return " ".join(pieces)


def mark_cuda_broken(reason: str) -> None:
    """Запомнить, что CUDA не работает, чтобы не пробовать снова."""
    _CUDA_STATE.update(usable=False, error=str(reason or "CUDA недоступна"))
    print(f"[stt] CUDA отключена до перезапуска: {reason}")


def cuda_state() -> dict:
    return {"usable": _CUDA_STATE["usable"], "error": _CUDA_STATE["error"]}


def reset_cuda_state() -> None:
    """Сбросить кэш проверки (например, после установки библиотек CUDA)."""
    _CUDA_STATE.update(usable=None, error="")


class WhisperSTT:
    """Ленивая загрузка модели faster-whisper + потокобезопасная транскрипция."""

    def __init__(self) -> None:
        self._model = None
        # RLock: из transcribe() можно повторно вызвать load() при откате с CUDA на CPU
        self._lock = threading.RLock()
        self._loading = False
        self._loaded_key = ""
        self.last_error = ""
        # фактически использованное устройство и причина отката на CPU
        self.device = ""
        self.compute_type = ""
        self.fallback_note = ""

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def model_name(self) -> str:
        return self._loaded_key.split("|")[0] if self._loaded_key else ""

    @property
    def loading(self) -> bool:
        return self._loading

    def status(self) -> dict:
        """Короткая сводка для интерфейса: что реально используется."""
        return {
            "ready": self.ready,
            "loading": self._loading,
            "model": self.model_name,
            "device": self.device or "—",
            "compute_type": self.compute_type or "—",
            "on_cpu": bool(self.device and self.device != "cuda"),
            "fallback_note": self.fallback_note,
            "cuda": cuda_state(),
            "error": self.last_error or "",
        }

    def unload(self) -> None:
        """Освободить память перед загрузкой другой модели."""
        with self._lock:
            self._model = None
            self._loaded_key = ""

    def load(self, model_size: str = "large-v3", device: str = "auto", compute_type: str = "auto") -> None:
        key = f"{model_size}|{device}|{compute_type}"
        with self._lock:
            if self._model is not None and key == self._loaded_key:
                return
            try:
                from faster_whisper import WhisperModel  # type: ignore
            except Exception as exc:  # noqa: BLE001
                raise STTError(
                    "Модуль faster-whisper не установлен. Выполните: pip install faster-whisper "
                    "(при первом запуске модель скачается автоматически)."
                ) from exc

            resolved_device = device
            note = ""
            if device in ("auto", "cuda"):
                usable, reason = cuda_usable()
                if usable:
                    resolved_device = "cuda"
                else:
                    # видеокарта есть, но библиотек CUDA 12 нет — считаем на процессоре
                    resolved_device = "cpu"
                    note = f"нет библиотек CUDA ({reason}); считаю на процессоре"
            resolved_compute = compute_type
            if compute_type == "auto":
                resolved_compute = "float16" if resolved_device == "cuda" else "int8"

            def build(dev: str, compute: str):
                return WhisperModel(model_size, device=dev, compute_type=compute, download_root=None)

            self._loading = True
            self.last_error = ""
            try:
                try:
                    self._model = build(resolved_device, resolved_compute)
                except Exception as exc:  # noqa: BLE001
                    # вторая попытка на процессоре: CUDA есть, но не работает
                    if resolved_device == "cuda" and is_cuda_library_error(exc):
                        mark_cuda_broken(str(exc))
                        resolved_device, resolved_compute = "cpu", "int8"
                        note = f"CUDA не заработала ({exc}); считаю на процессоре"
                        self._model = build(resolved_device, resolved_compute)
                    else:
                        raise
                self._loaded_key = f"{model_size}|{resolved_device}|{resolved_compute}"
                self.device = resolved_device
                self.compute_type = resolved_compute
                self.fallback_note = note
                if note:
                    print(f"[stt] {note}")
            except Exception as exc:  # noqa: BLE001
                self._model = None
                self.device = ""
                self.compute_type = ""
                self.last_error = str(exc)
                raise STTError(f"Не удалось загрузить модель Whisper «{model_size}»: {exc}") from exc
            finally:
                self._loading = False

    def transcribe(self, audio: np.ndarray, language: str = "ru", model_size: str = "large-v3",
                   device: str = "auto", compute_type: str = "auto",
                   on_progress: Optional[Callable[[str], None]] = None,
                   beam_size: int = 5, initial_prompt: str = "") -> str:
        if audio.size < SAMPLE_RATE * 0.25:
            return ""
        if not self.ready or not self._loaded_key.startswith(f"{model_size}|"):
            if on_progress:
                on_progress("Загружаю модель распознавания речи…")
            self.load(model_size, device, compute_type)
        audio = normalize_audio(audio)
        if on_progress:
            on_progress("Распознаю речь…")
        started = time.time()
        options = dict(
            language=None if language in ("", "auto") else language,
            beam_size=max(1, int(beam_size or 5)),
            # подсказка про домен и язык: короткие команды распознаются точнее
            initial_prompt=(initial_prompt or None),
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 350, "speech_pad_ms": 120},
            condition_on_previous_text=False,
            no_speech_threshold=0.5,
            temperature=[0.0, 0.2, 0.4],
        )
        with self._lock:
            model = self._model
            if model is None:
                raise STTError("Модель распознавания не загружена")
            try:
                segments, info = model.transcribe(audio, **options)
                text = " ".join(seg.text.strip() for seg in segments if seg.text and seg.text.strip())
            except Exception as exc:  # noqa: BLE001
                # ctranslate2 грузит cublas лениво: ошибка всплывает уже на первой фразе
                if not is_cuda_library_error(exc) or self.device != "cuda":
                    raise
                mark_cuda_broken(str(exc))
                self.fallback_note = f"CUDA не заработала ({exc}); считаю на процессоре"
                self._model = None
                self._loaded_key = ""
                if on_progress:
                    on_progress("CUDA недоступна — переключаюсь на процессор…")
                self.load(model_size, "cpu", "int8")
                segments, info = self._model.transcribe(audio, **options)
                text = " ".join(seg.text.strip() for seg in segments if seg.text and seg.text.strip())
        print(f"[stt] {time.time() - started:.2f} c ({self.device or '?'}): {text!r}")
        return text.strip()


def transcribe_google(audio: np.ndarray, language: str = "ru-RU") -> str:
    """Резервный онлайн-вариант через SpeechRecognition (если установлен)."""
    try:
        import speech_recognition as sr  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise STTError("Резервный движок недоступен: установите SpeechRecognition и PyAudio.") from exc
    pcm16 = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm16)
    buf.seek(0)
    recognizer = sr.Recognizer()
    with sr.AudioFile(buf) as source:
        data = recognizer.record(source)
    try:
        return recognizer.recognize_google(data, language=language).strip()
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as exc:
        raise STTError(f"Google Speech недоступен: {exc}") from exc


whisper_stt = WhisperSTT()
