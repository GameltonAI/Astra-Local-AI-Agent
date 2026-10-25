"""Работа с сессиями (чатами) и сообщениями."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

from .db import get_conn, transaction


def _row_to_session(row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"] or "Без названия",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "model": row["model"] or "",
        "pinned": bool(row["pinned"]),
        "archived": bool(row["archived"]),
        "meta": json.loads(row["meta"] or "{}"),
    }


def _row_to_message(row, with_meta: bool = True) -> Dict[str, Any]:
    msg = {
        "id": row["id"],
        "session_id": row["session_id"],
        "role": row["role"],
        "content": row["content"],
        "hidden": bool(row["hidden"]),
        "created_at": row["created_at"],
    }
    if with_meta:
        try:
            msg["meta"] = json.loads(row["meta"] or "{}")
        except json.JSONDecodeError:
            msg["meta"] = {}
    return msg


# ---------------------------------------------------------------- сессии ---
def create_session(title: str = "", model: str = "") -> Dict[str, Any]:
    now = time.time()
    sid = uuid.uuid4().hex[:12]
    with transaction() as conn:
        conn.execute(
            "INSERT INTO sessions(id, title, created_at, updated_at, model) VALUES(?,?,?,?,?)",
            (sid, title or "Новый чат", now, now, model),
        )
    return get_session(sid)  # type: ignore[return-value]


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    row = get_conn().execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return _row_to_session(row) if row else None


def list_sessions(include_archived: bool = False, query: str = "") -> List[Dict[str, Any]]:
    sql = "SELECT * FROM sessions"
    params: List[Any] = []
    where = []
    if not include_archived:
        where.append("archived=0")
    if query.strip():
        where.append("(title LIKE ? OR id IN (SELECT session_id FROM messages WHERE content LIKE ?))")
        like = f"%{query.strip()}%"
        params += [like, like]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY pinned DESC, updated_at DESC LIMIT 500"
    rows = get_conn().execute(sql, params).fetchall()
    return [_row_to_session(r) for r in rows]


def update_session(session_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    allowed = {"title", "model", "pinned", "archived", "meta"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "meta" and isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        if key in ("pinned", "archived"):
            value = 1 if value else 0
        sets.append(f"{key}=?")
        params.append(value)
    if sets:
        sets.append("updated_at=?")
        params.append(time.time())
        params.append(session_id)
        with transaction() as conn:
            conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", params)
    return get_session(session_id)


def delete_session(session_id: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def touch_session(session_id: str) -> None:
    with transaction() as conn:
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), session_id))


def ensure_session(session_id: Optional[str], model: str = "") -> Dict[str, Any]:
    if session_id:
        session = get_session(session_id)
        if session:
            return session
    return create_session(model=model)


# ------------------------------------------------------------ сообщения ----
def add_message(
    session_id: str,
    role: str,
    content: str,
    meta: Optional[Dict[str, Any]] = None,
    hidden: bool = False,
) -> Dict[str, Any]:
    now = time.time()
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO messages(session_id, role, content, meta, hidden, created_at) VALUES(?,?,?,?,?,?)",
            (session_id, role, content, json.dumps(meta or {}, ensure_ascii=False), 1 if hidden else 0, now),
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
        msg_id = cur.lastrowid
    return {
        "id": msg_id,
        "session_id": session_id,
        "role": role,
        "content": content,
        "meta": meta or {},
        "hidden": hidden,
        "created_at": now,
    }


def list_messages(session_id: str, include_hidden: bool = True, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM messages WHERE session_id=?"
    params: List[Any] = [session_id]
    if not include_hidden:
        sql += " AND hidden=0"
    sql += " ORDER BY id ASC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = get_conn().execute(sql, params).fetchall()
    return [_row_to_message(r) for r in rows]


def delete_message(message_id: int) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM messages WHERE id=?", (message_id,))


def truncate_after(session_id: str, message_id: int) -> None:
    """Удалить сообщения новее указанного (для «перегенерировать»/«отредактировать»)."""
    with transaction() as conn:
        conn.execute("DELETE FROM messages WHERE session_id=? AND id>?", (session_id, message_id))


def export_session_markdown(session_id: str) -> str:
    session = get_session(session_id)
    if not session:
        return ""
    lines = [f"# {session['title']}", ""]
    for msg in list_messages(session_id, include_hidden=False):
        stamp = time.strftime("%d.%m.%Y %H:%M", time.localtime(msg["created_at"]))
        who = {"user": "Пользователь", "assistant": "Astra", "note": "Система"}.get(msg["role"], msg["role"])
        lines.append(f"### {who} · {stamp}")
        lines.append("")
        lines.append(msg["content"].strip())
        actions = (msg.get("meta") or {}).get("actions") or []
        if actions:
            lines.append("")
            lines.append("<details><summary>Действия</summary>")
            lines.append("")
            for act in actions:
                lines.append(f"- `{act.get('tool')}` {json.dumps(act.get('args') or {}, ensure_ascii=False)}")
            lines.append("")
            lines.append("</details>")
        lines.append("")
    return "\n".join(lines)


def stats() -> Dict[str, Any]:
    conn = get_conn()
    sessions = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
    messages = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    return {"sessions": sessions, "messages": messages}
