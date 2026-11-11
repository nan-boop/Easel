from __future__ import annotations

import pytest

from easel.session_store import SessionStore, SessionStoreError


def _session(session_id: str = "chat-a") -> dict:
    return {
        "id": session_id,
        "title": "测试会话",
        "created": 1_700_000_000_000,
        "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "收到"},
        ],
        "sessionKey": "session-key-a",
        "persona": "生活分享",
        "pendingTurnId": "turn-a",
    }


def test_sessions_survive_reopen(tmp_path):
    path = tmp_path / "easel.sqlite"
    store = SessionStore(path)
    result = store.sync(
        [_session()],
        [],
        active_session_id="chat-a",
        update_active=True,
    )

    assert result == {
        "ok": True,
        "saved": 1,
        "skipped": 0,
        "deleted": 0,
        "total": 1,
    }

    reopened = SessionStore(path)
    snapshot = reopened.snapshot()
    assert snapshot["activeSessionId"] == "chat-a"
    assert snapshot["sessions"][0]["sessionKey"] == "session-key-a"
    assert snapshot["sessions"][0]["messages"][1]["content"] == "收到"


def test_delete_cascades_and_blocks_stale_resurrection(tmp_path):
    store = SessionStore(tmp_path / "easel.sqlite")
    store.sync([_session()], [], active_session_id="chat-a", update_active=True)

    store.sync([], ["chat-a"], active_session_id=None, update_active=True)
    snapshot = store.snapshot()

    assert snapshot["sessions"] == []
    assert snapshot["activeSessionId"] is None
    assert "chat-a" in snapshot["deletedSessionIds"]
    assert store.sync([_session()], [])["skipped"] == 1
    assert store.snapshot()["sessions"] == []


def test_draft_persists_with_update_time(tmp_path):
    path = tmp_path / "easel.sqlite"
    store = SessionStore(path)
    draft = {
        "title": "标题",
        "body": "正文",
        "platforms": ["xiaohongshu"],
        "overrides": {},
        "tags": "加湿器",
    }

    saved = store.save_draft(draft)
    assert saved["ok"] is True
    assert saved["updatedAt"] > 0

    reopened = SessionStore(path)
    result = reopened.draft()
    assert result == {"draft": draft, "updatedAt": saved["updatedAt"]}


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "../escape", "messages": []},
        {"id": "valid", "messages": [{"role": "system", "content": "x"}]},
        {"id": "valid", "messages": [{"role": "user", "content": None}]},
        {"id": "valid", "messages": [], "archived": "yes"},
    ],
)
def test_invalid_session_is_rejected(tmp_path, payload):
    store = SessionStore(tmp_path / "easel.sqlite")
    with pytest.raises(SessionStoreError):
        store.sync([payload], [])
