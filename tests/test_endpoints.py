"""Тесты выбора сервера LM Studio: приоритет локальной сети и откат на локальный."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRA_DATA_DIR", "/tmp/astra_pytest_data")

from astra.config import config  # noqa: E402
from astra.endpoints import (  # noqa: E402
    EndpointManager,
    EndpointUnavailable,
    kind_label,
    normalize_url,
    url_kind,
)

LAN = "http://192.168.0.32:1234/v1"
LOCAL = "http://127.0.0.1:1234/v1"


# ------------------------------------------------------------- утилиты ---
@pytest.mark.parametrize("raw,expected", [
    ("192.168.0.32:1234", "http://192.168.0.32:1234/v1"),
    ("http://192.168.0.32:1234", "http://192.168.0.32:1234/v1"),
    ("http://192.168.0.32:1234/", "http://192.168.0.32:1234/v1"),
    ("http://192.168.0.32:1234/v1", "http://192.168.0.32:1234/v1"),
    ("http://192.168.0.32:1234/v1/", "http://192.168.0.32:1234/v1"),
    ("https://example.local/lm/v1", "https://example.local/lm/v1"),
    ("", ""),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("url,kind", [
    (LAN, "lan"),
    (LOCAL, "local"),
    ("http://localhost:1234/v1", "local"),
    ("http://[::1]:1234/v1", "local"),
])
def test_url_kind(url, kind):
    assert url_kind(url) == kind


def test_kind_label_is_human_readable():
    assert "сеть" in kind_label(LAN)
    assert "компьютер" in kind_label(LOCAL)


# ------------------------------------------------------- выбор сервера ---
@pytest.fixture()
def manager(monkeypatch):
    """Менеджер с подменённой проверкой доступности."""
    mgr = EndpointManager()
    config.patch({
        "lmstudio": {
            "auto_detect": True,
            "candidates": [LAN, LOCAL],
            "endpoint_ttl": 0,
            "model": "qwen2.5-7b-instruct",
        }
    }, save=False)

    state = {"up": {LAN: True, LOCAL: True}, "models": {LAN: ["qwen2.5-7b-instruct"], LOCAL: ["llama-3.1-8b"]}}

    async def fake_probe(self, url, timeout=None):
        url = normalize_url(url)
        up = state["up"].get(url, False)
        models = [{"id": m, "name": m} for m in state["models"].get(url, [])]
        return {"url": url, "ok": up, "models": models if up else [], "kind": url_kind(url),
                "latency_ms": 3 if up else 0, "error": "" if up else "нет соединения"}

    monkeypatch.setattr(EndpointManager, "probe", fake_probe)
    return mgr, state


@pytest.mark.asyncio
async def test_prefers_lan_when_available(manager):
    mgr, _ = manager
    result = await mgr.resolve(force=True)
    assert result["ok"] is True
    assert result["url"] == LAN
    assert result["kind"] == "lan"
    assert result["models_count" if "models_count" in result else "models"]


@pytest.mark.asyncio
async def test_falls_back_to_local_when_lan_down(manager):
    mgr, state = manager
    state["up"][LAN] = False
    result = await mgr.resolve(force=True)
    assert result["ok"] is True
    assert result["url"] == LOCAL
    assert result["kind"] == "local"
    assert result["switched_from"] in (None, LAN)


@pytest.mark.asyncio
async def test_error_when_all_servers_down(manager):
    mgr, state = manager
    state["up"][LAN] = False
    state["up"][LOCAL] = False
    result = await mgr.resolve(force=True)
    assert result["ok"] is False
    assert result["url"] == ""
    assert "недоступен" in result["error"]
    assert LAN in result["error"] and LOCAL in result["error"]
    with pytest.raises(EndpointUnavailable):
        await mgr.active_url()


@pytest.mark.asyncio
async def test_model_switched_when_missing_on_server(manager):
    mgr, state = manager
    state["up"][LAN] = False          # останется только локальный, где другой набор моделей
    config.set("lmstudio.model", "qwen2.5-7b-instruct", save=False)
    result = await mgr.resolve(force=True)
    assert result["url"] == LOCAL
    assert config.get("lmstudio.model") == "llama-3.1-8b"


@pytest.mark.asyncio
async def test_mark_failed_triggers_reconnect(manager):
    mgr, state = manager
    first = await mgr.resolve(force=True)
    assert first["url"] == LAN

    # LAN «упал»: запрос ломается, приложение уходит на резервный адрес
    state["up"][LAN] = False
    mgr.mark_failed(first["url"])
    second = await mgr.resolve(force=True)
    assert second["url"] == LOCAL
    assert second["switched_from"] == LAN


@pytest.mark.asyncio
async def test_cache_prevents_extra_probes(manager, monkeypatch):
    mgr, _ = manager
    calls = {"n": 0}
    original = EndpointManager.probe

    async def counting(self, url, timeout=None):
        calls["n"] += 1
        return await original(self, url, timeout)

    monkeypatch.setattr(EndpointManager, "probe", counting)
    config.set("lmstudio.endpoint_ttl", 30, save=False)
    await mgr.resolve(force=True)
    first_count = calls["n"]
    await mgr.resolve()          # из кэша
    assert calls["n"] == first_count


@pytest.mark.asyncio
async def test_manual_mode_uses_base_url_only(manager):
    mgr, state = manager
    config.patch({"lmstudio": {"auto_detect": False, "base_url": LOCAL}}, save=False)
    result = await mgr.resolve(force=True)
    assert result["url"] == LOCAL
    assert [p["url"] for p in result["probes"]] == [LOCAL]


@pytest.mark.asyncio
async def test_custom_candidate_order(manager):
    mgr, state = manager
    custom = "http://10.0.0.5:1234/v1"
    state["up"][custom] = True
    state["models"][custom] = ["mystery-model"]
    config.patch({"lmstudio": {"candidates": [custom, LAN, LOCAL]}}, save=False)
    result = await mgr.resolve(force=True)
    assert result["url"] == custom
    assert result["kind"] == "lan"
