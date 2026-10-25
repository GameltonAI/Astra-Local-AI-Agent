"""Тесты продвинутого зрения: координатная сетка, Set-of-Mark, проверка результата.

Проверяем то, из-за чего агент раньше «промахивался»: точные координаты кликов,
разметка элементов, сравнение снимков до/после и работа с динамикой (загрузка,
поп-апы, бесконечный скролл) и капчей.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRA_DATA_DIR", "/tmp/astra_pytest_data")

from PIL import Image, ImageDraw  # noqa: E402

from astra import grounding  # noqa: E402
from astra.tools import window_control  # noqa: E402
from astra.tools import vision_tools as vt  # noqa: E402


# ----------------------------------------------------------------------
#  Фикстуры: синтетический «интерфейс»
# ----------------------------------------------------------------------
def make_ui(path: Path, buttons=((40, 40, 180, 80), (220, 40, 380, 80), (40, 120, 300, 160)),
            bg=(24, 26, 34), fg=(235, 238, 245)) -> Path:
    """Нарисовать «окно» с несколькими кнопками-прямоугольниками."""
    img = Image.new("RGB", (800, 600), bg)
    draw = ImageDraw.Draw(img)
    for (x0, y0, x1, y1) in buttons:
        draw.rectangle([x0, y0, x1, y1], fill=(56, 60, 78), outline=fg, width=2)
        draw.text((x0 + 12, y0 + 14), "Button", fill=fg)
    draw.rectangle([40, 220, 760, 300], outline=fg, width=2)   # поле ввода
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def make_flat(path: Path, color=(24, 26, 34)) -> Path:
    Image.new("RGB", (800, 600), color).save(path)
    return path


def fake_window(title: str = "Тестовое окно", client_offset: int = 40) -> window_control.WindowInfo:
    return window_control.WindowInfo(
        wid="101", title=title, app="test", pid=1,
        left=0, top=0, width=800, height=600,
        client_left=0, client_top=client_offset, client_width=800, client_height=600 - client_offset,
    )


@pytest.fixture()
def patch_window(monkeypatch, tmp_path):
    """Подменить оконный слой: снимок = заранее нарисованная картинка."""
    state = {"image": make_ui(tmp_path / "ui.png"), "captures": [], "clicks": []}
    window = fake_window()

    def capture(win, path):
        state["captures"].append(str(path))
        src = state["image"]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(Path(src).read_bytes())
        with Image.open(src) as img:
            return str(path), img.size

    monkeypatch.setattr(window_control, "capture_window", capture)
    monkeypatch.setattr(window_control, "find_window", lambda query="", index=0: window)
    monkeypatch.setattr(window_control, "focus_window", lambda query="", index=0: window)
    monkeypatch.setattr(window_control, "client_point", lambda win, x, y: (win.left + x, win.top + y))
    monkeypatch.setattr(vt, "_LAST_MARKS", {})
    monkeypatch.setattr(vt, "_BASELINE", {})
    return state


# ======================================================================
#  Сетка и метки
# ======================================================================
def test_auto_step_is_readable():
    assert grounding.auto_step(1920, 1080) == 100
    assert grounding.auto_step(800, 600) == 50
    assert grounding.auto_step(4000, 3000) == 300


def test_detect_regions_finds_buttons(tmp_path):
    image = Image.open(make_ui(tmp_path / "ui.png")).convert("RGB")
    marks = grounding.detect_regions(image, max_marks=20)
    assert marks, "должны найтись элементы интерфейса"
    centers = [((m.box[0] + m.box[2]) // 2, (m.box[1] + m.box[3]) // 2) for m in marks]
    # каждая нарисованная кнопка должна быть накрыта какой-то меткой
    for expected in [(110, 60), (300, 60), (170, 140)]:
        assert any(abs(cx - expected[0]) < 90 and abs(cy - expected[1]) < 60 for cx, cy in centers), expected
    # метки пронумерованы по порядку чтения
    assert [m.id for m in marks] == list(range(1, len(marks) + 1))


def test_detect_regions_skips_blank_image(tmp_path):
    image = Image.open(make_flat(tmp_path / "flat.png")).convert("RGB")
    assert grounding.detect_regions(image) == []


def test_detect_regions_respects_limit(tmp_path):
    image = Image.open(make_ui(tmp_path / "ui.png")).convert("RGB")
    marks = grounding.detect_regions(image, max_marks=2)
    assert len(marks) <= 2


def test_grid_is_drawn_with_labels(tmp_path):
    image = Image.open(make_flat(tmp_path / "flat.png")).convert("RGB")
    canvas, meta = grounding.annotate_image(image, marks=[], step=100, mode="grid")
    arr = np.asarray(canvas)
    assert meta["step"] == 100
    # на пустом фоне появились цветные линии сетки
    assert not np.array_equal(arr, np.asarray(image))
    # линия сетки проходит по x=100 (цвет близок к GRID_COLOR)
    column = arr[:, 100, :].astype(int)
    grid = np.array(grounding.GRID_COLOR)
    assert (np.abs(column - grid).sum(axis=1) < 240).any()


def test_marks_are_numbered_on_image(tmp_path):
    image = Image.open(make_ui(tmp_path / "ui.png")).convert("RGB")
    marks = grounding.detect_regions(image, max_marks=5)
    canvas, meta = grounding.annotate_image(image, marks=marks, mode="marks")
    assert len(meta["marks"]) == len(marks)
    arr = np.asarray(canvas).astype(int)
    # вокруг центра первой метки есть цвет рамки
    x0, y0, x1, y1 = marks[0].box
    patch = arr[y0:y1 + 1, x0:x1 + 1]
    colors = {tuple(c) for c in patch.reshape(-1, 3)[::7].tolist()}
    assert any(any(abs(c[i] - grounding.MARK_COLORS[0][i]) < 70 for i in range(3)) for c in colors)


def test_origin_shifts_click_coordinates(tmp_path):
    """Клик по метке должен быть в координатах содержимого окна, а не снимка целиком."""
    source = make_ui(tmp_path / "ui.png")
    window = fake_window(client_offset=40)
    annotation = grounding.annotate_file(source, tmp_path / "out.png", mode="both",
                                         window=window, title="Тест")
    assert annotation.origin == (0, 40)
    for mark in annotation.marks:
        image_center_y = (mark.box[1] + mark.box[3]) // 2
        assert mark.click[1] == image_center_y - 40
    assert Path(annotation.path).exists()


def test_describe_mentions_grid_and_marks(tmp_path):
    source = make_ui(tmp_path / "ui.png")
    annotation = grounding.annotate_file(source, tmp_path / "out.png", mode="both",
                                        window=fake_window(), title="Chrome")
    text = grounding.describe(annotation)
    assert "Сетка" in text and "Метки" in text
    assert "click_window_mark" in text
    assert "#1" in text


# ======================================================================
#  Сравнение снимков (верификация результата)
# ======================================================================
def test_compare_detects_no_change(tmp_path):
    a = make_flat(tmp_path / "a.png")
    b = make_flat(tmp_path / "b.png")
    result = grounding.compare(a, b)
    assert result["ok"] and result["changed"] is False
    assert result["verdict"] == "same"
    assert "не изменился" in result["text"]


def test_compare_detects_change(tmp_path):
    a = make_flat(tmp_path / "a.png")
    b = make_ui(tmp_path / "b.png")
    result = grounding.compare(a, b)
    assert result["changed"] is True
    assert result["verdict"] in ("changed", "small")
    assert result["diff"] > 0


def test_compare_tiny_change_is_not_alarming(tmp_path):
    """Пара пикселей (мигающий курсор) не должна считаться изменением."""
    a = make_ui(tmp_path / "a.png")
    img = Image.open(a).copy()
    ImageDraw.Draw(img).rectangle([600, 500, 602, 505], fill=(255, 0, 0))
    b = tmp_path / "b.png"
    img.save(b)
    result = grounding.compare(a, b)
    assert result["verdict"] == "same"
    assert result["changed"] is False


def test_compare_detects_status_text_line(tmp_path):
    """Главный случай из жизни: после клика появилась строка «Сохранено» — это изменение."""
    a = make_flat(tmp_path / "a.png")
    img = Image.open(a).copy()
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 520, 260, 540], fill=(58, 208, 192))   # «текст» внизу окна
    b = tmp_path / "b.png"
    img.save(b)
    result = grounding.compare(a, b)
    assert result["changed"] is True
    assert result["verdict"] in ("micro", "small")
    assert result["blocks_changed"] >= 1


def test_compare_ignores_noise_floor(tmp_path, monkeypatch):
    from astra.config import config

    monkeypatch.setitem(config._data["agent"], "verify_min_diff", 0.5)  # заведомо недостижимый порог
    a = make_flat(tmp_path / "a.png")
    b = make_ui(tmp_path / "b.png")
    assert grounding.compare(a, b)["verdict"] == "same"


def test_compare_handles_size_change(tmp_path):
    a = make_flat(tmp_path / "a.png")
    b = tmp_path / "b.png"
    Image.new("RGB", (400, 300), (24, 26, 34)).save(b)
    result = grounding.compare(a, b)
    assert result["changed"] is True


def test_compare_survives_broken_file(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    result = grounding.compare(bad, bad)
    assert result["ok"] is False


def test_signature_is_stable(tmp_path):
    a = make_flat(tmp_path / "a.png")
    b = make_flat(tmp_path / "b.png")
    c = make_ui(tmp_path / "c.png")
    assert grounding.signature(a) == grounding.signature(b)
    assert grounding.signature(a) != grounding.signature(c)


# ======================================================================
#  Инструменты: see_window и клик по метке
# ======================================================================
def test_see_window_returns_marks_and_artifact(patch_window):
    result = vt.see_window(title="", mode="both")
    assert "Метки" in result["text"] and "Сетка" in result["text"]
    assert "click_window_mark" in result["text"]
    assert result["artifact"]["data_uri"].startswith("data:image/jpeg;base64,")
    marks = vt.last_marks(fake_window())
    assert marks, "метки должны запомниться для клика"


def test_see_window_grid_only(patch_window):
    result = vt.see_window(title="", mode="grid")
    assert "Сетка" in result["text"]
    assert "Метки:" not in result["text"]
    assert vt.last_marks(fake_window()) == []


def test_click_window_mark_uses_mark_coordinates(patch_window, monkeypatch):
    vt.see_window(title="")
    marks = vt.last_marks(fake_window())
    target = marks[0]
    clicked = {}

    def fake_click(button="left", clicks=1, x=None, y=None):
        clicked.update(button=button, clicks=clicks, x=x, y=y)
        return "клик выполнен"

    monkeypatch.setattr(vt.os_control, "mouse_click", fake_click)
    text = vt.click_window_mark(title="", mark=target.id)
    assert f"#{target.id}" in text and f"({target.click[0]}, {target.click[1]})" in text
    assert clicked["x"] == target.click[0] and clicked["y"] == target.click[1]


def test_click_window_mark_requires_marks(patch_window):
    with pytest.raises(vt.ToolError) as exc:
        vt.click_window_mark(title="", mark=1)
    assert "see_window" in str(exc.value)


def test_click_window_mark_unknown_number(patch_window):
    vt.see_window(title="")
    with pytest.raises(vt.ToolError) as exc:
        vt.click_window_mark(title="", mark=999)
    assert "Доступные метки" in str(exc.value)


def test_click_window_mark_rejects_out_of_window(patch_window, monkeypatch):
    vt.see_window(title="")
    monkeypatch.setitem(vt._LAST_MARKS, "101", ("Тестовое окно",
                                               [grounding.Mark(id=1, box=(0, 0, 10, 10), click=(0, -50))]))
    with pytest.raises(vt.ToolError) as exc:
        vt.click_window_mark(title="", mark=1)
    assert "вне области" in str(exc.value)


# ======================================================================
#  Динамика: ожидание изменений, поп-апы, скролл
# ======================================================================
def _shot_sequence(monkeypatch, tmp_path, images):
    """Подменить снимок окна последовательностью картинок (по файлу на вызов)."""
    seq = [Path(src).read_bytes() for src in images]
    state = {"i": 0, "made": 0}

    def fake_shot(window, prefix="see"):
        index = min(state["i"], len(seq) - 1)
        state["i"] += 1
        state["made"] += 1
        path = tmp_path / f"shot_{state['made']}.png"
        path.write_bytes(seq[index])
        return path

    monkeypatch.setattr(vt, "_shot", fake_shot)
    return state


def test_wait_for_change_detects_reaction(patch_window, monkeypatch, tmp_path):
    flat = make_flat(tmp_path / "flat.png")
    ui = make_ui(tmp_path / "ui.png")
    _shot_sequence(monkeypatch, tmp_path, [flat, flat, ui])
    result = vt.wait_for_change(title="", timeout=3, interval=0.05)
    assert "изменился" in result["text"]
    assert result["change"]["changed"] is True
    assert result["artifact"]


def test_wait_for_change_sees_reaction_that_already_happened(patch_window, monkeypatch, tmp_path):
    """Клик сработал, а wait_for_change вызвали позже — сравнение идёт со снимком «до»."""
    flat = make_flat(tmp_path / "flat.png")
    ui = make_ui(tmp_path / "ui.png")
    # «до действия» — пустой экран; всё, что сняли после, уже показывает результат
    baseline = thread_temp(tmp_path / "baseline.png", flat)
    vt.set_baseline(baseline, fake_window())
    _shot_sequence(monkeypatch, tmp_path, [ui, ui, ui])
    result = vt.wait_for_change(title="", timeout=2, interval=0.05)
    assert "изменился" in result["text"]
    assert result["change"]["changed"] is True


def thread_temp(path: Path, source: Path) -> Path:
    Path(path).write_bytes(Path(source).read_bytes())
    return path


def test_click_window_mark_stores_baseline_before_click(patch_window, monkeypatch, tmp_path):
    vt.see_window(title="")
    monkeypatch.setattr(vt, "_BASELINE", {})
    seen = {}

    def fake_click(button="left", clicks=1, x=None, y=None):
        seen["baseline_before_click"] = vt.get_baseline(fake_window()) is not None
        return "клик"

    monkeypatch.setattr(vt.os_control, "mouse_click", fake_click)
    vt.click_window_mark(title="", mark=1)
    assert seen["baseline_before_click"] is True


def test_wait_for_change_reports_timeout(patch_window, monkeypatch, tmp_path):
    flat = make_flat(tmp_path / "flat.png")
    _shot_sequence(monkeypatch, tmp_path, [flat, flat, flat, flat])
    result = vt.wait_for_change(title="", timeout=1, interval=0.05)
    assert "не изменился" in result["text"]
    assert "не сработало" in result["text"]


def test_wait_until_stable(patch_window, monkeypatch, tmp_path):
    flat = make_flat(tmp_path / "flat.png")
    _shot_sequence(monkeypatch, tmp_path, [flat, flat, flat, flat, flat])
    result = vt.wait_until_stable(title="", timeout=3, quiet_ms=200)
    assert "загрузка завершена" in result["text"]


def test_scroll_until_end_stops_when_content_exhausted(patch_window, monkeypatch, tmp_path):
    a = make_flat(tmp_path / "a.png")
    b = make_ui(tmp_path / "b.png")
    _shot_sequence(monkeypatch, tmp_path, [a, b, b, b])
    monkeypatch.setattr(vt.os_control, "mouse_move", lambda x, y: None)
    monkeypatch.setattr(vt.os_control, "mouse_scroll", lambda amount: "прокручено")
    result = vt.scroll_until_end(title="", amount=-5, max_steps=6, pause=0.01)
    assert result["reached_end"] is True
    assert result["steps"] >= 1


def test_scroll_until_end_keeps_scrolling_when_content_grows(patch_window, monkeypatch, tmp_path):
    images = []
    for index in range(7):
        img = Image.new("RGB", (800, 600), (24, 26, 34))
        draw = ImageDraw.Draw(img)
        # каждый «экран» — заметно другая раскладка карточек
        for row in range(3):
            top = 20 + ((row + index) % 5) * 100
            draw.rectangle([20, top, 780, top + 80], fill=(60 + index * 20, 70, 90))
        path = tmp_path / f"{index}.png"
        img.save(path)
        images.append(path)
    _shot_sequence(monkeypatch, tmp_path, images)
    monkeypatch.setattr(vt.os_control, "mouse_move", lambda x, y: None)
    monkeypatch.setattr(vt.os_control, "mouse_scroll", lambda amount: "ок")
    result = vt.scroll_until_end(title="", max_steps=3, pause=0.01)
    assert result["reached_end"] is False
    assert result["steps"] == 3


def test_wait_for_new_window(patch_window, monkeypatch):
    base = fake_window("Chrome")
    dialog = window_control.WindowInfo(wid="202", title="Подтвердите действие", app="chrome",
                                       width=420, height=200)
    calls = {"n": 0}

    def fake_list(include_hidden=False):
        calls["n"] += 1
        return [base] if calls["n"] < 2 else [base, dialog]

    monkeypatch.setattr(vt.window_control, "list_windows", fake_list)
    monkeypatch.setattr(vt.time, "sleep", lambda s: None)
    text = vt.wait_for_new_window(title="", timeout=3)
    assert "Подтвердите действие" in text


def test_wait_for_new_window_marks_captcha(patch_window, monkeypatch):
    base = fake_window("Chrome")
    captcha = window_control.WindowInfo(wid="303", title="reCAPTCHA: проверка", app="chrome",
                                        width=400, height=300)
    calls = {"n": 0}

    def fake_list(include_hidden=False):
        calls["n"] += 1
        return [base] if calls["n"] < 2 else [base, captcha]

    monkeypatch.setattr(vt.window_control, "list_windows", fake_list)
    monkeypatch.setattr(vt.time, "sleep", lambda s: None)
    text = vt.wait_for_new_window(title="", timeout=3)
    assert "капч" in text.lower()


@pytest.mark.parametrize("title", [
    "Google — подтвердите, что вы не робот",
    "Cloudflare — Checking your browser",
    "hCaptcha",
    "Вход — надо пройти капчу",
])
def test_find_captcha_detects_markers(title):
    found = vt.find_captcha([window_control.WindowInfo(wid="1", title=title)])
    assert found and found[0]["window"] == title


def test_find_captcha_clean_window():
    assert vt.find_captcha([window_control.WindowInfo(wid="1", title="Документ — Word")]) == []


def test_detect_captcha_tool_text(patch_window, monkeypatch):
    monkeypatch.setattr(vt.window_control, "list_windows",
                        lambda include_hidden=False: [window_control.WindowInfo(
                            wid="9", title="Проверка: Вы человек?")])
    text = vt.detect_captcha()
    assert "капч" in text.lower()
    assert "пользовател" in text.lower()


def test_detect_captcha_tool_when_clean(patch_window, monkeypatch):
    monkeypatch.setattr(vt.window_control, "list_windows",
                        lambda include_hidden=False: [window_control.WindowInfo(wid="9", title="Word")])
    assert "не видно" in vt.detect_captcha()


# ======================================================================
#  Агент: проверка результата и остановка на капче
# ======================================================================
def _worker(tmp_path):
    """Импорт агента с изолированным каталогом скриншотов."""
    from astra.agent import Agent
    import astra.paths as paths

    monkey_root = tmp_path / "shots"
    monkey_root.mkdir(exist_ok=True)
    paths.SCREENSHOT_DIR = monkey_root
    return Agent, monkey_root


@pytest.mark.asyncio
async def test_agent_verification_flags_unchanged_screen(tmp_path, monkeypatch):
    Agent, shots = _worker(tmp_path)
    flat = make_flat(shots / "flat.png")

    def capture(win, path):
        Path(path).write_bytes(flat.read_bytes())
        return str(path), (800, 600)

    monkeypatch.setattr(window_control, "capture_window", capture)
    monkeypatch.setattr(window_control, "find_window", lambda query="", index=0: fake_window())
    events = []

    async def emit(event):
        events.append(event)

    agent = Agent(emit=emit)
    executed = [{"ok": True, "tool": "click_in_window", "args": {"title": "x"}, "result": "клик"}]
    before = str(flat)
    await agent._verify_change(before, {"title": "x"}, executed)

    assert executed[-1]["verified"] == "same"
    assert "НЕ изменился" in executed[-1]["result"]
    assert any(e["type"] == "toast" and e.get("level") == "warn" for e in events)


@pytest.mark.asyncio
async def test_agent_verification_accepts_changed_screen(tmp_path, monkeypatch):
    Agent, shots = _worker(tmp_path)
    flat = make_flat(shots / "flat.png")
    ui = make_ui(shots / "ui.png")

    def capture(win, path):
        Path(path).write_bytes(ui.read_bytes())
        return str(path), (800, 600)

    monkeypatch.setattr(window_control, "capture_window", capture)
    monkeypatch.setattr(window_control, "find_window", lambda query="", index=0: fake_window())
    events = []

    async def emit(event):
        events.append(event)

    agent = Agent(emit=emit)
    executed = [{"ok": True, "tool": "click_in_window", "args": {"title": "x"}, "result": "клик"}]
    await agent._verify_change(str(flat), {"title": "x"}, executed)
    assert executed[-1].get("verified") in ("changed", "small")
    assert "Проверка по снимку" in executed[-1]["result"]


@pytest.mark.asyncio
async def test_agent_stops_on_captcha(tmp_path, monkeypatch):
    Agent, _ = _worker(tmp_path)
    monkeypatch.setattr(vt.window_control, "list_windows",
                        lambda include_hidden=False: [window_control.WindowInfo(
                            wid="1", title="Подтвердите, что вы не робот")])
    events = []

    async def emit(event):
        events.append(event)

    agent = Agent(emit=emit)
    executed = [{"ok": True, "tool": "click_in_window", "args": {}, "result": "клик"}]
    stopped = await agent._check_captcha(executed, {})
    assert stopped is True
    assert executed[-1]["captcha"] is True
    assert any(e["type"] == "toast" for e in events)


@pytest.mark.asyncio
async def test_agent_captcha_can_be_non_blocking(tmp_path, monkeypatch):
    Agent, _ = _worker(tmp_path)
    from astra.config import config

    monkeypatch.setitem(config._data["agent"], "stop_on_captcha", False)
    monkeypatch.setattr(vt.window_control, "list_windows",
                        lambda include_hidden=False: [window_control.WindowInfo(
                            wid="1", title="капча")])
    agent = Agent(emit=lambda e: asyncio.sleep(0))
    assert await agent._check_captcha([{"ok": True}], {}) is False


@pytest.mark.asyncio
async def test_agent_skips_verification_when_disabled(tmp_path, monkeypatch):
    Agent, _ = _worker(tmp_path)
    from astra.config import config

    monkeypatch.setitem(config._data["agent"], "visual_verify", False)
    agent = Agent(emit=lambda e: asyncio.sleep(0))
    assert agent._verify_snapshot({"title": "x"}) == ""


import asyncio  # noqa: E402  (нужен для async-тестов выше)
