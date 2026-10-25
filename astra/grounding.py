"""Зрение для действий: координатная сетка, Set-of-Mark и проверка результата.

Зачем это нужно
---------------
Просто «показать модели скриншот» недостаточно: модель видит картинку, но не знает,
куда именно кликать — она придумывает координаты «на глаз» и промахивается.
Здесь три технологии, которые превращают зрение в координаты и проверяют результат:

1. **Координатная сетка (OS-World alignment).** Поверх снимка рисуется сетка с подписями,
   причём подписи — в ТОЙ ЖЕ системе координат, которую принимает `click_in_window`
   (от левого верхнего угла содержимого окна). Модели не нужно пересчитывать масштаб:
   она читает число с картинки и передаёт его в инструмент.

2. **Set-of-Mark.** Кроме сетки на снимок наносятся нумерованные метки на найденных
   элементах интерфейса (кнопки, поля, строки, иконки). Метка = прямоугольник + номер,
   а в тексте рядом с картинкой — таблица «#номер → координаты клика». Модель может
   ответить «нажми метку 7», и это будет точный пиксель.

   Классический Set-of-Mark использует модель-сегментатор (SAM). Здесь зависимостей нет:
   кандидаты находятся классическим компьютерным зрением (градиенты → дилатация →
   связные компоненты → фильтрация по площади/форме). Работает на любом ПК и на любом
   интерфейсе, включая веб-страницы и «рисованные» приложения.

3. **Верификация результата.** `compare()` сравнивает снимок до и после действия:
   если экран не изменился, агент честно сообщает «похоже, клик не сработал»,
   вместо того чтобы считать задачу выполненной.

Всё считается на numpy + Pillow, без OpenCV.
"""
from __future__ import annotations

import hashlib
import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .config import config

Box = Tuple[int, int, int, int]   # x0, y0, x1, y1


# ----------------------------------------------------------------------
#  Модель данных
# ----------------------------------------------------------------------
@dataclass
class Mark:
    """Кандидат на интерактивный элемент (или ячейка сетки)."""

    id: int
    box: Box                     # в координатах изображения
    kind: str = "region"         # region | cell
    area: int = 0
    score: float = 0.0
    click: Tuple[int, int] = (0, 0)   # координаты для click_in_window

    @property
    def size(self) -> Tuple[int, int]:
        return self.box[2] - self.box[0], self.box[3] - self.box[1]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "box": list(self.box),
            "center": [self.click[0], self.click[1]],
            "size": list(self.size),
            "kind": self.kind,
        }


@dataclass
class Annotation:
    """Результат разметки: файл с наложением + метки + текст для модели."""

    path: str
    width: int
    height: int
    step: int
    origin: Tuple[int, int]              # смещение содержимого окна (dx, dy)
    marks: List[Mark] = field(default_factory=list)
    mode: str = "both"
    title: str = ""
    elapsed_ms: int = 0

    def marks_table(self, limit: int = 40) -> str:
        """Таблица «метка → координаты» для текстовой части ответа."""
        if not self.marks:
            return "Метки не найдены — ориентируйся по сетке."
        lines = []
        for mark in self.marks[:limit]:
            w, h = mark.size
            lines.append(f"  #{mark.id}: клик ({mark.click[0]}, {mark.click[1]}), размер {w}×{h}")
        return "\n".join(lines)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "step": self.step,
            "origin": list(self.origin),
            "mode": self.mode,
            "title": self.title,
            "marks": [m.as_dict() for m in self.marks],
            "elapsed_ms": self.elapsed_ms,
        }


# ----------------------------------------------------------------------
#  Утилиты
# ----------------------------------------------------------------------
def _load_rgb(path: str | Path) -> Any:
    from PIL import Image  # type: ignore

    img = Image.open(path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


GRID_STEPS = (20, 25, 40, 50, 75, 100, 125, 150, 200, 250, 300, 400, 500)


def auto_step(width: int, height: int) -> int:
    """Шаг сетки: подбираем «круглое» число, дающее ~10 линий по короткой стороне."""
    short = max(1, min(width, height))
    target = 10.0
    return min(GRID_STEPS, key=lambda step: (abs(short / step - target), step))


def _client_origin(window: Any, image_size: Tuple[int, int]) -> Tuple[int, int]:
    """Смещение содержимого окна внутри снимка.

    PrintWindow отдаёт снимок всего окна (с рамкой и заголовком), ImageGrab по границам
    окна — тоже. Но некоторые системы/режимы отдают уже только клиентскую область.
    Определяем по размерам: если картинка совпадает с клиентской областью — смещения нет.
    """
    try:
        iw, ih = image_size
        cw, ch = int(window.client_width), int(window.client_height)
        dx = int(window.client_left) - int(window.left)
        dy = int(window.client_top) - int(window.top)
        if abs(iw - cw) <= 4 and abs(ih - ch) <= 4:
            return 0, 0
        return max(0, dx), max(0, dy)
    except Exception:  # noqa: BLE001
        return 0, 0


# ----------------------------------------------------------------------
#  Поиск элементов (Set-of-Mark без тяжёлых моделей)
# ----------------------------------------------------------------------
def _edge_mask(gray: np.ndarray, threshold: int = 18) -> np.ndarray:
    """Границы объектов: разности соседних пикселей по X и Y."""
    arr = gray.astype(np.int16)
    gx = np.zeros_like(arr)
    gy = np.zeros_like(arr)
    gx[:, :-1] = np.abs(np.diff(arr, axis=1))
    gy[:-1, :] = np.abs(np.diff(arr, axis=0))
    magnitude = gx + gy
    return magnitude > threshold


def _dilate(mask: np.ndarray, radius: int = 1) -> np.ndarray:
    """Расширение маски (без OpenCV) — склеиваем контур одного элемента."""
    out = mask.copy()
    for _ in range(max(1, radius)):
        src = out.copy()
        out[1:, :] |= src[:-1, :]
        out[:-1, :] |= src[1:, :]
        out[:, 1:] |= src[:, :-1]
        out[:, :-1] |= src[:, 1:]
    return out


def _components(mask: np.ndarray, min_area: int, max_area: int) -> List[Tuple[Box, int]]:
    """Связные компоненты обходом в ширину. Возвращает (box, площадь)."""
    height, width = mask.shape
    live = mask.copy()
    result: List[Tuple[Box, int]] = []
    ys, xs = np.nonzero(live)
    for start_y, start_x in zip(ys.tolist(), xs.tolist()):
        if not live[start_y, start_x]:
            continue
        live[start_y, start_x] = False
        stack = [(start_x, start_y)]
        x0 = x1 = start_x
        y0 = y1 = start_y
        area = 0
        while stack:
            cx, cy = stack.pop()
            area += 1
            if cx < x0:
                x0 = cx
            elif cx > x1:
                x1 = cx
            if cy < y0:
                y0 = cy
            elif cy > y1:
                y1 = cy
            for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                if 0 <= nx < width and 0 <= ny < height and live[ny, nx]:
                    live[ny, nx] = False
                    stack.append((nx, ny))
        if min_area <= area <= max_area:
            result.append(((x0, y0, x1 + 1, y1 + 1), area))
    return result


def _iou(a: Box, b: Box) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / union if union > 0 else 0.0


def detect_regions(
    image: Any,
    max_marks: int = 40,
    max_dim: int = 560,
    min_side: int = 10,
) -> List[Mark]:
    """Найти кандидатов на элементы интерфейса. Возвращает метки в координатах изображения."""
    from PIL import Image, ImageFilter  # type: ignore

    width, height = image.size
    if width < 8 or height < 8:
        return []
    scale = min(1.0, max_dim / float(max(width, height)))
    small = image.convert("L")
    if scale < 0.999:
        small = small.resize((max(1, int(width * scale)), max(1, int(height * scale))),
                             Image.BILINEAR)

    gray = np.asarray(small, dtype=np.uint8)
    edges = _edge_mask(gray, threshold=18)
    # «утолщаем» контуры: так рамка кнопки становится единым объектом
    edges = np.asarray(
        Image.fromarray((edges * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(3)),
        dtype=np.uint8,
    ) > 0

    total = gray.shape[0] * gray.shape[1]
    min_area = max(24, int(total * 0.00025))
    max_area = int(total * 0.45)
    raw = _components(edges, min_area=min_area, max_area=max_area)

    candidates: List[Tuple[float, Box]] = []
    for (x0, y0, x1, y1), area in raw:
        w, h = x1 - x0, y1 - y0
        if w < min_side * scale or h < min_side * scale:
            continue
        box_area = w * h
        if box_area <= 0:
            continue
        fill = area / float(box_area)
        if fill < 0.04:                     # разрозненный мусор
            continue
        aspect = w / float(h)
        if not (0.06 <= aspect <= 14.0):
            continue
        ratio = box_area / float(total)
        if ratio > 0.5:                     # всё окно целиком — не элемент
            continue
        # Оценка «похоже на элемент управления»: компактные блоки среднего размера
        size_score = 1.0 - min(1.0, abs(np.log(max(ratio, 1e-6) / 0.02)) / 3.0)
        shape_score = 1.0 - min(1.0, abs(np.log(aspect)) / 2.2)
        fill_score = min(1.0, fill / 0.35)
        score = 0.45 * size_score + 0.3 * shape_score + 0.25 * fill_score
        candidates.append((score, (int(x0 / scale), int(y0 / scale),
                                   int(min(width, x1 / scale)), int(min(height, y1 / scale)))))

    # убираем вложенные/дублирующиеся рамки
    candidates.sort(key=lambda item: (-item[0], -(item[1][2] - item[1][0]) * (item[1][3] - item[1][1])))
    kept: List[Tuple[float, Box]] = []
    for score, box in candidates:
        if any(_iou(box, other) > 0.55 for _, other in kept):
            continue
        kept.append((score, box))
        if len(kept) >= max(1, max_marks) * 3:
            break

    # нумеруем в порядке чтения: сверху вниз, слева направо
    kept.sort(key=lambda item: (item[1][1] // 12, item[1][0]))
    marks: List[Mark] = []
    for index, (score, box) in enumerate(kept[: max(1, max_marks)], start=1):
        marks.append(Mark(id=index, box=box, kind="region", score=round(score, 3)))
    return marks


# ----------------------------------------------------------------------
#  Отрисовка сетки и меток
# ----------------------------------------------------------------------
def _font(size: int):
    from PIL import ImageFont  # type: ignore

    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "DejaVuSans.ttf",
                 "arial.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


GRID_COLOR = (0, 220, 140)
GRID_TEXT = (10, 40, 30)
MARK_COLORS = [
    (255, 92, 120), (90, 170, 255), (255, 190, 60), (150, 120, 255),
    (60, 220, 200), (255, 140, 60), (120, 220, 100), (230, 120, 255),
]


def annotate_image(
    image: Any,
    marks: Optional[Sequence[Mark]] = None,
    step: int = 0,
    origin: Tuple[int, int] = (0, 0),
    mode: str = "both",
    title: str = "",
) -> Tuple[Any, Dict[str, Any]]:
    """Нарисовать сетку и/или метки. Подписи — в координатах клика (клиентская область)."""
    from PIL import ImageDraw  # type: ignore

    mode = (mode or "both").lower()
    if mode not in ("grid", "marks", "both", "none"):
        mode = "both"
    canvas = image.copy()
    draw = ImageDraw.Draw(canvas, "RGBA")
    width, height = canvas.size
    dx, dy = origin
    step = int(step or 0) or auto_step(width, height)
    font = _font(13)
    small_font = _font(11)

    if mode in ("grid", "both"):
        # линии с шагом step; подпись соответствует клиентской координате
        start_x = int(dx % step)
        for x in range(start_x, width, step):
            alpha = 120 if (x - dx) % (step * 2) == 0 else 70
            draw.line([(x, 0), (x, height)], fill=(*GRID_COLOR, alpha), width=1)
        start_y = int(dy % step)
        for y in range(start_y, height, step):
            alpha = 120 if (y - dy) % (step * 2) == 0 else 70
            draw.line([(0, y), (width, y)], fill=(*GRID_COLOR, alpha), width=1)
        # подписи по верхнему и левому краю
        for x in range(start_x, width, step):
            label = str(x - dx)
            tw = draw.textlength(label, font=small_font)
            draw.rectangle([x + 1, 1, x + tw + 5, 15], fill=(255, 255, 255, 210))
            draw.text((x + 3, 2), label, fill=GRID_TEXT, font=small_font)
        for y in range(start_y, height, step):
            label = str(y - dy)
            tw = draw.textlength(label, font=small_font)
            draw.rectangle([1, y + 1, tw + 5, y + 14], fill=(255, 255, 255, 210))
            draw.text((3, y + 1), label, fill=GRID_TEXT, font=small_font)

    if mode in ("marks", "both") and marks:
        for mark in marks:
            x0, y0, x1, y1 = mark.box
            color = MARK_COLORS[(mark.id - 1) % len(MARK_COLORS)]
            draw.rectangle([x0, y0, max(x0 + 2, x1), max(y0 + 2, y1)], outline=(*color, 255), width=2)
            # номер в кружке в левом верхнем углу рамки
            label = str(mark.id)
            tw = draw.textlength(label, font=font)
            radius = max(8, int(tw / 2) + 5)
            cx = min(max(radius + 1, x0), width - radius - 1)
            cy = min(max(radius + 1, y0), height - radius - 1)
            draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                         fill=(*color, 235), outline=(255, 255, 255, 230), width=1)
            draw.text((cx - tw / 2, cy - 7), label, fill=(15, 15, 20), font=font)

    meta = {
        "step": step,
        "origin": [dx, dy],
        "mode": mode,
        "title": title,
        "width": width,
        "height": height,
        "grid": {"cols": len(range(int(dx % step), width, step)),
                 "rows": len(range(int(dy % step), height, step))},
        "marks": [m.as_dict() for m in (marks or [])],
    }
    return canvas, meta


def annotate_file(
    source: str | Path,
    target: Optional[str | Path] = None,
    mode: str = "both",
    step: int = 0,
    max_marks: int = 40,
    window: Any = None,
    title: str = "",
) -> Annotation:
    """Снять разметку с файла и сохранить результат. Метки — с готовыми координатами клика."""
    started = time.time()
    source_path = Path(source)
    image = _load_rgb(source_path)
    origin = _client_origin(window, image.size) if window is not None else (0, 0)
    marks: List[Mark] = []
    if mode in ("marks", "both"):
        marks = detect_regions(image, max_marks=max_marks)
        for mark in marks:
            x0, y0, x1, y1 = mark.box
            mark.click = (int((x0 + x1) / 2) - origin[0], int((y0 + y1) / 2) - origin[1])
    canvas, meta = annotate_image(image, marks=marks, step=step, origin=origin,
                                  mode=mode, title=title)
    out_path = Path(target) if target else source_path.with_name(
        source_path.stem + f"_marks_{int(started) % 100000}.png")
    canvas.save(out_path)
    return Annotation(
        path=str(out_path),
        width=image.size[0],
        height=image.size[1],
        step=meta["step"],
        origin=origin,
        marks=marks,
        mode=meta["mode"],
        title=title,
        elapsed_ms=int((time.time() - started) * 1000),
    )


def describe(annotation: Annotation) -> str:
    """Текст-легенда: что означают сетка и метки (уходит модели)."""
    dx, dy = annotation.origin
    parts = [
        f"Разметка снимка «{annotation.title or 'окно'}»: {annotation.width}×{annotation.height} px, "
        f"смещение содержимого окна ({dx}, {dy}).",
    ]
    if annotation.mode in ("grid", "both"):
        parts.append(
            f"Сетка: линии через каждые {annotation.step} px, подписи по краям — это координаты "
            f"в системе click_in_window (0,0 — левый верхний угол содержимого окна). "
            f"Число с сетки можно сразу передавать в click_in_window(x, y)."
        )
    if annotation.mode in ("marks", "both"):
        parts.append(
            f"Метки: {len(annotation.marks)} пронумерованных прямоугольников на найденных элементах "
            f"интерфейса (кнопки, поля, строки, иконки). Координаты клика по метке:\n"
            + annotation.marks_table()
        )
        if annotation.marks:
            parts.append("Чтобы нажать элемент — вызови click_window_mark(title, mark=N); "
                         "пересчитывать координаты не нужно.")
    return "\n".join(parts)


# ----------------------------------------------------------------------
#  Проверка результата: сравнение снимков до/после
# ----------------------------------------------------------------------
def _thumb_gray(path: str | Path, size: int = 192) -> np.ndarray:
    from PIL import Image  # type: ignore

    with Image.open(path) as img:
        gray = img.convert("L")
        gray.thumbnail((size, size))
        return np.asarray(gray, dtype=np.float32)


def compare(before: str | Path, after: str | Path, min_diff: Optional[float] = None,
            thumb: int = 512, pixel_delta: float = 0.10, block: int = 16,
            block_ratio: float = 0.06) -> Dict[str, Any]:
    """Сравнить два снимка и решить, отреагировал ли интерфейс.

    Маленькая реакция (появилась строка «Сохранено», подсветилась кнопка, открылся
    выпадающий список) занимает доли процента площади окна, поэтому смотреть только на
    общую долю изменённых пикселей нельзя. Считаем ещё и по блокам 16×16: если хотя бы
    один блок изменился существенно — интерфейс отреагировал.
    """
    from PIL import Image  # type: ignore

    # порог: доля изменённых пикселей, ниже которой это шум (мигающий курсор, антиалиасинг)
    noise_floor = float(min_diff if min_diff is not None
                        else (config.get("agent.verify_min_diff") or 0.0002))

    try:
        with Image.open(before) as img_a, Image.open(after) as img_b:
            size_a, size_b = img_a.size, img_b.size
        a = _thumb_gray(before, thumb)
        b = _thumb_gray(after, thumb)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Не удалось прочитать снимки: {exc}",
                "changed": True, "diff": 1.0, "verdict": "unknown", "regions": [],
                "blocks_changed": 0, "text": ""}

    # сначала проверяем сами размеры: окно могли развернуть/изменить
    if (abs(size_a[0] - size_b[0]) > max(4, size_a[0] * 0.02)
            or abs(size_a[1] - size_b[1]) > max(4, size_a[1] * 0.02)):
        return {"ok": True, "changed": True, "diff": 1.0, "verdict": "changed", "regions": [],
                "blocks_changed": 0,
                "text": f"Размер окна изменился ({size_a[0]}×{size_a[1]} → {size_b[0]}×{size_b[1]}) — "
                        f"экран точно обновился."}

    if a.shape != b.shape:
        return {"ok": True, "changed": True, "diff": 1.0, "verdict": "changed", "regions": [],
                "blocks_changed": 0, "text": "Пропорции снимка изменились — экран обновился."}

    delta = np.abs(a - b) / 255.0
    changed_pixels = delta > pixel_delta
    diff = float(changed_pixels.mean())

    # блочный анализ: ловим локальные изменения (текст, иконки, поп-апы)
    h, w = delta.shape
    regions: List[Dict[str, Any]] = []
    blocks_changed = 0
    for by in range(0, max(1, h - block + 1), block):
        for bx in range(0, max(1, w - block + 1), block):
            part = delta[by:by + block, bx:bx + block]
            if part.size == 0:
                continue
            ratio = float((part > pixel_delta).mean())
            if ratio >= block_ratio:             # блок «реально» изменился
                blocks_changed += 1
                regions.append({"xy": [bx, by], "ratio": round(ratio, 3)})

    if blocks_changed == 0 or diff < noise_floor:
        verdict = "same"
    elif diff < 0.002:
        verdict = "micro"
    elif diff < 0.03:
        verdict = "small"
    else:
        verdict = "changed"

    texts = {
        "same": (f"Снимок до и после действия практически не изменился "
                 f"(различие {diff * 100:.3f}%). Похоже, действие не сработало."),
        "micro": (f"Экран изменился совсем немного ({diff * 100:.3f}%, блоков: {blocks_changed}) — "
                  f"возможно, реакция на действие (подсветка, курсор), а возможно и ничего."),
        "small": (f"Интерфейс отреагировал: изменилось {diff * 100:.2f}% пикселей "
                  f"в {blocks_changed} областях — например, появилось сообщение или обновился элемент."),
        "changed": (f"Экран заметно изменился ({diff * 100:.1f}% пикселей, областей: {blocks_changed})"
                    + (f", в том числе {regions[0]['xy']}" if regions else "") + "."),
    }
    return {
        "ok": True,
        "changed": verdict != "same",
        "verdict": verdict,
        "diff": round(diff, 6),
        "blocks_changed": blocks_changed,
        "regions": regions[:12],
        "before": str(before),
        "after": str(after),
        "text": texts[verdict],
    }


def signature(path: str | Path, size: int = 192) -> str:
    """Быстрый «отпечаток» снимка — для ожиданий и кэша."""
    try:
        gray = _thumb_gray(path, size)
    except Exception:  # noqa: BLE001
        return ""
    return hashlib.sha256(gray.astype(np.uint8).tobytes()).hexdigest()[:16]


def image_preview_data_uri(path: str | Path, max_px: int = 1100, quality: int = 74) -> Optional[str]:
    """JPEG-preview для UI и модели (data-URI)."""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            if max(img.size) > max_px:
                img.thumbnail((max_px, max_px))
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=quality, optimize=True)
            import base64

            return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        print(f"[grounding] превью не удалось: {exc}")
        return None
