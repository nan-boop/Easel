"""Isolated custom Responses provider configuration for Codex app-server."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from .base import HarnessError, RunRequest
from .commands import base_command

CODEX_CUSTOM_BASE_ENV = "EASEL_CODEX_API_BASE_URL"
CODEX_CUSTOM_KEY_ENV = "EASEL_CODEX_API_KEY"
CODEX_CUSTOM_PROTOCOL_ENV = "EASEL_CODEX_API_PROTOCOL"
CODEX_CUSTOM_PROVIDER = "easel-custom"

_PROTOCOL_ALIASES = {
    "": "openai-responses",
    "responses": "openai-responses",
    "openai-responses": "openai-responses",
    "openai-response": "openai-responses",
}


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def custom_api(env: dict[str, str], model: str = "", *, require_model: bool = True) -> dict | None:
    """Return validated custom API settings, or None when Codex native auth is selected."""
    base = str(env.get(CODEX_CUSTOM_BASE_ENV) or "").strip().rstrip("/")
    key = str(env.get(CODEX_CUSTOM_KEY_ENV) or "").strip()
    protocol_raw = str(env.get(CODEX_CUSTOM_PROTOCOL_ENV) or "").strip().lower()
    if not base and not key and not protocol_raw:
        return None
    if not base:
        raise HarnessError("Codex 自定义 API 缺少 Base URL")
    if not key:
        raise HarnessError("Codex 自定义 API 缺少 API Key")
    if any(character.isspace() or ord(character) < 32 for character in base):
        raise HarnessError("Codex 自定义 API 的 Base URL 不能包含空白或控制字符")
    try:
        parsed = urlsplit(base)
        valid_base = (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and parsed.port != 0
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid_base = False
    if not valid_base:
        raise HarnessError("Codex 自定义 API 的 Base URL 需形如 https://api.example.com/v1")
    if len(key) > 400 or any(character.isspace() or ord(character) < 32 for character in key):
        raise HarnessError("Codex 自定义 API 的 Key 格式不正确")
    protocol = _PROTOCOL_ALIASES.get(protocol_raw)
    if protocol != "openai-responses":
        raise HarnessError(
            "当前 Codex CLI 只支持 Responses API；需要 Chat Completions 时请使用"
            "“仅聊天模式”或 OpenClaw"
        )
    selected_model = str(model or "").strip()
    if require_model and not selected_model:
        raise HarnessError("使用 Codex 自定义 API 时必须填写模型名称")
    if len(selected_model) > 240 or any(ord(character) < 32 for character in selected_model):
        raise HarnessError("Codex 模型名称格式不正确")
    return {"base_url": base, "api_key": key, "protocol": protocol, "model": selected_model}


def provider_args(settings: dict, model: str) -> list[str]:
    """Build Codex CLI overrides without touching the user's global config.toml."""
    if not settings:
        return []
    overrides = {
        "model": model,
        "model_provider": CODEX_CUSTOM_PROVIDER,
        f"model_providers.{CODEX_CUSTOM_PROVIDER}.name": "Easel Custom API",
        f"model_providers.{CODEX_CUSTOM_PROVIDER}.base_url": settings["base_url"],
        f"model_providers.{CODEX_CUSTOM_PROVIDER}.env_key": CODEX_CUSTOM_KEY_ENV,
        f"model_providers.{CODEX_CUSTOM_PROVIDER}.wire_api": "responses",
    }
    arguments = []
    for key, value in overrides.items():
        arguments.extend(["-c", f"{key}={_toml_string(str(value))}"])
    return arguments


def app_server_command(request: RunRequest, settings: dict | None = None) -> list[str]:
    settings = settings if settings is not None else custom_api(request.env, request.model)
    return base_command("codex") + provider_args(settings, request.model) + ["app-server"]
