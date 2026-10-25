"""Инструменты интернета: поиск и чтение страниц."""
from __future__ import annotations

import html
import re
import urllib.parse
from typing import List

import httpx

from .base import Param, Tool

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_PAGE_CHARS = 8000


def _clean_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<!--.*?-->", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def web_search(query: str, max_results: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        raise ValueError("Пустой поисковый запрос")
    max_results = max(1, min(int(max_results or 5), 10))
    headers = {"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
    results: List[dict] = []
    with httpx.Client(timeout=25, follow_redirects=True, headers=headers) as client:
        # 1) html-версия DuckDuckGo
        try:
            resp = client.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "ru-ru"})
            if resp.status_code == 200:
                pattern = re.compile(
                    r'<a[^>]+class="result__a"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>'
                    r'(?:.*?class="result__snippet"[^>]*>(?P<snippet>.*?)</a>)?',
                    re.S,
                )
                for m in pattern.finditer(resp.text):
                    url = html.unescape(m.group("url"))
                    if "uddg=" in url:
                        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                        url = parsed.get("uddg", [url])[0]
                    title = _clean_html(m.group("title") or "")
                    snippet = _clean_html(m.group("snippet") or "")
                    if title and url:
                        results.append({"title": title, "url": url, "snippet": snippet})
                    if len(results) >= max_results:
                        break
        except Exception as exc:
            print(f"[web_search] duckduckgo html недоступен: {exc}")

        # 2) резервный вариант — lite.duckduckgo.com
        if not results:
            try:
                resp = client.post("https://lite.duckduckgo.com/lite/", data={"q": query})
                if resp.status_code == 200:
                    pattern = re.compile(r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
                    for url, title in pattern.findall(resp.text)[:max_results]:
                        results.append({"title": _clean_html(title), "url": html.unescape(url), "snippet": ""})
            except Exception as exc:
                print(f"[web_search] lite-версия недоступна: {exc}")

    if not results:
        return f"Поиск «{query}» не дал результатов (возможно, нет интернета)."
    lines = [f"Результаты поиска «{query}»:"]
    for i, item in enumerate(results, 1):
        lines.append(f"\n{i}. {item['title']}\n   {item['url']}")
        if item["snippet"]:
            lines.append(f"   {item['snippet'][:400]}")
    return "\n".join(lines)


def fetch_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("Пустой адрес")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    headers = {"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
        resp = client.get(url)
    if resp.status_code >= 400:
        raise RuntimeError(f"Сайт вернул ошибку {resp.status_code}")
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        body = resp.text[:MAX_PAGE_CHARS]
    elif "html" in content_type or resp.text.lstrip().startswith("<"):
        body = _clean_html(resp.text)
    else:
        body = resp.text
    if len(body) > MAX_PAGE_CHARS:
        body = body[:MAX_PAGE_CHARS] + "\n… (страница обрезана)"
    return f"{url}\n\n{body}"


TOOLS = [
    Tool(
        name="web_search",
        summary="Найти информацию в интернете (актуальные данные, новости, документация).",
        params=[
            Param("query", "str", "Поисковый запрос", True),
            Param("max_results", "int", "Сколько результатов", False, 5),
        ],
        func=web_search,
        category="web",
        examples=['{"query": "погода в Мурманске завтра"}'],
    ),
    Tool(
        name="fetch_url",
        summary="Скачать страницу по адресу и вернуть её текст.",
        params=[Param("url", "str", "Адрес страницы", True)],
        func=fetch_url,
        category="web",
    ),
]
