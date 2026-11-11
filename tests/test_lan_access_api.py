from __future__ import annotations

from fastapi.testclient import TestClient

import web.app as webapp


def test_lan_access_toggle_and_host_guard(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(webapp, "ENV_FILE", env_file)
    monkeypatch.setattr(webapp, "_LAN_ACCESS_ENABLED", False)
    monkeypatch.setattr(webapp, "_LAN_ACCESS_LOCKED", False)
    monkeypatch.setattr(webapp, "_lan_ipv4_addresses", lambda: ["192.168.1.20"])

    local = TestClient(webapp.app, base_url="http://127.0.0.1:7860")
    lan = TestClient(webapp.app, base_url="http://192.168.1.20:7860")
    headers = {"Origin": "http://127.0.0.1:7860"}

    state = local.get("/api/settings/lan").json()
    assert state["enabled"] is False
    assert state["urls"] == ["http://192.168.1.20:7860"]
    assert lan.get("/api/settings/lan").status_code == 400

    enabled = local.post("/api/settings/lan", headers=headers, json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert enabled.json()["source"] == "envfile"
    assert "EASEL_LAN_ACCESS=1" in env_file.read_text(encoding="utf-8")
    assert lan.get("/api/settings/lan").status_code == 200

    disabled = local.post("/api/settings/lan", headers=headers, json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert "EASEL_LAN_ACCESS" not in env_file.read_text(encoding="utf-8")
    assert lan.get("/api/settings/lan").status_code == 400


def test_lan_access_rejects_invalid_payload(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(webapp, "ENV_FILE", env_file)
    monkeypatch.setattr(webapp, "_LAN_ACCESS_LOCKED", False)

    client = TestClient(webapp.app, base_url="http://127.0.0.1:7860")
    response = client.post(
        "/api/settings/lan",
        headers={"Origin": "http://127.0.0.1:7860"},
        json={"enabled": "yes"},
    )
    assert response.status_code == 422
