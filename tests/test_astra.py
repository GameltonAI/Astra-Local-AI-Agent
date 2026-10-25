"""Тесты разбора решений модели, конфига, инструментов и хранилища."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ASTRA_DATA_DIR", "/tmp/astra_pytest_data")

from astra.llm import parse_decision  # noqa: E402
from astra.tools import build_registry  # noqa: E402
from astra.tools.base import ToolError  # noqa: E402


# ------------------------------------------------------------------ парсинг ---
def test_parse_plain_actions():
    raw = '{"thought": "открываю", "actions": [{"tool": "open_app", "args": {"name": "chrome"}}]}'
    result = parse_decision(raw)
    assert result["thought"] == "открываю"
    assert result["actions"] == [{"tool": "open_app", "args": {"name": "chrome"}}]


def test_parse_fenced_json():
    raw = 'Думаю...\n```json\n{"actions": [{"tool": "press_key", "args": {"key": "enter"}}]}\n```\n'
    result = parse_decision(raw)
    assert result["actions"][0]["tool"] == "press_key"


def test_parse_with_surrounding_text():
    raw = ('Я решил, что нужно сделать так:\n{"tool": "type_text", "args": {"text": "привет"}}\n'
           "и это сработает.")
    result = parse_decision(raw)
    assert result["actions"][0] == {"tool": "type_text", "args": {"text": "привет"}}


def test_parse_final_action_yields_no_tools():
    for raw in ('{"thought": "готово", "action": "final"}',
                '{"actions": []}',
                '{"tool": "none"}'):
        assert parse_decision(raw)["actions"] == []


def test_parse_openai_function_shape():
    raw = json.dumps({"tool_calls": [
        {"function": {"name": "open_url", "arguments": '{"url": "yandex.ru"}'}}
    ]})
    result = parse_decision(raw)
    assert result["actions"] == [{"tool": "open_url", "args": {"url": "yandex.ru"}}]


def test_parse_action_as_string_and_args_at_top_level():
    result = parse_decision('{"action": "mouse_scroll", "amount": 3}')
    assert result["actions"] == [{"tool": "mouse_scroll", "args": {"amount": 3}}]


def test_parse_plain_text_returns_no_actions():
    assert parse_decision("Просто расскажи мне анекдот.")["actions"] == []


def test_parse_broken_json_does_not_crash():
    assert parse_decision('{"actions": [{"tool": ')["actions"] == []


def test_parse_nested_args_string():
    raw = '{"actions": [{"tool": "hotkey", "args": "{\\"keys\\": [\\"ctrl\\", \\"s\\"]}"}]}'
    assert parse_decision(raw)["actions"][0]["args"] == {"keys": ["ctrl", "s"]}


# --------------------------------------------------------------- инструменты ---
@pytest.fixture(scope="module")
def registry():
    return build_registry()


def test_registry_catalog_not_empty(registry):
    assert len(registry.all()) >= 20
    catalog = registry.catalog()
    assert "open_app(" in catalog and "run_command(" in catalog


def test_prepare_coerces_types(registry):
    prepared = registry.prepare("mouse_click", {"button": "left", "clicks": "2", "x": "100", "y": 200})
    assert prepared["kwargs"]["clicks"] == 2
    assert prepared["kwargs"]["x"] == 100


def test_prepare_rejects_unknown_args(registry):
    with pytest.raises(ToolError):
        registry.prepare("open_app", {"name": "chrome", "bogus": 1})


def test_prepare_requires_mandatory(registry):
    with pytest.raises(ToolError):
        registry.prepare("open_url", {})


def test_prepare_hotkey_list_from_string(registry):
    prepared = registry.prepare("hotkey", {"keys": "ctrl, s"})
    assert prepared["kwargs"]["keys"] == ["ctrl", "s"]


def test_unknown_tool_message_lists_alternatives(registry):
    with pytest.raises(ToolError) as exc:
        registry.prepare("definitely_not_a_tool", {})
    assert "open_app" in str(exc.value)


@pytest.mark.asyncio
async def test_execute_reports_error_without_raising(registry):
    result = await registry.execute("read_text_file", {"path": "/nonexistent/file.txt"})
    assert result["ok"] is False
    assert "не найден" in result["error"].lower()


@pytest.mark.asyncio
async def test_execute_success(registry, tmp_path):
    target = tmp_path / "hello.txt"
    result = await registry.execute("write_text_file", {"path": str(target), "content": "привет"})
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "привет"
    read_back = await registry.execute("read_text_file", {"path": str(target)})
    assert "привет" in read_back["result"]


# ------------------------------------------------------------------ хранилище ---
def test_sessions_lifecycle(tmp_path, monkeypatch):
    from astra.storage import db as db_module
    from astra.storage import sessions as store

    monkeypatch.setattr(db_module, "DB_FILE", tmp_path / "test_astra.db")
    conn = getattr(db_module._local, "conn", None)
    if conn is not None:
        conn.close()
        del db_module._local.conn
    db_module.init_db()
    session = store.create_session(title="Первый чат")
    store.add_message(session["id"], "user", "привет")
    store.add_message(session["id"], "assistant", "здравствуй", meta={"actions": []})

    found = store.list_sessions()
    assert len(found) == 1
    messages = store.list_messages(session["id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]

    store.update_session(session["id"], title="Переименован", pinned=True)
    assert store.get_session(session["id"])["title"] == "Переименован"
    assert store.list_sessions()[0]["pinned"] is True

    markdown = store.export_session_markdown(session["id"])
    assert "Переименован" in markdown and "здравствуй" in markdown

    store.delete_session(session["id"])
    assert store.list_sessions() == []


# --------------------------------------------------------------- утилиты сервера ---
def test_clean_for_speech_strips_markdown():
    from astra.textutils import clean_for_speech

    text = "**Готово!** Вот список:\n- первый\n- второй\n```py\nprint(1)\n```\nСсылка: https://ya.ru"
    spoken = clean_for_speech(text)
    assert "**" not in spoken
    assert "https://" not in spoken
    assert "- " not in spoken
    assert "Готово!" in spoken


def test_config_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_DATA_DIR", str(tmp_path))
    from astra.config import Config

    cfg = Config(path=tmp_path / "config.json")
    cfg.set("lmstudio.model", "qwen2.5-7b-instruct")
    cfg.patch({"voice": {"tts_voice": "ru-RU-SvetlanaNeural"}})

    reloaded = Config(path=tmp_path / "config.json")
    assert reloaded.get("lmstudio.model") == "qwen2.5-7b-instruct"
    assert reloaded.get("voice.tts_voice") == "ru-RU-SvetlanaNeural"
    # значения по умолчанию для незатронутых ключей сохраняются
    # (модель распознавания по умолчанию — большая, large-v3)
    assert reloaded.get("voice.whisper_model") == "large-v3"
