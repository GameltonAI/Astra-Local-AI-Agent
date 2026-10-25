"""Утилиты обработки текста (общие для сервера и тестов)."""
from __future__ import annotations

import re

CODE_BLOCK = re.compile(r"```.*?```", re.S)
INLINE_CODE = re.compile(r"`([^`]*)`")
MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
MD_EMPHASIS = re.compile(r"[*_#>]+")
EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]+")
BULLETS = re.compile(r"^\s*(?:[-–—•*]|\d+[.)])\s+", re.M)



def clean_for_speech(text: str) -> str:
    """Убираем markdown и лишние символы, чтобы озвучка звучала естественно."""
    text = CODE_BLOCK.sub(" (пример кода пропущен) ", text or "")
    text = INLINE_CODE.sub(r"\1", text)
    text = MD_LINK.sub(r"\1", text)
    text = MD_EMPHASIS.sub("", text)
    text = EMOJI.sub("", text)
    text = BULLETS.sub("", text)
    text = re.sub(r"https?://\S+", "ссылка", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


