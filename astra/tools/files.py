"""Инструменты работы с файлами и папками."""
from __future__ import annotations

import fnmatch
import os
import shutil
from pathlib import Path
from typing import List

from .base import Param, Tool

MAX_READ_BYTES = 200_000


def _resolve(path: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()
    return p


def list_dir(path: str = "~") -> str:
    p = _resolve(path)
    if not p.exists():
        raise FileNotFoundError(f"Путь не существует: {p}")
    if not p.is_dir():
        return f"{p} — это файл, размер {p.stat().st_size} байт"
    lines: List[str] = [f"Содержимое {p}:"]
    try:
        entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    except PermissionError:
        raise PermissionError(f"Нет доступа к папке: {p}")
    for entry in entries[:300]:
        try:
            if entry.is_dir():
                lines.append(f"[папка] {entry.name}/")
            else:
                lines.append(f"[файл]  {entry.name} ({entry.stat().st_size} б)")
        except OSError:
            lines.append(f"[?]     {entry.name}")
    if len(entries) > 300:
        lines.append(f"… и ещё {len(entries) - 300} объектов")
    return "\n".join(lines)


def read_text_file(path: str) -> str:
    p = _resolve(path)
    if not p.exists():
        raise FileNotFoundError(f"Файл не найден: {p}")
    if p.is_dir():
        raise IsADirectoryError(f"{p} — это папка")
    size = p.stat().st_size
    if size > 20_000_000:
        raise ValueError(f"Файл слишком большой ({size} байт). Прочитайте его частями через командную строку.")
    raw = p.read_bytes()[:MAX_READ_BYTES]
    for encoding in ("utf-8", "cp1251", "utf-16"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Не удалось определить кодировку файла (похоже, это бинарный файл)")
    if size > MAX_READ_BYTES:
        text += f"\n… (прочитано {MAX_READ_BYTES} из {size} байт)"
    return f"{p}:\n\n{text}"


def write_text_file(path: str, content: str, append: bool = False) -> str:
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with open(p, mode, encoding="utf-8") as fh:
        fh.write(content or "")
    action = "дописан" if append else "записан"
    return f"Файл {action}: {p} ({len(content or '')} символов)"


def create_dir(path: str) -> str:
    p = _resolve(path)
    p.mkdir(parents=True, exist_ok=True)
    return f"Папка создана: {p}"


def delete_path(path: str, recursive: bool = False) -> str:
    p = _resolve(path)
    if not p.exists():
        raise FileNotFoundError(f"Путь не найден: {p}")
    if p.is_dir():
        if not recursive:
            try:
                p.rmdir()
            except OSError:
                raise ValueError(f"Папка не пуста: {p}. Передайте recursive=true, чтобы удалить со всем содержимым.")
            return f"Пустая папка удалена: {p}"
        if len(p.parts) <= 3:
            raise ValueError(f"Отказ: удаление корневой папки {p} заблокировано")
        shutil.rmtree(p)
        return f"Папка удалена со всем содержимым: {p}"
    p.unlink()
    return f"Файл удалён: {p}"


def search_files(pattern: str, directory: str = "~", max_results: int = 40) -> str:
    root = _resolve(directory)
    if not root.exists():
        raise FileNotFoundError(f"Папка не найдена: {root}")
    needle = (pattern or "").strip()
    if not needle:
        raise ValueError("Пустой шаблон поиска")
    glob_pat = needle if any(ch in needle for ch in "*?[]") else f"*{needle}*"
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "$"))][:60]
        for fname in filenames:
            if fnmatch.fnmatch(fname.lower(), glob_pat.lower()):
                found.append(os.path.join(dirpath, fname))
                if len(found) >= max_results:
                    break
        if len(found) >= max_results:
            break
    if not found:
        return f"Ничего не найдено по шаблону «{glob_pat}» в {root}"
    return f"Найдено {len(found)}:\n" + "\n".join(f"- {f}" for f in found)


TOOLS = [
    Tool(
        name="list_dir",
        summary="Показать содержимое папки.",
        params=[Param("path", "str", "Путь к папке (можно ~)", False, "~")],
        func=list_dir,
        category="files",
        examples=['{"path": "~/Downloads"}'],
    ),
    Tool(
        name="read_text_file",
        summary="Прочитать текстовый файл (txt, md, json, py, csv, лог и т.п.).",
        params=[Param("path", "str", "Путь к файлу", True)],
        func=read_text_file,
        category="files",
    ),
    Tool(
        name="write_text_file",
        summary="Создать или перезаписать текстовый файл.",
        params=[
            Param("path", "str", "Путь к файлу", True),
            Param("content", "str", "Содержимое", True),
            Param("append", "bool", "Дописать в конец, а не перезаписать", False, False),
        ],
        func=write_text_file,
        category="files",
        danger="confirm",
    ),
    Tool(
        name="create_dir",
        summary="Создать папку (вместе с родительскими).",
        params=[Param("path", "str", "Путь", True)],
        func=create_dir,
        category="files",
    ),
    Tool(
        name="delete_path",
        summary="Удалить файл или папку. Для непустой папки нужен recursive=true.",
        params=[
            Param("path", "str", "Что удалить", True),
            Param("recursive", "bool", "Удалить папку со всем содержимым", False, False),
        ],
        func=delete_path,
        category="files",
        danger="confirm",
    ),
    Tool(
        name="search_files",
        summary="Найти файлы по имени/шаблону в папке (рекурсивно).",
        params=[
            Param("pattern", "str", "Часть имени или шаблон (*.pdf)", True),
            Param("directory", "str", "Где искать", False, "~"),
            Param("max_results", "int", "Максимум результатов", False, 40),
        ],
        func=search_files,
        category="files",
    ),
]
