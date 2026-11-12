"""Codex native app-server adapter with optional isolated Responses provider."""
from __future__ import annotations

import asyncio
from dataclasses import replace

from easel import __version__

from . import store as harness_store
from .base import Descriptor, HarnessError, RunRequest
from .codex_config import app_server_command, custom_api
from .permissions import can_auto_approve
from .rpc import connection
from .workspace import harness_instructions

FULL_ACCESS_OPTION = "完全访问（以后不再询问）"


def _enable_full_access(request: RunRequest) -> None:
    config = harness_store.settings(request.cwd / "outputs", request.env)
    config["runtime"] = "codex"
    config["sandbox"] = "danger-full-access"
    config["codex_auto_approve"] = True
    harness_store.write_json(
        request.cwd / "outputs" / "_harness" / "config.json", config)


class CodexAdapter:
    descriptor = Descriptor("codex", "Codex", "app-server", "npm install -g @openai/codex；codex login")

    async def run(self, request: RunRequest, emit, ask, bind):
        if request.sandbox not in ("read-only", "workspace-write", "danger-full-access"):
            raise HarnessError("Codex 权限设置无效，请选择只读、工作区写入或完全访问")
        full_access = request.sandbox == "danger-full-access"
        effective_auto_approve = request.codex_auto_approve
        approval_policy = "never" if full_access else "on-request"
        completion = asyncio.get_running_loop().create_future()
        thread_id = request.native_id
        turn_id = ""
        streamed: set[str] = set()

        def notify(method, params):
            def _thread_scoped() -> bool:
                # 无 threadId/turnId 的是全局事件（连接级/认证失败常在 turn/start 前发出）；
                # 带 id 的必须匹配本会话。放行判定独立于 turn_id 是否已闩锁。
                if params.get("threadId") is None and params.get("turnId") is None:
                    return True
                return bool(turn_id) and params.get("threadId") == thread_id \
                    and params.get("turnId", turn_id) == turn_id

            # 致命 error 事件常不带 thread 上下文（如 turn/start 前的认证失败）；
            # 放在 id 过滤之前处理，否则只能等外层 wait_for 超时（30min+）才报错。
            if method == "error":
                if params.get("willRetry", False) or not _thread_scoped():
                    return
                if not completion.done():
                    completion.set_exception(HarnessError("Codex 执行失败，请检查原生登录与模型配置"))
                return

            if not turn_id or params.get("threadId") != thread_id or params.get("turnId", turn_id) != turn_id:
                return
            if method == "item/agentMessage/delta":
                streamed.add(params.get("itemId", ""))
                emit("token", params.get("delta", ""))
            elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
                emit("thinking", params.get("delta", ""))
            elif method == "item/started":
                item = params.get("item") or {}
                if item.get("type") not in ("agentMessage", "reasoning", "userMessage"):
                    emit("activity", f"工具：{item.get('type', 'tool')}")
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("id") not in streamed:
                    emit("token", item.get("text", ""))
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                if turn.get("id") != turn_id or completion.done():
                    return
                if turn.get("status") == "completed":
                    completion.set_result(None)
                else:
                    error = turn.get("error") or {}
                    completion.set_exception(HarnessError(
                        str(error.get("message") or f"Codex 本轮未完成：{turn.get('status')}")))

        async def respond(method, params):
            nonlocal full_access, effective_auto_approve
            if params.get("threadId") != thread_id or (
                    turn_id and params.get("turnId", turn_id) != turn_id):
                raise HarnessError("不属于当前会话的请求")
            if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                # 设置保存后立即生效：同一轮对话里的后续授权也必须重新读取本机策略，
                # 不能继续使用本轮启动时冻结的旧权限。
                try:
                    live = harness_store.settings(request.cwd / "outputs", request.env)
                except Exception:
                    live = {}
                if live.get("sandbox") == "danger-full-access":
                    full_access = True
                    effective_auto_approve = True
                elif not full_access:
                    effective_auto_approve = live.get(
                        "codex_auto_approve", effective_auto_approve)
                effective_request = replace(
                    request,
                    sandbox="danger-full-access" if full_access else request.sandbox,
                    codex_auto_approve=effective_auto_approve,
                )
                if can_auto_approve(method, params, effective_request):
                    emit("activity", "Codex：完全访问模式已自动授权"
                         if full_access else "Codex：已自动通过普通操作授权")
                    return {"decision": "accept"}
                detail = params.get("command") or params.get("reason") or params.get("itemId", "")
                risk_note = ("\n此操作不属于可自动批准的普通操作，请确认风险。"
                             if effective_auto_approve else "")
                answers = await ask([{
                    "questionId": "permission", "header": "执行授权",
                    "question": f"Codex 请求执行权限：{str(detail)[:3000]}{risk_note}"
                        "\n选择「完全访问」会把权限保存到本机，后续 Codex 对话不再逐次询问。",
                    "options": [
                        {"label": "允许本次"},
                        {"label": FULL_ACCESS_OPTION,
                         "description": "点击即启用：允许读写全机并运行命令；当前及后续对话不再弹出执行授权"},
                        {"label": "拒绝"},
                    ],
                }])
                choice = (answers.get("permission") or [""])[0]
                if choice == FULL_ACCESS_OPTION:
                    try:
                        _enable_full_access(request)
                    except Exception:
                        full_access = True
                        effective_auto_approve = True
                        emit("activity", "Codex：完全访问设置保存失败；当前对话已临时切换为自动授权")
                    else:
                        full_access = True
                        effective_auto_approve = True
                        emit("activity", "Codex：已切换为完全访问，后续执行将自动授权")
                    return {"decision": "accept"}
                return {"decision": "accept" if choice == "允许本次" else "decline"}
            if method == "item/tool/requestUserInput":
                questions = params.get("questions") or []
                if any(question.get("isSecret") for question in questions):
                    raise HarnessError("请在终端完成敏感凭证输入")
                normalized = [{**question, "questionId": question["id"]} for question in questions]
                answers = await ask(normalized)
                return {"answers": {key: {"answers": value} for key, value in answers.items()}}
            raise HarnessError("不支持的 Codex 客户端请求")

        if request.command:
            command = list(request.command)
        else:
            command = app_server_command(request, custom_api(request.env, request.model))
        async with connection(request, command, notify, respond) as rpc:
            if rpc.initialized is None:
                rpc.initialized = await rpc.call("initialize", {
                    "clientInfo": {"name": "easel", "version": __version__},
                    "capabilities": {"experimentalApi": True}})
                await rpc.send({"method": "initialized"})
            options = {"cwd": str(request.cwd), "approvalPolicy": approval_policy,
                       "sandbox": request.sandbox,
                       "developerInstructions": harness_instructions(request)}
            if request.model:
                options["model"] = request.model
            if thread_id and rpc.native_id == thread_id:
                result = {"thread": {"id": thread_id}}
            elif thread_id:
                result = await rpc.call("thread/resume", {**options, "threadId": thread_id})
            else:
                result = await rpc.call("thread/start", options)
            resumed_id = (result.get("thread") or {}).get("id", "")
            if request.native_id and resumed_id != request.native_id:
                raise HarnessError("Codex 返回了不同的原生会话 ID，不会替换已有历史")
            thread_id = resumed_id
            if not thread_id:
                raise HarnessError("Codex 未返回原生会话 ID")
            bind(thread_id)
            rpc.native_id = thread_id
            policy = ({"type": "dangerFullAccess"} if full_access
                      else {"type": "readOnly"} if request.sandbox == "read-only"
                      else {"type": "workspaceWrite", "writableRoots": [str(request.cwd)],
                            "networkAccess": False})
            params = {"threadId": thread_id, "input": [{"type": "text", "text": request.prompt}],
                      "approvalPolicy": approval_policy, "sandboxPolicy": policy}
            if request.model:
                params["model"] = request.model
            early: list[tuple[str, dict]] = []
            original_notify = rpc.notify
            rpc.notify = lambda method, data: early.append((method, data))
            result = await rpc.call("turn/start", params)
            turn_id = (result.get("turn") or {}).get("id", "")
            if not turn_id:
                raise HarnessError("Codex 未返回本轮 ID")
            rpc.notify = original_notify
            for method, data in early:
                notify(method, data)
            try:
                await rpc.wait_notification(completion)
            except asyncio.CancelledError:
                try:
                    await rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=2)
                except HarnessError:
                    pass
                raise
            finally:
                if completion.done() and not completion.cancelled():
                    completion.exception()
