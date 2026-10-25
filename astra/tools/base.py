"""Базовые примитивы системы инструментов."""
from __future__ import annotations

import asyncio
import inspect
import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

MAX_RESULT_CHARS = 6000


@dataclass
class Param:
    name: str
    type: str = "str"          # str | int | float | bool | list[str]
    description: str = ""
    required: bool = True
    default: Any = None
    enum: Optional[List[str]] = None


@dataclass
class Tool:
    name: str
    summary: str
    params: List[Param]
    func: Callable[..., Any]
    danger: str = "safe"       # safe | confirm | blocked
    category: str = "misc"
    examples: List[str] = field(default_factory=list)

    # --- представление для промпта -------------------------------------
    def signature(self) -> str:
        parts = []
        for p in self.params:
            piece = f"{p.name}: {p.type}"
            if not p.required:
                piece += "?" if p.default is None else f" = {json.dumps(p.default, ensure_ascii=False)}"
            if p.enum:
                piece += " [" + "|".join(p.enum) + "]"
            parts.append(piece)
        return f"{self.name}({', '.join(parts)})"

    def help_line(self) -> str:
        line = f"- {self.signature()} — {self.summary}"
        if self.examples:
            line += "  Пример: " + "; ".join(self.examples[:2])
        return line

    def to_public(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "signature": self.signature(),
            "category": self.category,
            "danger": self.danger,
            "params": [
                {"name": p.name, "type": p.type, "description": p.description,
                 "required": p.required, "default": p.default, "enum": p.enum}
                for p in self.params
            ],
        }


class ToolError(RuntimeError):
    """Ошибка выполнения инструмента с человекочитаемым текстом."""


def _coerce(value: Any, param: Param) -> Any:
    if value is None:
        return param.default
    try:
        if param.type == "int":
            if isinstance(value, bool):
                return int(value)
            return int(float(str(value).replace(",", ".")))
        if param.type == "float":
            return float(str(value).replace(",", "."))
        if param.type == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "да", "on", "истина")
        if param.type == "list[str]":
            if isinstance(value, (list, tuple)):
                return [str(v) for v in value]
            if isinstance(value, str):
                text = value.strip()
                if text.startswith("["):
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, list):
                            return [str(v) for v in parsed]
                    except json.JSONDecodeError:
                        pass
                return [p.strip() for p in text.replace("+", ",").split(",") if p.strip()]
            return [str(value)]
        if param.type == "str":
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False)
            return str(value)
    except (TypeError, ValueError):
        raise ToolError(f"Не удалось привести параметр «{param.name}» к типу {param.type}: {value!r}")
    return value


class ToolRegistry:
    def __init__(self, tools: Optional[Iterable[Tool]] = None):
        self._tools: Dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get((name or "").strip())

    def all(self) -> List[Tool]:
        return list(self._tools.values())

    def by_category(self) -> Dict[str, List[Tool]]:
        out: Dict[str, List[Tool]] = {}
        for tool in self._tools.values():
            out.setdefault(tool.category, []).append(tool)
        return out

    def catalog(self) -> str:
        """Каталог инструментов для системного промпта."""
        titles = {
            "apps": "Запуск программ и открытие ссылок/файлов",
            "windows": "Работа в конкретном окне (браузер, программы)",
            "vision": "Зрение: разметка окна, клик по метке, проверка результата",
            "input": "Клавиатура и мышь (вся система)",
            "system": "Система, звук, питание, буфер обмена, скриншоты",
            "files": "Файлы и папки",
            "shell": "Командная строка",
            "web": "Интернет",
        }
        blocks: List[str] = []
        grouped = self.by_category()
        for cat, tools in grouped.items():
            blocks.append(f"\n## {titles.get(cat, cat)}")
            for tool in sorted(tools, key=lambda t: t.name):
                blocks.append(tool.help_line())
        return "\n".join(blocks)

    def public_catalog(self) -> List[Dict[str, Any]]:
        return [t.to_public() for t in sorted(self._tools.values(), key=lambda x: (x.category, x.name))]

    # --- исполнение -----------------------------------------------------
    def prepare(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        tool = self.get(name)
        if not tool:
            raise ToolError(f"Инструмент «{name}» не существует. Доступные: "
                            + ", ".join(sorted(self._tools)[:30]))
        args = dict(args or {})
        known = {p.name: p for p in tool.params}
        unknown = [k for k in args if k not in known]
        if unknown:
            raise ToolError(f"У инструмента «{name}» нет параметров: {', '.join(unknown)}. "
                            f"Допустимые: {', '.join(known)}")
        prepared: Dict[str, Any] = {}
        missing: List[str] = []
        for param in tool.params:
            if param.name in args and args[param.name] != "":
                prepared[param.name] = _coerce(args[param.name], param)
            elif param.required and param.default is None:
                missing.append(param.name)
            else:
                prepared[param.name] = param.default
        if missing:
            raise ToolError(f"Не хватает обязательных параметров для «{name}»: {', '.join(missing)}. "
                            f"Сигнатура: {tool.signature()}")
        return {"tool": tool, "kwargs": prepared}

    async def execute(self, name: str, args: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
        started = time.time()
        try:
            prepared = self.prepare(name, args)
        except ToolError as exc:
            return {"ok": False, "tool": name, "args": args, "error": str(exc), "duration_ms": 0}

        tool: Tool = prepared["tool"]
        kwargs = prepared["kwargs"]

        def _call() -> Any:
            result = tool.func(**kwargs)
            if inspect.iscoroutine(result):
                raise ToolError("Инструмент вернул корутину — это внутренняя ошибка")
            return result

        try:
            result = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "tool": name, "args": args,
                    "error": f"Инструмент не завершился за {int(timeout)} с", "duration_ms": int((time.time() - started) * 1000)}
        except ToolError as exc:
            return {"ok": False, "tool": name, "args": args, "error": str(exc),
                    "duration_ms": int((time.time() - started) * 1000)}
        except Exception as exc:  # noqa: BLE001
            detail = traceback.format_exc(limit=3)
            print(f"[tool:{name}] {detail}")
            return {"ok": False, "tool": name, "args": args,
                    "error": f"{type(exc).__name__}: {exc}", "duration_ms": int((time.time() - started) * 1000)}

        text, artifact = _split_result(result)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n… (обрезано, всего {len(text)} символов)"
        return {
            "ok": True,
            "tool": name,
            "args": args,
            "result": text,
            "artifact": artifact,
            "duration_ms": int((time.time() - started) * 1000),
        }


def _split_result(result: Any) -> tuple[str, Optional[Dict[str, Any]]]:
    artifact = None
    if isinstance(result, dict) and ("text" in result or "artifact" in result):
        artifact = result.get("artifact")
        text = result.get("text", "")
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        return text, artifact
    if isinstance(result, str):
        return result, None
    return json.dumps(result, ensure_ascii=False, default=str), None
