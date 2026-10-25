"""Агентный цикл: планирование → выполнение инструментов → ответ пользователю."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .config import config
from .llm import LLMError, llm, parse_decision
from .prompts import answer_prompt, planner_prompt, summarize_actions
from .status import status_for
from . import grounding, vision
from .tools import Tool, ToolError, get_registry

# действия в окне, после которых имеет смысл проверять, изменился ли экран
VERIFY_TOOLS = {
    "click_in_window", "click_window_mark", "type_in_window", "hotkey_in_window",
    "scroll_in_window", "scroll_until_end", "window_action", "open_in_browser",
}

EmitFn = Callable[[Dict[str, Any]], Awaitable[None]]
ConfirmFn = Callable[[Dict[str, Any]], Awaitable[bool]]


def needs_confirmation(tool: Tool) -> bool:
    """Нужно ли спрашивать разрешение перед выполнением инструмента."""
    if tool.danger != "confirm":
        return False
    allowed = config.get("safety.allowed_tools") or []
    if tool.name in allowed:
        return False
    if tool.category == "shell" and not config.get("safety.confirm_shell", True):
        return False
    if tool.name in ("power_action", "lock_screen") and not config.get("safety.confirm_power", True):
        return False
    if tool.name in ("write_text_file", "delete_path") and not config.get("safety.confirm_files", True):
        return False
    if tool.category == "input" and not config.get("safety.confirm_typing", True):
        return False
    return True


@dataclass
class AgentResult:
    text: str
    actions: List[Dict[str, Any]] = field(default_factory=list)
    model: str = ""
    steps: int = 0
    cancelled: bool = False
    error: str = ""


class Agent:
    """Один экземпляр на приложение; состояние сессии передаётся в run()."""

    def __init__(self, emit: EmitFn, confirm: Optional[ConfirmFn] = None) -> None:
        self.emit = emit
        self.confirm = confirm
        self._images: List[Dict[str, Any]] = []   # снимки для модели (в пределах одного запуска)
        self._vision_caption = ""
        self._verify_note = ""                    # результат визуальной проверки последнего действия

    # ------------------------------------------------------------------
    async def run(
        self,
        user_text: str,
        history: Optional[List[Dict[str, Any]]] = None,
        modality: str = "text",
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AgentResult:
        registry = get_registry()
        history = history or []
        max_steps = int(config.get("agent.max_steps") or 6)
        ctx_limit = int(config.get("lmstudio.context_messages") or 24)
        model_name = str(config.get("lmstudio.model") or "")
        cancel_event = cancel_event or asyncio.Event()

        self._images: List[Dict[str, Any]] = []       # снимки для модели (в пределах одного запуска)
        self._vision_caption = ""
        planner_messages = self._build_planner_messages(history, user_text, ctx_limit)
        executed: List[Dict[str, Any]] = []
        signature_counts: Dict[str, int] = {}
        steps_used = 0

        for step in range(1, max_steps + 1):
            if cancel_event.is_set():
                return AgentResult(text="", actions=executed, model=model_name,
                                   steps=steps_used, cancelled=True)
            steps_used = step
            await self.emit({"type": "state", "state": "thinking"})
            if step == 1:
                await self.emit_status("Думаю, как выполнить задачу", kind="planning")
            try:
                reply = await self._chat_with_vision(planner_messages, temperature=0.15, max_tokens=700)
            except LLMError as exc:
                return AgentResult(text="", actions=executed, model=model_name,
                                   steps=steps_used, error=str(exc))

            decision = parse_decision(reply["content"])
            thought = (decision.get("thought") or "").strip()
            if thought and config.get("agent.stream_thoughts", True):
                await self.emit({"type": "thought", "text": thought})

            actions = decision.get("actions") or []
            if not actions:
                break  # модель ответила текстом или решила, что действий не нужно

            stop_after = False
            for action in actions:
                if cancel_event.is_set():
                    break
                name = (action.get("tool") or "").strip()
                args = action.get("args") or {}
                action_id = uuid.uuid4().hex[:8]

                signature = name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
                signature_counts[signature] = signature_counts.get(signature, 0) + 1
                if signature_counts[signature] > 2:
                    result = {"ok": False, "tool": name, "args": args,
                              "error": "Это действие уже выполнялось несколько раз — пропущено, чтобы не зацикливаться."}
                    executed.append(result)
                    await self.emit({"type": "action_result", "id": action_id, **result})
                    continue

                tool = registry.get(name)
                if tool is None:
                    result = {"ok": False, "tool": name, "args": args,
                              "error": f"Инструмент «{name}» не существует."}
                    executed.append(result)
                    await self.emit({"type": "action_start", "id": action_id, "tool": name, "args": args,
                                     "danger": "safe"})
                    await self.emit({"type": "action_result", "id": action_id, **result})
                    continue

                # валидация аргументов до запроса подтверждения
                try:
                    prepared = registry.prepare(name, args)
                except ToolError as exc:
                    result = {"ok": False, "tool": name, "args": args, "error": str(exc)}
                    executed.append(result)
                    await self.emit({"type": "action_start", "id": action_id, "tool": name, "args": args,
                                     "danger": tool.danger})
                    await self.emit({"type": "action_result", "id": action_id, **result})
                    continue

                await self.emit_status(
                    status_for(name, prepared["kwargs"], action.get("status")),
                    kind="action", tool=name, action_id=action_id,
                )
                await self.emit({
                    "type": "action_start",
                    "id": action_id,
                    "tool": name,
                    "args": prepared["kwargs"],
                    "summary": tool.summary,
                    "danger": tool.danger,
                    "category": tool.category,
                    "status": status_for(name, prepared["kwargs"], action.get("status")),
                })

                approved = True
                if needs_confirmation(tool) and self.confirm is not None:
                    await self.emit({"type": "state", "state": "waiting_confirmation"})
                    approved = await self.confirm({
                        "id": action_id,
                        "tool": name,
                        "args": prepared["kwargs"],
                        "summary": tool.summary,
                        "danger": tool.danger,
                        "category": tool.category,
                    })

                if not approved:
                    result = {"ok": False, "tool": name, "args": args,
                              "error": "Пользователь отклонил выполнение действия.",
                              "denied": True}
                    executed.append(result)
                    await self.emit({"type": "action_result", "id": action_id, **result})
                    continue

                await self.emit({"type": "state", "state": "acting"})
                timeout = 300.0 if name == "run_command" else 120.0
                before_shot = await asyncio.to_thread(self._verify_snapshot, args) if name in VERIFY_TOOLS else ""
                result = await registry.execute(name, args, timeout=timeout)
                executed.append(result)
                self._verify_note = ""
                if result.get("ok") and before_shot:
                    # проверка результата: экран действительно изменился?
                    await self._verify_change(before_shot, args, executed)
                await self.emit({"type": "action_result", "id": action_id, **executed[-1]})

                snapshot_emitted = False
                if result.get("ok") and tool.category == "windows" and name != "screenshot_window":
                    snapshot_emitted = await self._attach_window_snapshot(
                        registry, args, executed, action_id, emit=not self._verify_note)
                if self._verify_note and not snapshot_emitted:
                    await self.emit({"type": "action_result", "id": action_id, **executed[-1]})

                if result.get("ok"):
                    stopped = await self._check_captcha(executed, args)
                    if stopped:
                        stop_after = True
                        break

                if result.get("ok") and tool.category == "apps":
                    await asyncio.sleep(0.6)  # даём программе время открыться
                if result.get("ok") and name == "power_action" and "cancel" not in str(args).lower():
                    stop_after = True
                    break

            if stop_after:
                break

            # сообщаем планировщику, что уже сделано
            planner_messages = self._build_planner_messages(history, user_text, ctx_limit, executed)

        # ---------------- финальный ответ пользователю ----------------
        if cancel_event.is_set():
            return AgentResult(text="", actions=executed, model=model_name,
                               steps=steps_used, cancelled=True)

        await self.emit({"type": "state", "state": "thinking"})
        await self.emit_status("Готовлю ответ", kind="answer")
        answer_messages = self._build_answer_messages(history, user_text, modality, ctx_limit, executed)
        answer_messages = self._inject_images(answer_messages)
        text_parts: List[str] = []
        stream_error = ""
        try:
            async for chunk in llm.chat_stream(answer_messages):
                if cancel_event.is_set():
                    break
                text_parts.append(chunk)
                await self.emit({"type": "token", "text": chunk})
        except LLMError as exc:
            if vision.has_images(answer_messages) and vision.looks_like_vision_error(str(exc)):
                await self._disable_vision(str(exc))
                answer_messages = vision.strip_images(answer_messages)
                try:
                    async for chunk in llm.chat_stream(answer_messages):
                        if cancel_event.is_set():
                            break
                        text_parts.append(chunk)
                        await self.emit({"type": "token", "text": chunk})
                except LLMError as inner:
                    stream_error = str(inner)
            else:
                stream_error = str(exc)

        text = "".join(text_parts).strip()
        if not text and not cancel_event.is_set():
            # резерв: не-стриминговый запрос (некоторые сборки/модели отдают пустой поток)
            try:
                reply = await llm.chat(answer_messages, max_tokens=900)
                text = (reply.get("content") or "").strip()
            except LLMError as exc:
                return AgentResult(text="", actions=executed, model=model_name,
                                   steps=steps_used, error=str(exc) or stream_error)
            if text:
                await self.emit({"type": "token", "text": text})

        if not text:
            text = "Готово." if any(a.get("ok") for a in executed) else "Не удалось получить ответ от модели."

        return AgentResult(text=text, actions=executed, model=model_name, steps=steps_used)

    async def emit_status(self, text: str, kind: str = "action", tool: str = "",
                          action_id: str = "") -> None:
        """Короткая человеческая фраза о том, что происходит (для блока статусов)."""
        text = (text or "").strip()
        if not text:
            return
        await self.emit({"type": "status", "text": text, "kind": kind,
                         "tool": tool, "id": action_id})

    # ------------------------- зрение (скриншоты) -------------------------
    async def _disable_vision(self, error: str) -> None:
        vision.vision_state.mark_unsupported(error)
        await self.emit({
            "type": "toast",
            "message": "Модель не принимает изображения — дальше работаю по тексту. "
                       "Для «зрения» загрузите vision-модель (Qwen2.5-VL, Gemma 3, LLaVA).",
            "level": "warn",
        })
        await self.emit({"type": "vision", **vision.vision_state.status()})

    def _inject_images(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Приложить накопленные снимки к последнему сообщению пользователя."""
        if not self._images or not vision.vision_state.enabled():
            return messages
        caption = ("К запросу приложены снимки экрана/окна — используй их, чтобы понять, "
                   "что происходит на экране.\n" + self._vision_caption).strip()
        return vision.attach_images(messages, self._images, caption=caption)

    async def _chat_with_vision(self, messages: List[Dict[str, Any]], **kwargs: Any) -> Dict[str, Any]:
        """Запрос к модели; при ошибке «изображения не поддерживаются» повторяем без них."""
        prepared = self._inject_images(messages)
        if not vision.has_images(prepared):
            return await llm.chat(prepared, **kwargs)
        try:
            reply = await llm.chat(prepared, **kwargs)
            vision.vision_state.mark_sent()
            for item in self._images:
                item["sent"] = True
            await self.emit({"type": "vision", **vision.vision_state.status()})
            return reply
        except LLMError as exc:
            if not vision.looks_like_vision_error(str(exc)):
                raise
            await self._disable_vision(str(exc))
            return await llm.chat(vision.strip_images(prepared), **kwargs)

    # ------------------- визуальная проверка результата -------------------
    def _verify_snapshot(self, args: Dict[str, Any]) -> str:
        """Снимок окна «до» действия. Возвращает путь или пустую строку."""
        if not config.get("agent.visual_verify", True):
            return ""
        try:
            from .paths import SCREENSHOT_DIR
            from .tools import window_control

            title = str(args.get("title") or "").strip()
            window = window_control.find_window(title) if title else window_control.find_window("")
            path = SCREENSHOT_DIR / f"verify_before_{int(time.time() * 1000) % 10 ** 9}.png"
            window_control.capture_window(window, path)
            # делимся снимком «до» с инструментом wait_for_change — чтобы модель,
            # вызвав его после действия, не пропустила уже случившуюся реакцию
            try:
                from .tools.vision_tools import set_baseline

                set_baseline(path, window)
            except Exception:  # noqa: BLE001
                pass
            return str(path)
        except Exception as exc:  # noqa: BLE001
            print(f"[verify] снимок «до» не удался: {exc}")
            return ""

    async def _verify_change(self, before_path: str, args: Dict[str, Any],
                             executed: List[Dict[str, Any]]) -> None:
        """Сравнить экран до и после действия и честно записать вывод в карточку действия."""
        try:
            from .paths import SCREENSHOT_DIR
            from .tools import window_control

            title = str(args.get("title") or "").strip()
            window = window_control.find_window(title) if title else window_control.find_window("")
            after_path = SCREENSHOT_DIR / f"verify_after_{int(time.time() * 1000) % 10 ** 9}.png"
            window_control.capture_window(window, after_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[verify] снимок «после» не удался: {exc}")
            return

        diff = grounding.compare(before_path, after_path)
        if not diff.get("ok"):
            return

        if diff.get("changed"):
            note = f"\n✔ Проверка по снимку: {diff.get('text', '')}"
            executed[-1] = {**executed[-1], "verified": diff.get("verdict"),
                            "verify_diff": diff.get("diff"),
                            "result": (executed[-1].get("result") or "") + note}
            self._verify_note = note
            print(f"[verify] {diff.get('verdict')} ({diff.get('diff')})")
            return

        note = ("\n⚠ Проверка по снимку: экран НЕ изменился — возможно, действие не сработало. "
                "Сделай see_window и попробуй другой способ (клавиатура, другая метка).")
        executed[-1] = {**executed[-1], "verified": "same", "verify_diff": diff.get("diff"),
                        "result": (executed[-1].get("result") or "") + note}
        self._verify_note = note
        await self.emit({
            "type": "toast",
            "message": "Похоже, действие не сработало: экран не изменился. Агент проверит ещё раз.",
            "level": "warn",
        })

    async def _check_captcha(self, executed: List[Dict[str, Any]], args: Dict[str, Any]) -> bool:
        """Если на экране капча — остановиться и передать управление человеку."""
        try:
            from .tools.vision_tools import find_captcha

            found = find_captcha()
        except Exception:  # noqa: BLE001
            return False
        if not found:
            return False
        names = ", ".join(f"«{item['window']}»" for item in found[:3])
        note = (f"\n⛔ Обнаружена проверка «ты человек» ({names}). Такие проверки решает только "
                f"пользователь — агент остановился.")
        if executed:
            executed[-1] = {**executed[-1], "captcha": True,
                            "result": (executed[-1].get("result") or "") + note}
        await self.emit({"type": "toast", "level": "warn", "message":
                         f"Похоже на капчу ({names}). Пройди проверку сам — я остановился."})
        return bool(config.get("agent.stop_on_captcha", True))

    async def _attach_window_snapshot(self, registry: Any, args: Dict[str, Any],
                                      executed: List[Dict[str, Any]], action_id: str,
                                      emit: bool = True) -> bool:
        """Снять окно, в котором только что работали, и приложить снимок к запросу.

        Возвращает True, если карточка действия была обновлена (чтобы не отправлять её дважды).
        """
        if not config.get("agent.auto_window_screenshot", True):
            return False
        if not vision.vision_state.enabled():
            return False
        title = str(args.get("title") or args.get("query") or "").strip()
        try:
            shot = await registry.execute("screenshot_window", {"title": title}, timeout=60)
        except Exception as exc:  # noqa: BLE001
            print(f"[vision] авто-снимок окна не удался: {exc}")
            return False
        if not shot.get("ok") or not shot.get("artifact"):
            return False
        artifact = shot["artifact"]
        part = vision.artifact_to_part(artifact)
        if part is None:
            return False
        caption = f"Снимок окна «{title or 'активное окно'}» после действия."
        part["caption"] = caption
        self._images.append(part)
        limit = int(config.get("lmstudio.vision_max_images") or 2)
        if len(self._images) > limit:
            self._images = self._images[-limit:]
        self._vision_caption = caption

        # отражаем в карточке действия: снимок приложен и модели, и пользователю
        if not executed:
            return False
        executed[-1] = {**executed[-1], "artifact": artifact, "vision_sent": True,
                        "result": (executed[-1].get("result") or "")
                                  + f"\n(автоматически приложен снимок окна: {artifact.get('name', '')})"}
        if emit:
            await self.emit({"type": "action_result", "id": action_id, **executed[-1]})
        return emit

    # ------------------------------------------------------------------
    def _trim(self, text: str, limit: int = 1600) -> str:
        text = (text or "").strip()
        return text if len(text) <= limit else text[:limit] + "…"

    def _history_messages(self, history: List[Dict[str, Any]], limit: int) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = []
        recent = [m for m in history if m.get("role") in ("user", "assistant") and not m.get("hidden")][-limit:]
        for item in recent:
            role = "assistant" if item["role"] == "assistant" else "user"
            content = self._trim(item.get("content", ""))
            if content:
                messages.append({"role": role, "content": content})
        return messages

    def _build_planner_messages(self, history, user_text, limit, executed=None) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = [{"role": "system", "content": planner_prompt()}]
        messages.extend(self._history_messages(history, limit))
        messages.append({"role": "user", "content": user_text})
        if executed:
            messages.append({
                "role": "user",
                "content": ("Журнал уже выполненных действий:\n" + summarize_actions(executed)
                            + "\n\nРеши следующий шаг. Если задача решена — верни "
                              '{"thought": "...", "action": "final"}. Если нужен ещё инструмент — верни JSON с actions.'),
            })
        return messages

    def _build_answer_messages(self, history, user_text, modality, limit, executed) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = [{"role": "system", "content": answer_prompt(modality)}]
        messages.extend(self._history_messages(history, limit))
        messages.append({"role": "user", "content": user_text})
        if executed:
            messages.append({
                "role": "user",
                "content": ("Результаты выполнения инструментов (служебная информация, пользователю её видно "
                            "в виде карточек):\n" + summarize_actions(executed)
                            + "\n\nСформулируй ответ пользователю на его последнюю просьбу."),
            })
        return messages

    # ------------------------------------------------------------------
    async def generate_title(self, first_user_text: str, answer_text: str = "") -> str:
        prompt = [
            {"role": "system", "content": planner_prompt()[:0] or "Ты помогаешь придумывать названия диалогов."},
            {"role": "user",
             "content": (f"Сообщение пользователя: {first_user_text[:600]}\n"
                         f"Ответ ассистента: {answer_text[:600]}\n\n"
                         "Придумай короткое название (2–5 слов) для этого диалога. "
                         "Ответь только названием на русском, без кавычек и точки.")},
        ]
        try:
            reply = await llm.chat(prompt, temperature=0.3, max_tokens=40)
            title = (reply.get("content") or "").strip().strip('"«»\n ')
            title = title.split("\n")[0].strip()
            # модель иногда возвращает целую фразу вместо названия — тогда берём текст пользователя
            if not title or len(title.split()) > 7 or len(title) > 64:
                title = first_user_text.strip().replace("\n", " ")[:42]
            return title[:60] or "Новый чат" 
        except LLMError:
            return (first_user_text or "Новый чат")[:40]
