"""Клиент LM Studio (OpenAI-совместимый API) + разбор решений модели.

Адрес сервера выбирается автоматически (см. astra/endpoints.py): сначала
локальная сеть (например, http://192.168.0.32:1234), затем этот компьютер.
Если активный сервер пропал во время запроса, клиент один раз повторяет
запрос уже на резервном адресе.
"""
from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from .config import config
from .endpoints import EndpointUnavailable, endpoints


class LLMError(RuntimeError):
    """Ошибка взаимодействия с LM Studio."""


class LMStudioClient:
    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    def _timeout(self) -> httpx.Timeout:
        total = float(config.get("lmstudio.timeout") or 180)
        return httpx.Timeout(total, connect=8.0)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = config.get("lmstudio.api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(headers=self._headers(), timeout=self._timeout())
        return self._client

    def _reset_client(self) -> None:
        self._client = None

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # --- выбор сервера ---------------------------------------------------
    async def _resolve_base(self, force: bool = False) -> str:
        try:
            return await endpoints.active_url(force=force)
        except EndpointUnavailable as exc:
            raise LLMError(str(exc)) from exc

    # --- модели ---------------------------------------------------------
    async def list_models(self, base_url: Optional[str] = None) -> List[Dict[str, Any]]:
        """Список моделей: с активного сервера или с явно указанного адреса."""
        if base_url:
            probe = await endpoints.probe(base_url)
            if not probe["ok"]:
                raise LLMError(f"{probe['url']}: {probe['error']}")
            return probe["models"]
        _, models = await endpoints.active_models()
        return models

    async def probe(self, base_url: str) -> Dict[str, Any]:
        return await endpoints.probe(base_url)

    async def ping(self, base_url: Optional[str] = None) -> Dict[str, Any]:
        if base_url:
            probe = await endpoints.probe(base_url)
            return {"ok": probe["ok"], "models": probe["models"],
                    "base_url": probe["url"], "error": probe["error"]}
        result = await endpoints.resolve(force=True)
        return {
            "ok": result["ok"],
            "models": result["models"],
            "base_url": result["url"],
            "error": result["error"],
            "kind": result["kind"],
        }

    # --- чат ------------------------------------------------------------
    def _payload(self, messages: List[Dict[str, str]], stream: bool,
                 temperature: Optional[float], max_tokens: Optional[int]) -> Dict[str, Any]:
        model = config.get("lmstudio.model")
        if not model:
            raise LLMError("Модель не выбрана. Откройте настройки и выберите модель LM Studio.")
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "temperature": float(temperature if temperature is not None else config.get("lmstudio.temperature") or 0.35),
        }
        limit = int(max_tokens if max_tokens is not None else config.get("lmstudio.max_tokens") or 1400)
        if limit > 0:
            body["max_tokens"] = limit
        extra = config.get("lmstudio.extra_body") or {}
        if isinstance(extra, dict):
            body.update({k: v for k, v in extra.items() if k not in body})
        return body

    async def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        body = self._payload(messages, False, temperature, max_tokens)
        last_error: Optional[Exception] = None

        for attempt in (1, 2):
            base = await self._resolve_base(force=attempt == 2)
            url = base + "/chat/completions"
            try:
                resp = await self.client().post(url, json=body)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                endpoints.mark_failed(base)
                self._reset_client()
                last_error = exc
                if attempt == 1:
                    continue
                raise LLMError(
                    f"Сервер LM Studio ({base}) перестал отвечать: {type(exc).__name__}. "
                    "Запрос будет перенаправлен на резервный адрес при следующей попытке."
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMError(f"Ошибка сети при запросе к LM Studio: {exc}") from exc

            if resp.status_code == 404 and "model" in resp.text.lower():
                # модель пропала/переименована на этом сервере — синхронизируем список
                await endpoints.resolve(force=True)
                raise LLMError(
                    "LM Studio не нашёл выбранную модель. Список моделей обновлён — выберите модель заново."
                )
            if resp.status_code >= 400:
                raise LLMError(f"LM Studio вернул {resp.status_code}: {resp.text[:600]}")

            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
            if isinstance(content, list):  # некоторые модели отдают части
                content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
            usage = data.get("usage") or {}
            return {
                "content": content,
                "usage": usage,
                "finish_reason": choice.get("finish_reason"),
                "model": data.get("model") or body["model"],
                "endpoint": base,
            }

        raise LLMError(f"Не удалось получить ответ от LM Studio: {last_error}")

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        body = self._payload(messages, True, temperature, max_tokens)
        buffer: List[str] = []

        for attempt in (1, 2):
            base = await self._resolve_base(force=attempt == 2)
            url = base + "/chat/completions"
            retry = False
            try:
                async with self.client().stream("POST", url, json=body) as resp:
                    if resp.status_code >= 400:
                        detail = (await resp.aread()).decode("utf-8", "replace")[:600]
                        if resp.status_code == 404 and "model" in detail.lower():
                            await endpoints.resolve(force=True)
                            raise LLMError("LM Studio не нашёл выбранную модель — выберите её заново в настройках.")
                        raise LLMError(f"LM Studio вернул {resp.status_code}: {detail}")
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if not chunk or chunk == "[DONE]":
                            continue
                        try:
                            parsed = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        delta = ((parsed.get("choices") or [{}])[0].get("delta") or {})
                        piece = delta.get("content")
                        if piece:
                            buffer.append(piece)
                            yield piece
                return
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                endpoints.mark_failed(base)
                self._reset_client()
                if not buffer and attempt == 1:
                    retry = True
                else:
                    raise LLMError(
                        f"Соединение с LM Studio ({base}) прервалось: {type(exc).__name__}"
                    ) from exc
            except httpx.HTTPError as exc:
                raise LLMError(f"Ошибка сети при стриминге ответа: {exc}") from exc
            if not retry:
                return


# ======================================================================
#  Разбор решения модели
# ======================================================================
_FINAL_WORDS = {"final", "finish", "done", "answer", "none", "null", "готово", "ответ", "конец", ""}
_NAME_KEYS = ("tool", "tool_name", "action_name", "name", "function", "command", "action")
_ARGS_KEYS = ("args", "arguments", "parameters", "params", "input", "kwargs")
_THOUGHT_KEYS = ("thought", "reasoning", "think", "plan", "notes", "объяснение", "мысль")
# короткий человеческий статус действия: «Открываю блокнот», «Листаю»
_STATUS_KEYS = ("status", "say", "status_text", "label", "caption", "ui_status", "статус")


def _balanced_json_candidates(text: str) -> List[str]:
    out: List[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    out.append(text[start:i + 1])
                    start = -1
    return out


def parse_decision(text: str) -> Dict[str, Any]:
    """Достать из ответа модели решение: список действий + мысль."""
    raw = text or ""
    candidates: List[str] = []
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", raw, re.S | re.I):
        candidates.append(match.group(1).strip())
    candidates.extend(_balanced_json_candidates(raw))
    stripped = raw.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.insert(0, stripped)

    obj: Optional[Dict[str, Any]] = None
    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            obj = parsed
            break
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            obj = {"actions": parsed}
            break

    if obj is None:
        return {"actions": [], "thought": "", "raw": raw}

    thought = ""
    for key in _THOUGHT_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            thought = value.strip()
            break

    raw_actions: Any = None
    for key in ("actions", "tool_calls", "commands", "steps", "calls", "plan"):
        if isinstance(obj.get(key), list):
            raw_actions = obj[key]
            break

    action_name = obj.get("action") or obj.get("tool") or obj.get("tool_name")
    if raw_actions is None and isinstance(action_name, str) and action_name.strip().lower() not in _FINAL_WORDS:
        raw_actions = [obj]
    if raw_actions is None and isinstance(obj.get("function"), (str, dict)):
        raw_actions = [obj]

    actions: List[Dict[str, Any]] = []
    for item in raw_actions or []:
        if isinstance(item, str):
            actions.append({"tool": item.strip(), "args": {}})
            continue
        if not isinstance(item, dict):
            continue
        inner = item.get("function")
        if isinstance(inner, dict):
            item = {**item, **inner}
        elif isinstance(inner, str) and inner.strip() and not item.get("args"):
            item = {**item, "tool": inner}
        name = ""
        for key in _NAME_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
        if not name or name.lower() in _FINAL_WORDS:
            continue
        args: Dict[str, Any] = {}
        for key in _ARGS_KEYS:
            value = item.get(key)
            if isinstance(value, dict):
                args = dict(value)
                break
            if isinstance(value, str) and value.strip().startswith("{"):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        args = parsed
                        break
                except json.JSONDecodeError:
                    pass
        if not args:
            reserved = (set(_NAME_KEYS) | set(_ARGS_KEYS) | set(_THOUGHT_KEYS)
                        | set(_STATUS_KEYS) | {"function", "thought"})
            args = {k: v for k, v in item.items() if k not in reserved}
        status = ""
        for key in _STATUS_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                status = value.strip()
                break
        action = {"tool": name, "args": args}
        if status:
            action["status"] = status
        actions.append(action)

    return {"actions": actions, "thought": thought, "raw": raw}


llm = LMStudioClient()
