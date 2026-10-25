"""Голосовые возможности: запись, распознавание, синтез."""
from .recorder import AudioUnavailable, Recorder, SAMPLE_RATE, list_devices
from .stt import (STTError, WHISPER_MODELS, available_models, cuda_state, cuda_usable,
                  is_cuda_library_error, mark_cuda_broken, model_label, reset_cuda_state,
                  transcribe_google, whisper_stt)
from .tts import TTSError, tts

__all__ = [
    "Recorder", "AudioUnavailable", "SAMPLE_RATE", "list_devices",
    "whisper_stt", "STTError", "transcribe_google", "WHISPER_MODELS",
    "available_models", "model_label", "cuda_state", "cuda_usable",
    "is_cuda_library_error", "mark_cuda_broken", "reset_cuda_state",
    "tts", "TTSError",
]
