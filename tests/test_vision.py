"""Тесты зрения агента и исправления ввода многострочного текста."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRA_DATA_DIR", "/tmp/astra_pytest_data")

from astra import vision as v  # noqa: E402
from astra.tools.os_control import text_tokens  # noqa: E402


# ======================================================================
#  Ввод текста: переводы строк и табуляция (баг «всё слитно в блокноте»)
# ======================================================================
def test_newline_becomes_enter_key():
    tokens = text_tokens("1. раз\n2. два")
    assert ("key", "enter") in tokens
    # сам символ \n как unicode-событие отправляться не должен
    assert ("unicode", 10) not in tokens
    assert tokens.count(("key", "enter")) == 1


def test_crlf_is_single_enter():
    tokens = text_tokens("a\r\nb")
    assert [t for t in tokens if t[0] == "key"] == [("key", "enter")]


def test_tab_becomes_tab_key():
    assert ("key", "tab") in text_tokens("a\tb")


def test_spaces_and_cyrillic_preserved():
    tokens = text_tokens("привет мир")
    units = [val for kind, val in tokens if kind == "unicode"]
    assert units == [ord(ch) for ch in "привет мир"]


def test_emoji_uses_surrogate_pair():
    units = [val for kind, val in text_tokens("😀") if kind == "unicode"]
    assert len(units) == 2
    assert units[0] == 0xD83D and units[1] == 0xDE00


def test_multiline_list_keeps_line_breaks():
    text = "1. погулять\n2. вкусно покушать\n3. Поспать"
    tokens = text_tokens(text)
    assert tokens.count(("key", "enter")) == 2
    rebuilt = "".join(chr(val) for kind, val in tokens if kind == "unicode")
    assert rebuilt == text.replace("\n", "")


# ======================================================================
#  Зрение: сборка мультимодальных сообщений
# ======================================================================
def test_attach_images_wraps_text_and_image():
    messages = [
        {"role": "system", "content": "ты ассистент"},
        {"role": "user", "content": "что на экране?"},
    ]
    image = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}, "meta": {"name": "a.png"}}
    prepared = v.attach_images(messages, [image], caption="снимок окна Chrome")
    content = prepared[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text" and "что на экране?" in content[0]["text"]
    assert "Chrome" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    # исходные сообщения не мутируются
    assert messages[-1]["content"] == "что на экране?"


def test_attach_images_respects_limit(monkeypatch):
    from astra.config import config

    monkeypatch.setattr(config, "get", lambda key, default=None: 1 if key == "lmstudio.vision_max_images" else default)
    image = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}}
    prepared = v.attach_images([{"role": "user", "content": "текст"}], [image, image, image])
    assert sum(1 for part in prepared[-1]["content"] if part["type"] == "image_url") == 1


def test_has_images_and_strip_images():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "смотри"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
    ]}]
    assert v.has_images(messages) is True
    stripped = v.strip_images(messages)
    assert v.has_images(stripped) is False
    assert "смотри" in stripped[0]["content"]
    assert "не умеет смотреть" in stripped[0]["content"]


def test_has_images_false_for_plain_messages():
    assert v.has_images([{"role": "user", "content": "текст"}]) is False


@pytest.mark.parametrize("error,expected", [
    ("400: model does not support image input", True),
    ("image_url is not supported", True),
    ("vision not available", True),
    ("400: context length exceeded", False),
    ("LM Studio вернул 500: boom", False),
])
def test_vision_error_detection(error, expected):
    assert v.looks_like_vision_error(error) is expected


def test_image_to_part_compresses(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    path = tmp_path / "big.png"
    Image.new("RGB", (4000, 3000), (120, 80, 255)).save(path)
    part = v.image_to_part(path, max_px=512, quality=70)
    assert part is not None
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert part["meta"]["width"] == 512 and part["meta"]["height"] == 384


def test_image_to_part_missing_file(tmp_path):
    assert v.image_to_part(tmp_path / "nope.png") is None


def test_artifact_to_part_uses_data_uri():
    artifact = {"kind": "image", "name": "shot.jpg", "path": "/tmp/x.jpg",
                "data_uri": "data:image/jpeg;base64,ZZZ"}
    part = v.artifact_to_part(artifact)
    assert part["image_url"]["url"].endswith("ZZZ")
    assert part["meta"]["name"] == "shot.jpg"
    assert v.artifact_to_part(None) is None


def test_vision_state_modes(monkeypatch):
    from astra.config import config

    state = v.VisionState()

    monkeypatch.setattr(config, "get", lambda key, default=None: "off" if key == "lmstudio.vision" else default)
    assert state.enabled() is False

    monkeypatch.setattr(config, "get", lambda key, default=None: "on" if key == "lmstudio.vision" else default)
    assert state.enabled() is True

    monkeypatch.setattr(config, "get", lambda key, default=None: "auto" if key == "lmstudio.vision" else default)
    assert state.enabled() is True          # auto и ещё не проверяли
    state.mark_unsupported("does not support image input")
    assert state.enabled() is False         # после ошибки — только текст
    state.mark_sent()
    assert state.enabled() is True and state.supported is True


def test_build_test_image():
    pytest.importorskip("PIL")
    part = v.build_test_image()
    assert part is not None
    assert part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert part["meta"]["width"] == 320


# ======================================================================
#  Агент: авто-снимок окна и фолбэк
# ======================================================================
@pytest.mark.asyncio
async def test_agent_attaches_images_and_falls_back(monkeypatch):
    from astra import agent as agent_module
    from astra.config import config

    monkeypatch.setitem(config.as_dict(), "lmstudio", {**config.get("lmstudio"), "model": "fake"})
    monkeypatch.setattr(config, "get", lambda key, default=None: (
        "auto" if key == "lmstudio.vision" else
        "fake" if key == "lmstudio.model" else default))

    calls = {"with_images": 0, "without_images": 0}

    async def fake_chat(messages, **kwargs):
        if v.has_images(messages):
            calls["with_images"] += 1
            raise agent_module.LLMError("400: this model does not support image input")
        calls["without_images"] += 1
        return {"content": '{"thought": "ok", "action": "final"}'}

    monkeypatch.setattr(agent_module.llm, "chat", fake_chat)

    events = []

    async def emit(event):
        events.append(event)

    ag = agent_module.Agent(emit=emit)
    ag._images = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}}]
    v.vision_state.reset()

    reply = await ag._chat_with_vision([{"role": "user", "content": "что на экране?"}])
    assert calls["with_images"] == 1
    assert calls["without_images"] == 1          # повторили без картинок
    assert v.vision_state.supported is False
    assert any(e.get("type") == "toast" for e in events)
    assert "thought" in reply["content"]


@pytest.mark.asyncio
async def test_agent_keeps_images_when_model_supports(monkeypatch):
    from astra import agent as agent_module
    from astra.config import config

    monkeypatch.setattr(config, "get", lambda key, default=None: "auto" if key == "lmstudio.vision" else default)

    async def fake_chat(messages, **kwargs):
        assert v.has_images(messages)
        return {"content": "вижу браузер"}

    monkeypatch.setattr(agent_module.llm, "chat", fake_chat)

    async def emit(event):
        return None

    ag = agent_module.Agent(emit=emit)
    ag._images = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}}]
    v.vision_state.reset()
    reply = await ag._chat_with_vision([{"role": "user", "content": "что видно?"}])
    assert reply["content"] == "вижу браузер"
    assert v.vision_state.supported is True


def test_auto_window_screenshot_setting_default():
    from astra.config import DEFAULTS

    assert DEFAULTS["agent"]["auto_window_screenshot"] is True
    assert DEFAULTS["lmstudio"]["vision"] == "auto"


@pytest.mark.asyncio
async def test_attach_window_snapshot_adds_image_and_updates_card(monkeypatch):
    """После действия в окне агент сам прикладывает снимок и помечает карточку."""
    from astra import agent as agent_module
    from astra.config import config

    monkeypatch.setattr(config, "get", lambda key, default=None: {
        "agent.auto_window_screenshot": True,
        "lmstudio.vision": "on",
        "lmstudio.vision_max_images": 2,
    }.get(key, default))

    artifact = {"kind": "image", "name": "window_Chrome.png", "path": "/tmp/window_Chrome.png",
                "data_uri": "data:image/jpeg;base64,AAAA"}
    calls = {}

    class FakeRegistry:
        async def execute(self, name, args, timeout=60):
            calls["tool"] = name
            calls["args"] = args
            return {"ok": True, "tool": name, "args": args,
                    "result": "Снимок окна сохранён", "artifact": artifact}

    events = []

    async def emit(event):
        events.append(event)

    ag = agent_module.Agent(emit=emit)
    executed = [{"ok": True, "tool": "type_in_window", "result": "напечатано"}]
    await ag._attach_window_snapshot(FakeRegistry(), {"title": "chrome"}, executed, "abc123")

    assert calls["tool"] == "screenshot_window"
    assert calls["args"] == {"title": "chrome"}
    assert len(ag._images) == 1
    assert ag._images[0]["type"] == "image_url"
    assert "chrome" in ag._vision_caption
    assert executed[-1]["vision_sent"] is True
    assert executed[-1]["artifact"] is artifact
    assert any(e.get("type") == "action_result" and e.get("vision_sent") for e in events)


@pytest.mark.asyncio
async def test_attach_window_snapshot_respects_switch(monkeypatch):
    from astra import agent as agent_module
    from astra.config import config

    monkeypatch.setattr(config, "get", lambda key, default=None: {
        "agent.auto_window_screenshot": False,
    }.get(key, default))

    class BoomRegistry:
        async def execute(self, *a, **k):
            raise AssertionError("снимок не должен делаться")

    ag = agent_module.Agent(emit=lambda e: asyncio.sleep(0))
    await ag._attach_window_snapshot(BoomRegistry(), {"title": "chrome"}, [], "id")
    assert ag._images == []


@pytest.mark.asyncio
async def test_attach_window_snapshot_keeps_only_last_images(monkeypatch):
    from astra import agent as agent_module
    from astra.config import config

    monkeypatch.setattr(config, "get", lambda key, default=None: {
        "agent.auto_window_screenshot": True,
        "lmstudio.vision": "on",
        "lmstudio.vision_max_images": 2,
    }.get(key, default))

    artifact = {"kind": "image", "name": "s.png", "data_uri": "data:image/jpeg;base64,QQ"}

    class FakeRegistry:
        async def execute(self, name, args, timeout=60):
            return {"ok": True, "artifact": artifact, "result": ""}

    ag = agent_module.Agent(emit=lambda e: asyncio.sleep(0))
    for i in range(4):
        await ag._attach_window_snapshot(FakeRegistry(), {"title": f"w{i}"}, [], "id")
    assert len(ag._images) == 2
