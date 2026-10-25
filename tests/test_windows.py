"""Тесты оконного слоя: поиск окон, координаты, деградация без оконной системы."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRA_DATA_DIR", "/tmp/astra_pytest_data")

from astra.tools import build_registry  # noqa: E402
from astra.tools import window_control as wc  # noqa: E402


def make_window(title="Яндекс — Google Chrome", app="chrome.exe", **kwargs):
    return wc.WindowInfo(wid="1a2b3c", title=title, app=app,
                         left=100, top=50, width=1200, height=800,
                         client_left=108, client_top=90, client_width=1184, client_height=740, **kwargs)


# ------------------------------------------------------------------ модель ---
def test_window_info_reports_geometry():
    info = make_window(window_title if False else "Документ — Word") if False else make_window()
    data = info.as_dict()
    assert data["position"] == [100, 50]
    assert data["size"] == [1200, 800]
    assert "chrome.exe" in info.short()


def test_short_marks_minimized():
    assert "свёрнуто" in make_window(minimized=True).short()


def test_client_point_offsets_to_screen():
    window = make_window()
    assert wc.client_point(window, 0, 0) == (108, 90)
    assert wc.client_point(window, 50, 20) == (158, 110)


def test_window_rect():
    assert wc.window_rect(make_window()) == (100, 50, 1300, 850)


# ------------------------------------------------------------------ поиск ---
@pytest.mark.parametrize("query,expected", [
    ("chrome", True),
    ("Chrome", True),
    ("хром", False),          # в заголовке/процессе нет, но проверяем что не падает
    ("яндекс", True),
    ("", False),
])
def test_score_matching(query, expected):
    window = make_window()
    assert (wc._score(window, query) > 0) is expected


def test_score_prefers_exact_process():
    chrome = make_window(app="chrome.exe")
    notepad = make_window(title="Заметки", app="notepad.exe")
    assert wc._score(chrome, "chrome") > wc._score(notepad, "chrome")


def test_find_window_reports_available_windows(monkeypatch):
    monkeypatch.setattr(wc, "list_windows", lambda include_hidden=False: [make_window()])
    with pytest.raises(wc.WindowError) as exc:
        wc.find_window("блокнот-которого-нет")
    assert "не найдено" in str(exc.value)
    assert "Chrome" in str(exc.value)


def test_find_window_picks_best_match(monkeypatch):
    windows = [
        make_window(title="Проводник — Загрузки", app="explorer.exe"),
        make_window(title="YouTube — Google Chrome", app="chrome.exe"),
    ]
    monkeypatch.setattr(wc, "list_windows", lambda include_hidden=False: windows)
    found = wc.find_window("youtube")
    assert "YouTube" in found.title


def test_find_window_prefers_not_minimized(monkeypatch):
    windows = [
        make_window(title="YouTube — Chrome", app="chrome.exe", minimized=True),
        make_window(title="YouTube — Chrome", app="chrome.exe", minimized=False),
    ]
    monkeypatch.setattr(wc, "list_windows", lambda include_hidden=False: windows)
    assert wc.find_window("youtube").minimized is False


def test_format_windows_text(monkeypatch):
    monkeypatch.setattr(wc, "list_windows", lambda include_hidden=False: [make_window()])
    text = wc.format_windows()
    assert "Открытых окон: 1" in text and "chrome.exe" in text


def test_format_windows_when_unavailable(monkeypatch):
    monkeypatch.setattr(wc, "list_windows", lambda include_hidden=False: [])
    assert "недоступен" in wc.format_windows()


# ------------------------------------------------------- ошибки и деградация ---
def test_focus_window_without_match_raises(monkeypatch):
    monkeypatch.setattr(wc, "list_windows", lambda include_hidden=False: [make_window()])
    with pytest.raises(wc.WindowError):
        wc.focus_window("нет-такого-окна")


def test_open_in_browser_rejects_empty_url():
    with pytest.raises(wc.WindowError):
        wc.open_in_browser("")


# ------------------------------------------------------------------ реестр ---
def test_window_tools_registered():
    registry = build_registry()
    names = {t.name for t in registry.all()}
    expected = {"list_windows", "focus_window", "window_action", "click_in_window",
                "type_in_window", "hotkey_in_window", "scroll_in_window",
                "screenshot_window", "open_in_browser"}
    assert expected.issubset(names)
    assert all(registry.get(n).category == "windows" for n in expected)


def test_window_tools_in_prompt_catalog():
    catalog = build_registry().catalog()
    assert "click_in_window(" in catalog
    assert "Работа в конкретном окне" in catalog


@pytest.mark.asyncio
async def test_window_tool_fails_gracefully_without_windows():
    registry = build_registry()
    result = await registry.execute("focus_window", {"title": "chrome"})
    # В среде без оконной системы инструмент обязан вернуть понятную ошибку, а не упасть
    assert result["ok"] is False
    assert "окон" in result["error"].lower()


@pytest.mark.asyncio
async def test_click_in_window_validates_args():
    registry = build_registry()
    with pytest.raises(Exception):
        registry.prepare("click_in_window", {"title": "chrome", "x": 10})
