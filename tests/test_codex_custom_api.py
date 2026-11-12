from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import web.app as webapp
from easel.harnesses.base import HarnessError
from easel.harnesses.codex_config import (
    CODEX_CUSTOM_BASE_ENV,
    CODEX_CUSTOM_KEY_ENV,
    CODEX_CUSTOM_PROTOCOL_ENV,
    custom_api,
    provider_args,
)


def test_codex_custom_api_is_responses_only():
    with pytest.raises(HarnessError, match="Responses"):
        custom_api({
            CODEX_CUSTOM_BASE_ENV: "https://api.example.com/v1",
            CODEX_CUSTOM_KEY_ENV: "sk-test-secret",
            CODEX_CUSTOM_PROTOCOL_ENV: "chat",
        }, "gpt-test")


def test_codex_provider_args_keep_secret_out_of_command():
    secret = "sk-test-secret"
    settings = custom_api({
        CODEX_CUSTOM_BASE_ENV: "https://api.example.com/v1",
        CODEX_CUSTOM_KEY_ENV: secret,
        CODEX_CUSTOM_PROTOCOL_ENV: "openai-responses",
    }, "gpt-test")

    assert settings is not None
    joined = " ".join(provider_args(settings, "gpt-test"))
    assert secret not in joined
    assert 'model_provider="easel-custom"' in joined
    assert 'model_providers.easel-custom.base_url="https://api.example.com/v1"' in joined
    assert 'model_providers.easel-custom.env_key="EASEL_CODEX_API_KEY"' in joined
    assert 'model_providers.easel-custom.wire_api="responses"' in joined


def test_codex_custom_api_save_roundtrip_and_clear(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    outputs = tmp_path / "outputs"
    monkeypatch.setattr(webapp, "ENV_FILE", env_file)
    monkeypatch.setattr(webapp, "OUTPUTS_DIR", outputs)
    for name in (CODEX_CUSTOM_BASE_ENV, CODEX_CUSTOM_KEY_ENV, CODEX_CUSTOM_PROTOCOL_ENV):
        monkeypatch.delenv(name, raising=False)

    def describe(runtime: str) -> dict:
        return {
            "id": runtime,
            "label": runtime,
            "protocol": "test",
            "installed": True,
            "install_hint": "",
        }

    monkeypatch.setattr(webapp.harnesses, "describe", describe)
    client = TestClient(webapp.app, base_url="http://127.0.0.1:7860")
    response = client.post(
        "/api/settings/harness",
        headers={"Origin": "http://127.0.0.1:7860"},
        json={
            "runtime": "codex",
            "model": "gpt-test",
            "sandbox": "workspace-write",
            "codex_auto_approve": False,
            "codex_api": {
                "enabled": True,
                "baseUrl": "https://api.example.com/v1",
                "key": "sk-test-secret",
                "protocol": "openai-responses",
            },
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["codex_api"]["enabled"] is True
    assert data["codex_api"]["baseUrl"] == "https://api.example.com/v1"
    assert data["codex_api"]["keyMasked"]
    assert "sk-test-secret" not in response.text
    saved = env_file.read_text(encoding="utf-8")
    assert f"{CODEX_CUSTOM_BASE_ENV}=https://api.example.com/v1" in saved
    assert f"{CODEX_CUSTOM_KEY_ENV}=sk-test-secret" in saved
    assert f"{CODEX_CUSTOM_PROTOCOL_ENV}=openai-responses" in saved
    config = json.loads((outputs / "_harness" / "config.json").read_text(encoding="utf-8"))
    assert config["runtime"] == "codex"
    assert config["models"]["codex"] == "gpt-test"

    cleared = client.post(
        "/api/settings/harness",
        headers={"Origin": "http://127.0.0.1:7860"},
        json={
            "runtime": "codex",
            "model": "gpt-test",
            "sandbox": "workspace-write",
            "codex_api": {"enabled": False},
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["codex_api"]["enabled"] is False
    saved = env_file.read_text(encoding="utf-8")
    assert CODEX_CUSTOM_BASE_ENV not in saved
    assert CODEX_CUSTOM_KEY_ENV not in saved
    assert CODEX_CUSTOM_PROTOCOL_ENV not in saved


def test_codex_connection_test_rejects_chat_protocol(monkeypatch):
    monkeypatch.setattr(webapp, "_model_target_allowed", lambda _base, _slot="": True)
    client = TestClient(webapp.app, base_url="http://127.0.0.1:7860")
    response = client.post(
        "/api/settings/models/test",
        headers={"Origin": "http://127.0.0.1:7860"},
        json={
            "baseUrl": "https://api.example.com/v1",
            "key": "sk-test-secret",
            "model": "gpt-test",
            "protocol": "openai",
            "slot": "codex",
        },
    )
    assert response.status_code == 400
    assert "Responses" in response.text
