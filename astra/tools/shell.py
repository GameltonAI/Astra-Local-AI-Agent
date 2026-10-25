"""Инструмент: командная строка."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import List

from .base import Param, Tool

# Категорически запрещённые конструкции — защита от разрушительных команд.
DENY_PATTERNS = [
    r"rm\s+-rf\s+/(\s|$)",
    r"rm\s+-rf\s+/\*",
    r"del\s+/[sfq]\s+[a-z]:\\\*",
    r"format\s+[a-z]:",
    r"mkfs\.",
    r":\(\)\s*\{.*\};\s*:",
    r"dd\s+if=.*of=/dev/[sh]d",
    r"diskpart\s+/s",
    r"cipher\s+/w",
    r"bcdedit\s+/delete",
    r"vssadmin\s+delete\s+shadows",
    r"reg\s+delete\s+hklm",
    r"net\s+user\s+.*\s+/delete",
    r"schtasks\s+/delete\s+/tn\s+",
]
_DENY = [re.compile(p, re.IGNORECASE) for p in DENY_PATTERNS]

MAX_OUTPUT = 5000


def run_command(command: str, cwd: str = "", timeout: int = 60) -> str:
    command = (command or "").strip()
    if not command:
        raise ValueError("Пустая команда")
    for rx in _DENY:
        if rx.search(command):
            raise PermissionError(
                "Команда заблокирована встроенной защитой как потенциально разрушительная. "
                "Если это действительно нужно, выполните её вручную."
            )
    timeout = max(1, min(int(timeout or 60), 600))
    workdir = os.path.expanduser(cwd) if cwd else None
    if workdir and not os.path.isdir(workdir):
        raise FileNotFoundError(f"Рабочая папка не найдена: {workdir}")

    if sys.platform.startswith("win"):
        shell_cmd: List[str] = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        shell_cmd = ["/bin/bash", "-lc", command]

    try:
        proc = subprocess.run(shell_cmd, cwd=workdir, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"Команда не завершилась за {timeout} с и была прервана")

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parts: List[str] = [f"Код возврата: {proc.returncode}"]
    if out:
        parts.append("Вывод:\n" + (out[-MAX_OUTPUT:] if len(out) > MAX_OUTPUT else out))
    if err:
        parts.append("Ошибки:\n" + (err[-2000:] if len(err) > 2000 else err))
    if not out and not err:
        parts.append("Команда выполнена без вывода.")
    return "\n\n".join(parts)


TOOLS = [
    Tool(
        name="run_command",
        summary="Выполнить команду в PowerShell (Windows) или bash (Linux/macOS). "
                "Используй для сложных задач: установка пакетов, git, работа с архивами, системные запросы.",
        params=[
            Param("command", "str", "Команда целиком", True),
            Param("cwd", "str", "Рабочая папка", False, ""),
            Param("timeout", "int", "Таймаут в секундах", False, 60),
        ],
        func=run_command,
        category="shell",
        danger="confirm",
        examples=['{"command": "Get-ChildItem $HOME/Downloads | Select-Object -First 5"}'],
    ),
]
