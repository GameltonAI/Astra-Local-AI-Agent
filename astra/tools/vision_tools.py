"""Инструменты «умного зрения»: разметка окна, клик по метке, ожидание и проверка.

Идея: модель не должна угадывать координаты «на глаз». Сначала `see_window` отдаёт
снимок с координатной сеткой и нумерованными метками (Set-of-Mark), затем модель
кликает по номеру метки (`click_window_mark`) или по числу с сетки.
А `wait_for_change` / `wait_until_stable` / `scroll_until_end` / `wait_for_new_window`
позволяют работать с динамикой: загрузкой, поп-апами и бесконечной прокруткой.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import grounding
from ..config import config
from ..paths import SCREENSHOT_DIR
from . import os_control, window_control
from .base import Param, Tool, ToolError

# Последняя разметка по окну: ключ — id окна, значение — (заголовок, метки)
_LAST_MARKS: Dict[str, Tuple[str, List[grounding.Mark]]] = {}

# Снимок «до действия» по окну: чтобы wait_for_change не пропустил реакцию,
# которая случилась раньше, чем агент успел его вызвать.
_BASELINE: Dict[str, Tuple[str, str, float]] = {}   # wid -> (title, путь, время)

CAPTCHA_MARKERS = (
    "captcha", "капч", "recaptcha", "hcaptcha", "are you a robot", "not a robot",
    "verify you are human", "не робот", "вы человек", "подтвердите, что вы человек",
    "cloudflare", "checking your browser", "проверка браузера",
)
POPUP_HINTS = ("popup", "dialog", "alert", "cookie", "cookies", "уведомление", "подтверд")


# ----------------------------------------------------------------------
#  Вспомогательное
# ----------------------------------------------------------------------
def _safe(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)[:40] or "window"


def _shot(window: Any, prefix: str = "see") -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = SCREENSHOT_DIR / f"{prefix}_{_safe(window.title)}_{ts}_{int(time.time() * 1000) % 1000}.png"
    window_control.capture_window(window, path)
    return path


def _resolve_window(title: str, focus: bool = True) -> Any:
    query = (title or "").strip()
    if not query:
        return window_control.find_window("")
    return window_control.focus_window(query) if focus else window_control.find_window(query)


def _remember(window: Any, annotation: grounding.Annotation) -> None:
    _LAST_MARKS[str(window.wid)] = (window.title, annotation.marks)


def last_marks(window: Any) -> List[grounding.Mark]:
    item = _LAST_MARKS.get(str(window.wid))
    if item is None:
        # окно могло пересоздаться — ищем по заголовку
        for _, (title, marks) in _LAST_MARKS.items():
            if title and title == window.title:
                return marks
        return []
    return item[1]


def set_baseline(path: str | Path, window: Any = None, title: str = "") -> str:
    """Запомнить снимок «до действия» для окна (используется wait_for_change)."""
    import shutil

    try:
        if window is None:
            window = _resolve_window(title, focus=False)
        wid = str(window.wid)
        target = SCREENSHOT_DIR / f"baseline_{_safe(wid)}.png"
        shutil.copyfile(path, target)
        _BASELINE[wid] = (window.title, str(target), time.time())
        return str(target)
    except Exception as exc:  # noqa: BLE001
        print(f"[vision] не удалось запомнить снимок «до»: {exc}")
        return ""


def get_baseline(window: Any, max_age: float = 180.0) -> Optional[Path]:
    """Снимок окна до последнего действия (если он ещё актуален)."""
    item = _BASELINE.get(str(window.wid))
    if item is None:
        return None
    _title, path, ts = item
    if time.time() - ts > max_age or not Path(path).exists():
        return None
    return Path(path)


def _artifact(path: str | Path, name: str = "") -> Optional[Dict[str, Any]]:
    data_uri = grounding.image_preview_data_uri(path)
    if not data_uri:
        return None
    return {"kind": "image", "name": name or Path(path).name, "path": str(path), "data_uri": data_uri}


# ----------------------------------------------------------------------
#  Set-of-Mark: снимок с сеткой и метками
# ----------------------------------------------------------------------
def see_window(title: str = "", mode: str = "both", step: int = 0, max_marks: int = 40) -> Dict[str, Any]:
    """Снимок окна с координатной сеткой и нумерованными метками элементов."""
    window = _resolve_window(title)
    time.sleep(0.12)
    source = _shot(window, prefix="see")
    annotated_path = source.with_name(source.stem + "_marks.png")
    annotation = grounding.annotate_file(
        source, annotated_path, mode=mode, step=int(step or config.get("agent.grid_step") or 0),
        max_marks=max(6, min(80, int(max_marks))), window=window, title=window.title,
    )
    _remember(window, annotation)
    set_baseline(source, window)

    text = grounding.describe(annotation)
    text += (f"\n\nОкно «{window.title}»: размер {window.width}×{window.height} px, "
             f"содержимое {window.client_width}×{window.client_height} px.")
    if not annotation.marks and mode in ("marks", "both"):
        text += ("\nЭлементы не распознались — используй сетку: прочитай координаты нужного места "
                 "по линиям и вызови click_in_window.")
    return {"text": text, "artifact": _artifact(annotation.path, Path(annotation.path).name)}


def click_window_mark(title: str, mark: int, button: str = "left", clicks: int = 1) -> str:
    """Клик по нумерованной метке из последнего see_window (никаких пересчётов координат)."""
    window = _resolve_window(title)
    marks = last_marks(window)
    if not marks:
        raise ToolError(
            f"Для окна «{window.title}» ещё нет разметки. Сначала вызови "
            f"see_window(title=\"{title or window.title}\"), потом кликай по метке."
        )
    target = next((m for m in marks if m.id == int(mark)), None)
    if target is None:
        available = ", ".join(str(m.id) for m in marks[:20])
        raise ToolError(f"Метки №{mark} нет. Доступные метки: {available}")
    x, y = target.click
    if not (0 <= x <= window.client_width and 0 <= y <= window.client_height):
        raise ToolError(f"Метка №{mark} ({x}, {y}) вне области содержимого окна "
                        f"{window.client_width}×{window.client_height} — обнови разметку через see_window.")
    time.sleep(0.12)
    try:                                     # снимок «до» — чтобы потом было что сравнивать
        set_baseline(_shot(window, prefix="baseline"), window)
    except Exception as exc:  # noqa: BLE001
        print(f"[vision] базовый снимок не удался: {exc}")
    screen_x, screen_y = window_control.client_point(window, x, y)
    result = os_control.mouse_click(button, clicks, screen_x, screen_y)
    return (f"В окне «{window.title}»: нажата метка #{mark} в точке ({x}, {y}) — {result}. "
            f"Чтобы убедиться, что сработало, вызови wait_for_change(title=\"{title or window.title}\").")


# ----------------------------------------------------------------------
#  Динамика: загрузка, поп-апы, бесконечная прокрутка
# ----------------------------------------------------------------------
def wait_for_change(title: str = "", timeout: float = 8.0, min_diff: float = 0.0,
                    interval: float = 0.45) -> Dict[str, Any]:
    """Ждать, пока экран изменится (загрузка, поп-ап, реакция на клик)."""
    window = _resolve_window(title)
    started = time.time()
    deadline = started + max(1.0, float(timeout))
    baseline = get_baseline(window)
    before = _shot(window, prefix="wait_before")
    references: List[Path] = [before]
    if baseline is not None:
        # действие могло сработать ещё до вызова инструмента — сверяемся со снимком «до»
        references.insert(0, baseline)
    temps: List[Path] = [before]
    attempts = 0
    try:
        while time.time() < deadline:
            time.sleep(max(0.15, float(interval)))
            attempts += 1
            current = _shot(window, prefix="wait_now")
            temps.append(current)
            diff = None
            for reference in references:
                if not Path(reference).exists():
                    continue
                candidate = grounding.compare(reference, current, min_diff or None)
                if candidate.get("ok") and candidate.get("changed"):
                    diff = candidate
                    break
            if diff is not None:
                text = (f"Экран окна «{window.title}» изменился через {time.time() - started:.1f} с "
                        f"({attempts} проверок). {diff.get('text', '')}")
                return {"text": text, "artifact": _artifact(current, Path(current).name),
                        "change": diff}
        return {
            "text": (f"За {float(timeout):.0f} с экран окна «{window.title}» не изменился. "
                     f"Возможно, действие не сработало: проверь see_window и попробуй другой способ "
                     f"(например, клавиатуру вместо клика)."),
            "artifact": _artifact(before, Path(before).name),
            "change": {"changed": False, "verdict": "same"},
        }
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)


def wait_until_stable(title: str = "", timeout: float = 12.0, quiet_ms: int = 700) -> Dict[str, Any]:
    """Ждать, пока экран перестанет меняться (закончилась загрузка/анимация)."""
    window = _resolve_window(title)
    started = time.time()
    deadline = started + max(2.0, float(timeout))
    quiet = max(0.2, float(quiet_ms) / 1000.0)
    temps: List[Path] = []
    previous = _shot(window, prefix="stable")
    temps.append(previous)
    stable_since = time.time()
    try:
        while time.time() < deadline:
            time.sleep(0.3)
            current = _shot(window, prefix="stable_now")
            temps.append(current)
            diff = grounding.compare(previous, current)
            if not diff.get("changed"):
                if time.time() - stable_since >= quiet:
                    text = (f"Окно «{window.title}» перестало меняться через "
                            f"{time.time() - started:.1f} с — загрузка завершена, можно действовать.")
                    return {"text": text, "artifact": _artifact(current, Path(current).name),
                            "stable": True}
            else:
                stable_since = time.time()
            previous = current
        return {
            "text": (f"За {float(timeout):.0f} с окно «{window.title}» так и не «успокоилось» "
                     f"(что-то постоянно меняется — возможно, идёт анимация загрузки)."),
            "artifact": _artifact(previous, Path(previous).name),
            "stable": False,
        }
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)


def scroll_until_end(title: str, amount: int = -5, max_steps: int = 8,
                     pause: float = 0.5) -> Dict[str, Any]:
    """Листать, пока содержимое не перестанет меняться (конец списка / бесконечный скролл)."""
    window = _resolve_window(title)
    steps = max(1, min(20, int(max_steps)))
    moved = 0
    last_artifact: Optional[Dict[str, Any]] = None
    previous = _shot(window, prefix="scroll")
    for _ in range(steps):
        center_x, center_y = window_control.client_point(
            window, window.client_width // 2, window.client_height // 2)
        os_control.mouse_move(center_x, center_y)
        os_control.mouse_scroll(int(amount))
        time.sleep(max(0.2, float(pause)))
        current = _shot(window, prefix="scroll_now")
        diff = grounding.compare(previous, current)
        previous.unlink(missing_ok=True)
        previous = current
        last_artifact = _artifact(current, Path(current).name)
        if not diff.get("changed"):
            return {
                "text": (f"Прокрутил {moved} раз(а) — содержимое окна «{window.title}» больше не меняется, "
                         f"достигнут конец списка."),
                "artifact": last_artifact,
                "steps": moved,
                "reached_end": True,
            }
        moved += 1
    return {
        "text": (f"Прокрутил {moved} раз(а) в окне «{window.title}», содержимое всё ещё меняется — "
                 f"список длиннее, чем {moved} экранов. Нужное можно искать поиском по странице (Ctrl+F)."),
        "artifact": last_artifact,
        "steps": moved,
        "reached_end": False,
    }


def wait_for_new_window(title: str = "", timeout: float = 8.0,
                        exclude: str = "") -> str:
    """Ждать появления нового окна (диалог, поп-ап, подтверждение) и вернуть его заголовок."""
    started = time.time()
    deadline = started + max(1.0, float(timeout))
    known = {str(w.wid) for w in window_control.list_windows()}
    needle = (title or "").strip().lower()
    skip = (exclude or "").strip().lower()
    while time.time() < deadline:
        time.sleep(0.4)
        for window in window_control.list_windows():
            wid = str(window.wid)
            if wid in known:
                continue
            name = (window.title or "").lower()
            if needle and needle not in name:
                continue
            if skip and skip in name:
                continue
            hint = " (похоже на капчу — решать её нельзя, передай управление пользователю)" \
                if any(m in name for m in CAPTCHA_MARKERS) else ""
            return (f"Появилось новое окно: «{window.title}» ({window.app}, "
                    f"{window.width}×{window.height} px, id={window.wid}){hint}. "
                    f"Работать в нём: focus_window(title=\"{window.title[:40]}\").")
    return (f"За {float(timeout):.0f} с новое окно не появилось. Возможно, диалог не открылся — "
            f"проверь see_window.")


# ----------------------------------------------------------------------
#  Капча и «человеческие» проверки
# ----------------------------------------------------------------------
def find_captcha(windows: Optional[List[Any]] = None) -> List[Dict[str, str]]:
    """Найти признаки капчи/проверки «вы человек» по заголовкам окон."""
    found: List[Dict[str, str]] = []
    try:
        items = windows if windows is not None else window_control.list_windows()
    except Exception:  # noqa: BLE001
        return found
    for window in items:
        name = (window.title or "").lower()
        marker = next((m for m in CAPTCHA_MARKERS if m in name), "")
        if marker:
            found.append({"window": window.title, "marker": marker})
    return found


def detect_captcha() -> str:
    """Проверить, не просит ли сайт/программа подтвердить, что ты человек."""
    found = find_captcha()
    if not found:
        return ("Признаков капчи не видно. Если на странице всё-таки капча — сделай see_window "
                "и опиши её пользователю, решать самому нельзя.")
    lines = [f"⚠ Похоже на капчу/проверку: «{item['window']}» (признак: {item['marker']})"
             for item in found]
    lines.append("Такие проверки решает только человек. Останови работу и попроси пользователя "
                 "пройти проверку в этом окне, затем продолжи.")
    return "\n".join(lines)


# ----------------------------------------------------------------------
#  Реестр
# ----------------------------------------------------------------------
TOOLS = [
    Tool(
        name="see_window",
        summary="Снимок окна с координатной сеткой и нумерованными метками элементов (Set-of-Mark): "
                "точной координаты клика не нужно угадывать. Возвращает картинку и таблицу «метка → координаты».",
        params=[
            Param("title", "str", "Часть заголовка окна или имя программы (пусто — активное окно)", False, ""),
            Param("mode", "str", "Что нарисовать: both — сетка + метки, grid — только сетка, marks — только метки",
                  False, "both", enum=["both", "grid", "marks"]),
            Param("step", "int", "Шаг сетки в пикселях (0 — подобрать автоматически)", False, 0),
            Param("max_marks", "int", "Сколько меток максимум нарисовать", False, 40),
        ],
        func=see_window,
        category="vision",
        examples=['{"title": "chrome"}', '{"title": "chrome", "mode": "grid"}'],
    ),
    Tool(
        name="click_window_mark",
        summary="Клик по нумерованной метке из последнего see_window. Координаты берутся из разметки — "
                "самый точный способ нажать кнопку или поле в окне.",
        params=[
            Param("title", "str", "Часть заголовка окна или имя программы", True),
            Param("mark", "int", "Номер метки (#N) на снимке из see_window", True),
            Param("button", "str", "Кнопка мыши", False, "left", enum=["left", "right", "middle"]),
            Param("clicks", "int", "Количество кликов", False, 1),
        ],
        func=click_window_mark,
        category="vision",
        danger="confirm",
        examples=['{"title": "chrome", "mark": 7}'],
    ),
    Tool(
        name="wait_for_change",
        summary="Ждать изменения экрана окна (реакция на клик, загрузка, появление поп-апа). "
                "Возвращает вывод «изменилось / не изменилось» и свежий снимок — это и есть проверка результата.",
        params=[
            Param("title", "str", "Часть заголовка окна или имя программы", False, ""),
            Param("timeout", "float", "Сколько секунд ждать", False, 8.0),
            Param("min_diff", "float", "Порог изменения (0 — по настройкам, ~0.008 — чувствительно)", False, 0.0),
        ],
        func=wait_for_change,
        category="vision",
        examples=['{"title": "chrome", "timeout": 6}'],
    ),
    Tool(
        name="wait_until_stable",
        summary="Ждать, пока окно перестанет меняться — так проверяют, что загрузка/анимация закончилась.",
        params=[
            Param("title", "str", "Часть заголовка окна или имя программы", False, ""),
            Param("timeout", "float", "Сколько секунд ждать максимум", False, 12.0),
            Param("quiet_ms", "int", "Сколько миллисекунд экран должен быть неподвижен", False, 700),
        ],
        func=wait_until_stable,
        category="vision",
    ),
    Tool(
        name="scroll_until_end",
        summary="Листать окно, пока содержимое не перестанет меняться: конец списка, бесконечная лента, "
                "лента товаров/комментариев.",
        params=[
            Param("title", "str", "Часть заголовка окна или имя программы", True),
            Param("amount", "int", "Щелчки колеса за шаг (отрицательное — вниз)", False, -5),
            Param("max_steps", "int", "Сколько шагов максимум", False, 8),
        ],
        func=scroll_until_end,
        category="vision",
        danger="confirm",
    ),
    Tool(
        name="wait_for_new_window",
        summary="Ждать появления нового окна — поп-апа, диалога подтверждения, окна входа. "
                "Возвращает заголовок нового окна.",
        params=[
            Param("title", "str", "Часть ожидаемого заголовка (пусто — любое новое окно)", False, ""),
            Param("timeout", "float", "Сколько секунд ждать", False, 8.0),
            Param("exclude", "str", "Часть заголовка, которую надо игнорировать", False, ""),
        ],
        func=wait_for_new_window,
        category="vision",
    ),
    Tool(
        name="detect_captcha",
        summary="Проверить, не требует ли сайт/программа подтвердить, что ты человек (капча). "
                "Капчу решает только пользователь — агент обязан остановиться.",
        params=[],
        func=detect_captcha,
        category="vision",
    ),
]
