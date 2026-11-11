from __future__ import annotations

from fastapi.testclient import TestClient

import web.app as webapp
from easel.session_store import SessionStore


def test_session_and_draft_state_api(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "SESSION_STORE", SessionStore(tmp_path / "api.sqlite"))
    client = TestClient(webapp.app, base_url="http://127.0.0.1")
    headers = {"Origin": "http://127.0.0.1:7860"}
    session = {
        "id": "chat-api",
        "title": "API 会话",
        "created": 1_700_000_000_000,
        "messages": [{"role": "user", "content": "来自浏览器"}],
    }

    response = client.post(
        "/api/state/sessions/sync",
        headers=headers,
        json={
            "sessions": [session],
            "deleted": [],
            "activeSessionId": "chat-api",
            "updateActive": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["saved"] == 1

    snapshot = client.get("/api/state/sessions", headers=headers).json()
    assert snapshot["activeSessionId"] == "chat-api"
    assert snapshot["sessions"][0]["title"] == "API 会话"

    assert client.put(
        "/api/state/active",
        headers=headers,
        json={"activeSessionId": None},
    ).json() == {"ok": True, "activeSessionId": None}

    draft = {
        "title": "草稿标题",
        "body": "草稿正文",
        "platforms": ["xiaohongshu"],
        "overrides": {},
        "tags": "3C",
    }
    saved = client.put("/api/state/draft", headers=headers, json={"draft": draft})
    assert saved.status_code == 200
    loaded = client.get("/api/state/draft", headers=headers).json()
    assert loaded["draft"] == draft

    deleted = client.post(
        "/api/state/sessions/sync",
        headers=headers,
        json={"sessions": [], "deleted": ["chat-api"], "updateActive": False},
    )
    assert deleted.json()["deleted"] == 1
    assert client.get("/api/state/sessions", headers=headers).json()["sessions"] == []


def test_session_state_api_rejects_invalid_id(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "SESSION_STORE", SessionStore(tmp_path / "invalid.sqlite"))
    client = TestClient(webapp.app, base_url="http://127.0.0.1")
    response = client.post(
        "/api/state/sessions/sync",
        headers={"Origin": "http://127.0.0.1:7860"},
        json={"sessions": [{"id": "../bad", "messages": []}], "deleted": []},
    )
    assert response.status_code == 400
