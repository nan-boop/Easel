"""OpenAI-compatible chat without an OpenClaw installation or Gateway."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PROMPT = (
    "你是 Easel 社媒内容助手，用用户的语言协助写作、策划和分析。"
    "当前是模型 API 直连模式，没有本机文件、命令、浏览器或技能执行工具。"
    "仅根据对话和提供的画像、附件内容回答；不能声称已读取其他文件、"
    "执行技能、生成本地文件或发布内容。需要用户补充的信息请用文字询问。"
)

DIRECT_PROTOCOL_ENV = "EASEL_DIRECT_API_PROTOCOL"
DIRECT_PROTOCOLS = {"openai", "openai-responses"}
_DIRECT_PROTOCOL_ALIASES = {
    "": "openai",
    "chat": "openai",
    "chat-completions": "openai",
    "openai-chat": "openai",
    "responses": "openai-responses",
    "openai-response": "openai-responses",
}


class DirectAPIError(Exception):
    """Safe user-facing diagnostics; never include credentials or upstream bodies."""


def read_settings(env_file: Path | None = None) -> dict[str, str]:
    settings = {}
    path = env_file if env_file is not None else PROJECT_ROOT / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                settings[key.strip()] = value.strip().strip("\"'")
    settings.update(os.environ)
    return settings


def api_mode(settings: dict[str, str]) -> bool:
    return settings.get("EASEL_CHAT_TRANSPORT", "http").strip().lower() == "api"


def direct_protocol(settings: dict[str, str]) -> str:
    raw = settings.get(DIRECT_PROTOCOL_ENV, "").strip().lower()
    protocol = _DIRECT_PROTOCOL_ALIASES.get(raw, raw or "openai")
    if protocol not in DIRECT_PROTOCOLS:
        raise DirectAPIError(
            f"{DIRECT_PROTOCOL_ENV} 只支持 openai（Chat Completions）"
            "或 openai-responses（Responses API）"
        )
    return protocol


def api_config(settings: dict[str, str], *, require_model: bool = True) -> tuple[str, str, str]:
    base = settings.get("EASEL_DIRECT_API_BASE_URL", "").strip().rstrip("/")
    model = settings.get("EASEL_DIRECT_API_MODEL", "").strip()
    key = settings.get("EASEL_DIRECT_API_KEY", "").strip()
    try:
        url = urlsplit(base)
        valid = (url.scheme in ("http", "https") and bool(url.hostname)
                 and url.port != 0 and not url.username and not url.password
                 and not url.query and not url.fragment
                 and not any(c.isspace() or ord(c) < 32 for c in base))
    except ValueError:
        valid = False
    if not valid:
        raise DirectAPIError("请填写有效的 EASEL_DIRECT_API_BASE_URL，例如 http://localhost:50288/v1")
    if (require_model and not model) or any(c in model for c in "\r\n"):
        raise DirectAPIError("请填写 EASEL_DIRECT_API_MODEL，例如 gpt-6.1-sol")
    if any(c.isspace() for c in key) or re.search(
            r"replace_me|your[-_]?api[-_]?key|^xxx$|^\.{3}$|^<.*>$", key, re.I):
        raise DirectAPIError("EASEL_DIRECT_API_KEY 无效，请填写网关的真实 Key；无需鉴权时可留空")
    direct_protocol(settings)
    return base, model, key


def history_path(directory: Path, session: str) -> Path:
    return directory / "api" / f"{hashlib.sha256(session.encode()).hexdigest()}.json"


async def _sse_payloads(response: httpx.Response):
    data_lines = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        if not line and data_lines:
            yield "\n".join(data_lines)
            data_lines = []
    if data_lines:
        yield "\n".join(data_lines)


def _responses_content(content: object) -> object:
    """把 Chat Completions 风格内容转换为 Responses API 的 input 内容。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return []
    converted = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "")
        if kind in ("text", "input_text"):
            text = part.get("text")
            if isinstance(text, str) and text:
                converted.append({"type": "input_text", "text": text})
        elif kind in ("image_url", "input_image"):
            image = part.get("image_url") or part.get("image")
            if isinstance(image, dict):
                image = image.get("url")
            if isinstance(image, str) and image:
                converted.append({"type": "input_image", "image_url": image})
    return converted


def _responses_messages(history: list, user: dict) -> list[dict]:
    messages = []
    for message in [*history, user]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        if role not in ("user", "assistant", "system", "developer"):
            continue
        content = _responses_content(message.get("content"))
        messages.append({"role": role, "content": content})
    return messages


def _completed_response_text(event: dict) -> str:
    response = event.get("response")
    response = response if isinstance(response, dict) else event
    direct = response.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    chunks = []
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
    return "".join(chunks)


async def stream_chat(settings: dict[str, str], content: str | list, *,
                      system: str = SYSTEM_PROMPT, history_file: Path | None = None,
                      timeout: float = 300):
    """Yield (token/thinking/finish, text); persist only complete exchanges."""
    base, model, key = api_config(settings)
    protocol = direct_protocol(settings)
    history = []
    if history_file is not None and history_file.is_file():
        try:
            history = json.loads(history_file.read_text(encoding="utf-8"))
            if not isinstance(history, list):
                raise ValueError
        except (OSError, ValueError):
            raise DirectAPIError("直连会话历史损坏，请新建会话") from None
    user = {"role": "user", "content": content}
    if protocol == "openai-responses":
        endpoint = "/responses"
        body = {
            "model": model,
            "instructions": system,
            "input": _responses_messages(history, user),
            "stream": True,
        }
    else:
        endpoint = "/chat/completions"
        body = {"model": model, "stream": True,
                "messages": [{"role": "system", "content": system}, *history, user]}
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    text = []
    finish_reason = None
    try:
        # Local/private gateways must bypass inherited system HTTP proxies.
        # Redirects must never forward the credential to a different destination.
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(timeout, connect=10)) as client:
            async with client.stream("POST", base + endpoint,
                                     json=body, headers=headers) as response:
                if response.status_code != 200:
                    raise DirectAPIError(f"API 返回 HTTP {response.status_code}，请检查地址、Key 和模型")
                async for payload in _sse_payloads(response):
                    if payload == "[DONE]":
                        if protocol == "openai-responses" and text:
                            finish_reason = "stop"
                        break
                    try:
                        event = json.loads(payload)
                        if not isinstance(event, dict) or event.get("error"):
                            raise ValueError
                        if protocol == "openai-responses":
                            event_type = str(event.get("type") or "")
                            if event_type in ("response.failed", "error"):
                                raise DirectAPIError("Responses API 返回失败状态")
                            if event_type == "response.incomplete":
                                finish_reason = "length"
                                continue
                            if event_type == "response.output_item.added":
                                item = event.get("item")
                                if isinstance(item, dict) and item.get("type") == "function_call":
                                    raise DirectAPIError(
                                        "网关要求执行工具，但 API 直连模式没有本机执行工具")
                            if event_type in ("response.reasoning_summary_text.delta",
                                              "response.reasoning_text.delta"):
                                reasoning = event.get("delta")
                                if isinstance(reasoning, str) and reasoning:
                                    yield "thinking", reasoning
                                continue
                            if event_type in ("response.output_text.delta", "response.refusal.delta"):
                                token = event.get("delta")
                                if isinstance(token, str) and token:
                                    text.append(token)
                                    yield "token", token
                                continue
                            if event_type == "response.completed":
                                response = event.get("response")
                                response = response if isinstance(response, dict) else event
                                status = str(response.get("status") or "").strip().lower()
                                if status in ("failed", "cancelled"):
                                    raise DirectAPIError("Responses API 返回失败或取消状态")
                                if status == "incomplete":
                                    finish_reason = "length"
                                    continue
                                if status and status not in ("completed", "succeeded"):
                                    raise DirectAPIError("Responses API 返回了未知完成状态")
                                if not text:
                                    completed = _completed_response_text(event)
                                    if completed:
                                        text.append(completed)
                                        yield "token", completed
                                finish_reason = "stop"
                            continue
                        choices = event.get("choices") or []
                        if not choices:
                            continue  # usage-only event
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        if delta.get("tool_calls"):
                            raise DirectAPIError("网关要求执行工具，但 API 直连模式没有本机执行工具")
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(reasoning, str) and reasoning:
                            yield "thinking", reasoning
                        token = delta.get("content")
                        if isinstance(token, str) and token:
                            text.append(token)
                            yield "token", token
                        finish_reason = choice.get("finish_reason") or finish_reason
                    except DirectAPIError:
                        raise
                    except (ValueError, TypeError, KeyError, AttributeError):
                        raise DirectAPIError("API 返回了错误或无法解析的流数据") from None
    except httpx.TimeoutException:
        raise DirectAPIError("API 请求超时，请检查网关或重试") from None
    except httpx.HTTPError:
        raise DirectAPIError("API 连接失败，请确认网关正在运行且地址可达") from None
    if finish_reason == "length":
        raise DirectAPIError("回复达到模型长度上限，已保留收到的内容，请重试或缩小请求")
    if finish_reason not in (None, "stop"):
        raise DirectAPIError("模型未正常完成回复，请检查网关的 finish_reason")
    if not text or finish_reason != "stop":
        raise DirectAPIError("API 流中断或返回空回复，请重试")
    if history_file is not None:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = history_file.with_suffix(".tmp")
        temporary.write_text(json.dumps([*history, user, {"role": "assistant", "content": "".join(text)}],
                                        ensure_ascii=False), encoding="utf-8")
        temporary.replace(history_file)
    yield "finish", "stop"
