from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import easel.direct_api as direct_api
import web.app as webapp


def test_direct_responses_stream_and_history(tmp_path, monkeypatch):
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append({
            "url": str(request.url),
            "body": json.loads(request.content.decode("utf-8")),
            "authorization": request.headers.get("authorization"),
        })
        stream = "\n\n".join([
            'data: {"type":"response.reasoning_summary_text.delta","delta":"想一想"}',
            'data: {"type":"response.output_text.delta","delta":"你"}',
            'data: {"type":"response.output_text.delta","delta":"好"}',
            'data: {"type":"response.completed","response":{"status":"completed"}}',
        ]) + "\n\n"
        return httpx.Response(
            200,
            content=stream.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    real_client = httpx.AsyncClient

    class FakeAsyncClient:
        def __init__(self, *_args, **_kwargs):
            self.client = real_client(transport=httpx.MockTransport(handler))

        async def __aenter__(self):
            await self.client.__aenter__()
            return self.client

        async def __aexit__(self, *args):
            return await self.client.__aexit__(*args)

    monkeypatch.setattr(direct_api.httpx, "AsyncClient", FakeAsyncClient)
    history = tmp_path / "history.json"

    async def collect():
        return [
            item async for item in direct_api.stream_chat(
                {
                    "EASEL_DIRECT_API_BASE_URL": "https://api.example.com/v1",
                    "EASEL_DIRECT_API_MODEL": "gpt-test",
                    "EASEL_DIRECT_API_KEY": "test-key",
                    "EASEL_DIRECT_API_PROTOCOL": "openai-responses",
                },
                "你好",
                history_file=history,
            )
        ]

    events = asyncio.run(collect())

    assert ("thinking", "想一想") in events
    assert [text for kind, text in events if kind == "token"] == ["你", "好"]
    assert events[-1] == ("finish", "stop")
    assert requests[0]["url"] == "https://api.example.com/v1/responses"
    assert requests[0]["authorization"] == "Bearer test-key"
    assert requests[0]["body"]["model"] == "gpt-test"
    assert requests[0]["body"]["instructions"]
    assert requests[0]["body"]["input"][0]["content"] == "你好"
    saved = json.loads(history.read_text(encoding="utf-8"))
    assert saved[-1] == {"role": "assistant", "content": "你好"}


def test_direct_chat_stream_still_uses_chat_completions(monkeypatch):
    seen_url = ""

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_url
        seen_url = str(request.url)
        stream = (
            'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=stream.encode(), headers={"content-type": "text/event-stream"})

    real_client = httpx.AsyncClient

    class FakeAsyncClient:
        def __init__(self, *_args, **_kwargs):
            self.client = real_client(transport=httpx.MockTransport(handler))

        async def __aenter__(self):
            await self.client.__aenter__()
            return self.client

        async def __aexit__(self, *args):
            return await self.client.__aexit__(*args)

    monkeypatch.setattr(direct_api.httpx, "AsyncClient", FakeAsyncClient)

    async def collect():
        return [
            item async for item in direct_api.stream_chat(
                {
                    "EASEL_DIRECT_API_BASE_URL": "https://api.example.com/v1",
                    "EASEL_DIRECT_API_MODEL": "gpt-test",
                    "EASEL_DIRECT_API_KEY": "test-key",
                },
                "hi",
            )
        ]

    events = asyncio.run(collect())
    assert ("token", "OK") in events
    assert events[-1] == ("finish", "stop")
    assert seen_url == "https://api.example.com/v1/chat/completions"


def test_direct_responses_rejects_failed_completed_event(monkeypatch):
    async def handler(_request: httpx.Request) -> httpx.Response:
        stream = (
            'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
            'data: {"type":"response.completed","response":{"status":"failed"}}\n\n'
        )
        return httpx.Response(
            200,
            content=stream.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    real_client = httpx.AsyncClient

    class FakeAsyncClient:
        def __init__(self, *_args, **_kwargs):
            self.client = real_client(transport=httpx.MockTransport(handler))

        async def __aenter__(self):
            await self.client.__aenter__()
            return self.client

        async def __aexit__(self, *args):
            return await self.client.__aexit__(*args)

    monkeypatch.setattr(direct_api.httpx, "AsyncClient", FakeAsyncClient)

    async def collect():
        return [
            item async for item in direct_api.stream_chat(
                {
                    "EASEL_DIRECT_API_BASE_URL": "https://api.example.com/v1",
                    "EASEL_DIRECT_API_MODEL": "gpt-test",
                    "EASEL_DIRECT_API_KEY": "test-key",
                    "EASEL_DIRECT_API_PROTOCOL": "openai-responses",
                },
                "hello",
            )
        ]

    with pytest.raises(direct_api.DirectAPIError, match="失败或取消"):
        asyncio.run(collect())


def test_custom_responses_provider_save(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    openclaw = tmp_path / "openclaw.json"
    openclaw.write_text(json.dumps({"models": {"providers": {}}}), encoding="utf-8")

    monkeypatch.setattr(webapp, "ENV_FILE", env_file)
    monkeypatch.setattr(webapp, "_harness_settings", lambda: {"runtime": "openclaw"})
    monkeypatch.setattr(webapp, "_direct_api_enabled", lambda: False)
    monkeypatch.setattr(webapp, "_read_env", lambda: {})
    monkeypatch.setattr(webapp, "_oc_config_path", lambda: openclaw)
    monkeypatch.setattr(webapp, "_write_env_direct", lambda _updates: None)

    client = TestClient(webapp.app, base_url="http://127.0.0.1:7860")
    response = client.post(
        "/api/settings/models/save",
        headers={"Origin": "http://127.0.0.1:7860"},
        json={
            "channel": "chat",
            "rows": [{
                "slot": "custom",
                "name": "my-openai",
                "model": "gpt-test",
                "baseUrl": "https://api.example.com/v1",
                "key": "test-key",
                "primary": True,
                "protocol": "openai-responses",
            }],
        },
    )
    assert response.status_code == 200, response.text
    data = json.loads(openclaw.read_text(encoding="utf-8"))
    provider = data["models"]["providers"]["my-openai"]
    assert provider["api"] == "openai-responses"
    assert provider["baseUrl"] == "https://api.example.com/v1"
    assert provider["models"][0]["id"] == "gpt-test"
    assert data["agents"]["defaults"]["model"]["primary"] == "my-openai/gpt-test"


def test_model_connection_endpoint_canonicalizes_protocol(monkeypatch):
    seen: dict = {}

    def fake_probe(base, key, model, protocol, slot=""):
        seen.update(base=base, key=key, model=model, protocol=protocol, slot=slot)
        return {"ok": True, "ms": 12, "detail": ""}

    monkeypatch.setattr(webapp, "_probe_model_completion", fake_probe)
    monkeypatch.setattr(webapp, "_model_target_allowed", lambda _base, _slot="": True)
    client = TestClient(webapp.app, base_url="http://127.0.0.1:7860")

    response = client.post(
        "/api/settings/models/test",
        headers={"Origin": "http://127.0.0.1:7860"},
        json={
            "baseUrl": "https://api.example.com/v1",
            "key": "test-key",
            "model": "gpt-test",
            "protocol": "responses",
            "slot": "custom",
            "name": "my-openai",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert seen == {
        "base": "https://api.example.com/v1",
        "key": "test-key",
        "model": "gpt-test",
        "protocol": "openai-responses",
        "slot": "custom",
    }


def test_probe_model_completion_sends_real_responses_request(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"id":"resp_1","status":"completed","output":[]}'

    class FakeOpener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(webapp.urllib.request, "build_opener", lambda *_args: FakeOpener())
    result = webapp._probe_model_completion(
        "https://api.example.com/v1", "test-key", "gpt-test", "openai-responses", "custom")

    assert result["ok"] is True
    assert captured["url"] == "https://api.example.com/v1/responses"
    assert captured["authorization"] == "Bearer test-key"
    assert captured["body"] == {
        "model": "gpt-test",
        "input": "Reply with OK.",
        "stream": False,
    }
