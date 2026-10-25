"""Зрение агента: передача скриншотов в LM Studio (multimodal) с фолбэком на текст.

LM Studio принимает изображения в формате OpenAI:
    {"role": "user", "content": [
        {"type": "text", "text": "..."},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}]}

Не всякая локальная модель умеет смотреть картинки. Поэтому:
• режим `auto` — пробуем передавать изображения, а если сервер отвечает ошибкой
  «image/vision не поддерживается», запоминаем это и дальше работаем по тексту;
• режим `on` — передавать всегда (для vision-моделей);
• режим `off` — никогда.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import config

MAX_IMAGE_BYTES = 1_800_000          # ~1.8 МБ на изображение после сжатия
VISION_ERROR_MARKERS = (
    "image", "vision", "multimodal", "multi-modal", "image_url", "visual",
    "does not support", "not supported", "unsupported", "invalid content type",
    "не поддерж", "изображени",
)


class VisionState:
    """Поддерживает ли текущая модель изображения (определяется по факту)."""

    def __init__(self) -> None:
        self.supported: Optional[bool] = None   # None — ещё не проверяли
        self.last_error: str = ""
        self.sent_count: int = 0

    # --- режим из настроек ------------------------------------------------
    @property
    def mode(self) -> str:
        mode = str(config.get("lmstudio.vision") or "auto").lower()
        return mode if mode in ("auto", "on", "off") else "auto"

    def enabled(self) -> bool:
        if self.mode == "off":
            return False
        if self.mode == "on":
            return True
        return self.supported is not False   # auto: пробуем, пока не доказано обратное

    def mark_sent(self) -> None:
        self.supported = True
        self.last_error = ""
        self.sent_count += 1

    def mark_unsupported(self, error: str) -> None:
        self.supported = False
        self.last_error = str(error)[:400]

    def reset(self) -> None:
        self.supported = None
        self.last_error = ""
        self.sent_count = 0

    def status(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "enabled": self.enabled(),
            "supported": self.supported,
            "last_error": self.last_error,
            "sent_count": self.sent_count,
        }


vision_state = VisionState()


# ----------------------------------------------------------------------
#  Кодирование изображений
# ----------------------------------------------------------------------
def image_to_part(path: str | Path, max_px: Optional[int] = None,
                  quality: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Файл изображения → content-part вида image_url (JPEG, уменьшенный)."""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    path = Path(path)
    if not path.exists():
        return None
    limit = int(max_px or config.get("lmstudio.vision_max_px") or 1024)
    q = int(quality or config.get("lmstudio.vision_quality") or 80)
    try:
        with Image.open(path) as img:
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            if max(img.size) > limit:
                img.thumbnail((limit, limit))
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=q, optimize=True)
            data = buffer.getvalue()
            width, height = img.size
    except Exception as exc:  # noqa: BLE001
        print(f"[vision] не удалось подготовить {path}: {exc}")
        return None
    if len(data) > MAX_IMAGE_BYTES:
        return image_to_part(path, max_px=int(limit * 0.75), quality=max(45, q - 15))
    return {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")},
        "meta": {"path": str(path), "name": path.name, "width": width, "height": height,
                 "bytes": len(data)},
    }


def artifact_to_part(artifact: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Готовый artifact инструмента (data_uri) → content-part."""
    if not artifact or not isinstance(artifact, dict):
        return None
    data_uri = artifact.get("data_uri")
    if not data_uri and artifact.get("path"):
        return image_to_part(artifact["path"])
    if not data_uri:
        return None
    return {
        "type": "image_url",
        "image_url": {"url": data_uri},
        "meta": {"path": artifact.get("path", ""), "name": artifact.get("name", "снимок")},
    }


def part_caption(part: Dict[str, Any]) -> str:
    meta = part.get("meta") or {}
    name = meta.get("name") or "снимок"
    size = ""
    if meta.get("width") and meta.get("height"):
        size = f", {meta['width']}×{meta['height']}"
    return f"{name}{size}"


# ----------------------------------------------------------------------
#  Сборка сообщений с изображениями
# ----------------------------------------------------------------------
def has_images(messages: List[Dict[str, Any]]) -> bool:
    for message in messages or []:
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "image_url" for part in content
        ):
            return True
    return False


def strip_images(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Убрать изображения, оставив текст (для фолбэка на текстовые модели)."""
    result: List[Dict[str, Any]] = []
    for message in messages or []:
        content = message.get("content")
        if isinstance(content, list):
            texts = [part.get("text", "") for part in content
                     if isinstance(part, dict) and part.get("type") == "text"]
            images = sum(1 for part in content if isinstance(part, dict) and part.get("type") == "image_url")
            text = "\n".join(t for t in texts if t)
            if images:
                text += f"\n(изображений приложено: {images}, но текущая модель не умеет смотреть картинки)"
            result.append({**message, "content": text})
        else:
            result.append(message)
    return result


def attach_images(messages: List[Dict[str, Any]], images: List[Dict[str, Any]],
                  caption: str = "") -> List[Dict[str, Any]]:
    """Приложить изображения к последнему сообщению пользователя."""
    if not images:
        return messages
    msgs = [dict(m) for m in messages]
    for index in range(len(msgs) - 1, -1, -1):
        if msgs[index].get("role") != "user":
            continue
        content = msgs[index].get("content")
        if isinstance(content, list):
            parts = list(content)
            text = ""
        else:
            parts = []
            text = str(content or "")
        if caption:
            text = (text + "\n\n" + caption).strip() if text else caption
        new_parts: List[Dict[str, Any]] = [{"type": "text", "text": text}]
        new_parts.extend(images[: int(config.get("lmstudio.vision_max_images") or 2)])
        msgs[index] = {**msgs[index], "content": new_parts}
        break
    return msgs


def looks_like_vision_error(error: str) -> bool:
    text = (error or "").lower()
    return any(marker in text for marker in VISION_ERROR_MARKERS)


# ----------------------------------------------------------------------
#  Проверка «умеет ли модель видеть»
# ----------------------------------------------------------------------
def build_test_image() -> Optional[Dict[str, Any]]:
    """Маленькая картинка с текстом для проверки зрения."""
    try:
        from PIL import Image, ImageDraw  # type: ignore
    except ImportError:
        return None
    img = Image.new("RGB", (320, 160), (18, 20, 30))
    draw = ImageDraw.Draw(img)
    draw.rectangle([12, 12, 308, 148], outline=(124, 92, 255), width=4)
    draw.ellipse([36, 46, 116, 126], fill=(244, 114, 182))
    draw.text((136, 62), "ASTRA-42", fill=(240, 244, 255))
    draw.text((136, 92), "test image", fill=(140, 200, 240))
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=85)
    data_uri = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": data_uri},
            "meta": {"name": "vision-test.jpg", "width": 320, "height": 160}}
