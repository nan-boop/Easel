"""SQLite persistence for browser-independent chat sessions and drafts."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_SESSION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_MAX_TITLE = 500
_MAX_MESSAGES = 5_000
_MAX_MESSAGE_BYTES = 4_000_000
_MAX_DRAFT_BYTES = 2_000_000
_ACTIVE_KEY = "active_session_id"
_DRAFT_KEY = "publish_draft"
_INITIALIZED_KEY = "sessions_initialized"


class SessionStoreError(ValueError):
    """Invalid session or draft payload."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _text(value: Any, field: str, *, limit: int, optional: bool = False) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        raise SessionStoreError(f"{field} 必须是字符串")
    if "\x00" in value:
        raise SessionStoreError(f"{field} 不能包含 NUL 字符")
    if len(value) > limit:
        raise SessionStoreError(f"{field} 超过长度限制")
    return value


def _timestamp(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _now_ms()
    value = int(value)
    return value if 0 < value <= 9_007_199_254_740_991 else _now_ms()


def _normalize_session(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SessionStoreError("会话格式无效")
    session_id = raw.get("id")
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise SessionStoreError("会话 ID 无效")
    title = _text(raw.get("title") or "New Chat", "标题", limit=_MAX_TITLE)
    persona = _text(raw.get("persona"), "画像", limit=160, optional=True)
    session_key = _text(raw.get("sessionKey"), "sessionKey", limit=240, optional=True)
    pending_turn = _text(raw.get("pendingTurnId"), "pendingTurnId", limit=240, optional=True)
    archived = raw.get("archived", False)
    if not isinstance(archived, bool):
        raise SessionStoreError("归档状态无效")
    messages = raw.get("messages", [])
    if not isinstance(messages, list) or len(messages) > _MAX_MESSAGES:
        raise SessionStoreError("消息列表无效或过长")
    encoded_messages: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            raise SessionStoreError("消息格式无效")
        if message.get("role") not in {"user", "assistant"}:
            raise SessionStoreError("消息角色无效")
        if not isinstance(message.get("content"), str):
            raise SessionStoreError("消息正文无效")
        try:
            encoded = json.dumps(message, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise SessionStoreError("消息包含无法保存的数据") from error
        if len(encoded.encode("utf-8")) > _MAX_MESSAGE_BYTES:
            raise SessionStoreError("单条消息超过大小限制")
        encoded_messages.append(encoded)
    return {
        "id": session_id,
        "title": title,
        "persona": persona,
        "session_key": session_key,
        "pending_turn_id": pending_turn,
        "archived": archived,
        "created": _timestamp(raw.get("created")),
        "messages": encoded_messages,
    }


def _normalize_draft(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SessionStoreError("草稿格式无效")
    try:
        encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise SessionStoreError("草稿包含无法保存的数据") from error
    if len(encoded.encode("utf-8")) > _MAX_DRAFT_BYTES:
        raise SessionStoreError("草稿超过大小限制")
    return raw


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._init_lock = threading.Lock()
        self._initialized = False

    def _ensure(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=15)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        persona TEXT NOT NULL DEFAULT '',
                        created INTEGER NOT NULL,
                        updated INTEGER NOT NULL,
                        archived INTEGER NOT NULL DEFAULT 0,
                        session_key TEXT NOT NULL DEFAULT '',
                        pending_turn_id TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_sessions_updated
                        ON sessions(updated DESC);
                    CREATE TABLE IF NOT EXISTS session_messages (
                        session_id TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY (session_id, ordinal),
                        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS app_state (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS deleted_sessions (
                        id TEXT PRIMARY KEY,
                        deleted INTEGER NOT NULL
                    );
                    PRAGMA user_version=2;
                    """
                )
                connection.commit()
            finally:
                connection.close()
            self._initialized = True

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._ensure()
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _state_get(connection: sqlite3.Connection, key: str) -> Any:
        row = connection.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _state_set(connection: sqlite3.Connection, key: str, value: Any) -> int:
        if value is None:
            connection.execute("DELETE FROM app_state WHERE key=?", (key,))
            return _now_ms()
        updated = _now_ms()
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        connection.execute(
            """
            INSERT INTO app_state(key, value, updated) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated
            """,
            (key, encoded, updated),
        )
        return updated

    @staticmethod
    def _session_row(row: sqlite3.Row, messages: list[dict[str, Any]]) -> dict[str, Any]:
        session: dict[str, Any] = {
            "id": row["id"],
            "title": row["title"],
            "messages": messages,
            "created": row["created"],
            "updated": row["updated"],
            "archived": bool(row["archived"]),
        }
        if row["persona"]:
            session["persona"] = row["persona"]
        if row["session_key"]:
            session["sessionKey"] = row["session_key"]
        if row["pending_turn_id"]:
            session["pendingTurnId"] = row["pending_turn_id"]
        return session

    def snapshot(self) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions ORDER BY updated DESC, created DESC"
            ).fetchall()
            sessions: list[dict[str, Any]] = []
            for row in rows:
                message_rows = connection.execute(
                    "SELECT payload FROM session_messages "
                    "WHERE session_id=? ORDER BY ordinal",
                    (row["id"],),
                ).fetchall()
                messages = []
                for message_row in message_rows:
                    try:
                        payload = json.loads(message_row["payload"])
                    except (TypeError, ValueError):
                        continue
                    if isinstance(payload, dict):
                        messages.append(payload)
                sessions.append(self._session_row(row, messages))
            active = self._state_get(connection, _ACTIVE_KEY)
            if not isinstance(active, str):
                active = None
            deleted_rows = connection.execute(
                "SELECT id FROM deleted_sessions ORDER BY deleted DESC"
            ).fetchall()
            deleted_ids = [row["id"] for row in deleted_rows]
            initialized = self._state_get(connection, _INITIALIZED_KEY) is True
            return {
                "sessions": sessions,
                "activeSessionId": active,
                "deletedSessionIds": deleted_ids,
                "initialized": initialized or bool(sessions) or bool(deleted_ids),
            }

    def sync(
        self,
        sessions: list[Any],
        deleted: list[Any],
        *,
        active_session_id: str | None = None,
        update_active: bool = False,
    ) -> dict[str, Any]:
        normalized = [_normalize_session(item) for item in sessions]
        deleted_ids = []
        for item in deleted:
            if not isinstance(item, str) or not _SESSION_ID.fullmatch(item):
                raise SessionStoreError("删除的会话 ID 无效")
            deleted_ids.append(item)
        with self._connection() as connection:
            deleted_at = _now_ms()
            for session_id in deleted_ids:
                connection.execute("DELETE FROM sessions WHERE id=?", (session_id,))
                connection.execute(
                    """
                    INSERT INTO deleted_sessions(id, deleted) VALUES(?, ?)
                    ON CONFLICT(id) DO UPDATE SET deleted=excluded.deleted
                    """,
                    (session_id, deleted_at),
                )
                current = self._state_get(connection, _ACTIVE_KEY)
                if current == session_id:
                    self._state_set(connection, _ACTIVE_KEY, None)

            base_time = _now_ms()
            saved = 0
            for index, session in enumerate(normalized):
                deleted = connection.execute(
                    "SELECT 1 FROM deleted_sessions WHERE id=?", (session["id"],)
                ).fetchone()
                if deleted is not None:
                    continue
                existing = connection.execute(
                    "SELECT created FROM sessions WHERE id=?", (session["id"],)
                ).fetchone()
                created = existing["created"] if existing else session["created"]
                # Keep the incoming list order for clients that import many records at once.
                updated = base_time + max(0, len(normalized) - index)
                connection.execute(
                    """
                    INSERT INTO sessions(
                        id, title, persona, created, updated, archived,
                        session_key, pending_turn_id
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title=excluded.title,
                        persona=excluded.persona,
                        updated=excluded.updated,
                        archived=excluded.archived,
                        session_key=excluded.session_key,
                        pending_turn_id=excluded.pending_turn_id
                    """,
                    (
                        session["id"],
                        session["title"],
                        session["persona"],
                        created,
                        updated,
                        int(session["archived"]),
                        session["session_key"],
                        session["pending_turn_id"],
                    ),
                )
                connection.execute(
                    "DELETE FROM session_messages WHERE session_id=?",
                    (session["id"],),
                )
                connection.executemany(
                    "INSERT INTO session_messages(session_id, ordinal, payload) "
                    "VALUES(?, ?, ?)",
                    [
                        (session["id"], ordinal, payload)
                        for ordinal, payload in enumerate(session["messages"])
                    ],
                )
                saved += 1

            if update_active:
                if active_session_id is not None and not _SESSION_ID.fullmatch(active_session_id):
                    raise SessionStoreError("当前会话 ID 无效")
                self._state_set(connection, _ACTIVE_KEY, active_session_id or None)

            self._state_set(connection, _INITIALIZED_KEY, True)
            saved_rows = connection.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()
            return {
                "ok": True,
                "saved": saved,
                "skipped": len(normalized) - saved,
                "deleted": len(deleted_ids),
                "total": int(saved_rows["count"]),
            }

    def set_active(self, session_id: str | None) -> dict[str, Any]:
        if session_id is not None and not _SESSION_ID.fullmatch(session_id):
            raise SessionStoreError("当前会话 ID 无效")
        with self._connection() as connection:
            self._state_set(connection, _ACTIVE_KEY, session_id or None)
        return {"ok": True, "activeSessionId": session_id or None}

    def draft(self) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value, updated FROM app_state WHERE key=?", (_DRAFT_KEY,)
            ).fetchone()
        value = None
        updated_at = None
        if row is not None:
            try:
                parsed = json.loads(row["value"])
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                value = parsed
                updated_at = int(row["updated"])
        return {"draft": value, "updatedAt": updated_at}

    def save_draft(self, draft: Any) -> dict[str, Any]:
        value = _normalize_draft(draft)
        with self._connection() as connection:
            updated_at = self._state_set(connection, _DRAFT_KEY, value)
        return {"ok": True, "updatedAt": updated_at}
