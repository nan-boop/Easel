"""Easel 自更新：只从可信 Fork 拉取，保护本机配置与用户数据。"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any


DEFAULT_UPDATE_REPO = "https://github.com/nan-boop/Easel.git"
DEFAULT_UPDATE_BRANCH = "main"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
UPDATE_DIR = PROJECT_ROOT / "outputs" / "_update"
STATUS_FILE = UPDATE_DIR / "status.json"
LOG_FILE = UPDATE_DIR / "update.log"
_START_LOCK = threading.Lock()

ACTIVE_STATES = frozenset({"running", "restarting"})

STEP_DEFS = (
    ("fetch", "检查远程版本"),
    ("pull", "拉取并快进代码"),
    ("python", "安装 Python 依赖"),
    ("frontend", "安装前端依赖"),
    ("build", "构建 Web 前端"),
    ("restart", "重启 Easel 服务"),
)


class UpdateError(RuntimeError):
    """可直接展示给用户的更新错误。"""


def update_repo() -> str:
    return (os.environ.get("EASEL_UPDATE_REPO_URL") or DEFAULT_UPDATE_REPO).strip()


def update_branch() -> str:
    return (os.environ.get("EASEL_UPDATE_BRANCH") or DEFAULT_UPDATE_BRANCH).strip()


def _steps() -> list[dict[str, str]]:
    return [
        {"id": step_id, "label": label, "state": "pending", "detail": ""}
        for step_id, label in STEP_DEFS
    ]


def _now() -> int:
    return int(time.time())


def _short(commit: str) -> str:
    return (commit or "")[:8]


def _project_version(root: Path = PROJECT_ROOT) -> str:
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else ""


def _git(root: Path, args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(root), *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_command_env(),
        )
    except FileNotFoundError as exc:
        raise UpdateError("未找到 Git，请先安装 Git 并加入 PATH。") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"Git 操作超时：git {' '.join(args)}") from exc


def _git_text(root: Path, args: list[str], *, timeout: int = 60) -> str:
    proc = _git(root, args, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise UpdateError(detail[-500:] or f"Git 命令失败：git {' '.join(args)}")
    return (proc.stdout or "").strip()


def _command_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _current_commit(root: Path) -> str:
    return _git_text(root, ["rev-parse", "HEAD"])


def _current_branch(root: Path) -> str:
    return _git_text(root, ["branch", "--show-current"])


def _dirty_files(root: Path) -> list[str]:
    proc = _git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=normal"])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise UpdateError(detail[-500:] or "无法读取 Git 工作区状态")
    output = proc.stdout or ""
    if not output:
        return []
    files: list[str] = []
    entries = output.split("\0")
    index = 0
    while index < len(entries):
        line = entries[index]
        index += 1
        if not line:
            continue
        path = line[3:] if len(line) > 3 else line
        if line[:2] in {"R ", "RM", "R?", "C ", "CM", "C?"} and index < len(entries):
            path = entries[index]
            index += 1
        if path:
            files.append(path)
    return files


def _is_git_repo(root: Path) -> bool:
    if not root.is_dir():
        return False
    proc = _git(root, ["rev-parse", "--is-inside-work-tree"])
    return proc.returncode == 0 and (proc.stdout or "").strip() == "true"


def _remote_state(repo_url: str, branch: str, root: Path) -> dict[str, Any]:
    """Fetch only the target branch, then verify the update is a fast-forward."""
    _git_text(root, ["fetch", repo_url, branch], timeout=180)
    remote = _git_text(root, ["rev-parse", "FETCH_HEAD"]).lower()
    local = _current_commit(root)
    counts = _git_text(root, ["rev-list", "--left-right", "--count", "HEAD...FETCH_HEAD"])
    parts = counts.split()
    ahead = int(parts[0]) if parts and parts[0].isdigit() else 0
    behind = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    ancestor = _git(root, ["merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD"])
    if ancestor.returncode not in {0, 1}:
        detail = (ancestor.stderr or ancestor.stdout or "").strip()
        raise UpdateError(detail[-500:] or "无法判断远程提交是否可快进")
    return {
        "remoteCommit": remote,
        "localAhead": ahead,
        "remoteAhead": behind,
        "fastForward": ancestor.returncode == 0 and local != remote,
        "sameCommit": local == remote,
    }


def package_manager(root: Path = PROJECT_ROOT) -> tuple[list[str], str]:
    """返回前端包管理器命令前缀与类型；优先使用仓库自带的 npm lock。"""
    frontend = root / "web" / "frontend"
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    pnpm = shutil.which("pnpm.cmd") or shutil.which("pnpm")
    if npm and (frontend / "package-lock.json").is_file():
        return [npm], "npm"
    if pnpm and (frontend / "pnpm-lock.yaml").is_file():
        return [pnpm], "pnpm"
    if npm:
        return [npm], "npm"
    if pnpm:
        return [pnpm], "pnpm"
    raise UpdateError("未找到 npm 或 pnpm，请先安装 Node.js/npm。")


def _preflight(root: Path, *, require_update: bool = False) -> dict[str, Any]:
    if not _is_git_repo(root):
        raise UpdateError(f"项目目录不是 Git 仓库：{root}")
    branch = _current_branch(root)
    target = update_branch()
    if not branch:
        raise UpdateError("当前仓库处于 detached HEAD，无法安全更新。")
    if branch != target:
        raise UpdateError(f"当前分支是 {branch}，更新分支固定为 {target}，请先切换分支。")
    dirty = _dirty_files(root)
    if dirty:
        preview = "、".join(dirty[:6])
        more = f" 等 {len(dirty)} 项" if len(dirty) > 6 else ""
        raise UpdateError(f"本地有未提交修改：{preview}{more}。请先提交或备份后再更新。")
    commit = _current_commit(root)
    pm, pm_name = package_manager(root)
    if shutil.which("node") is None:
        raise UpdateError("未找到 Node.js，无法构建 Web 前端。")
    result = {
        "root": str(root),
        "branch": branch,
        "targetBranch": target,
        "currentCommit": commit,
        "packageManager": pm_name,
        "packageManagerCommand": pm[0],
    }
    if require_update:
        repo_url = update_repo()
        remote_state = _remote_state(repo_url, target, root)
        result.update(remote_state)
        result["updateAvailable"] = not remote_state["sameCommit"]
        if remote_state["sameCommit"]:
            raise UpdateError("当前已是最新版本。")
        if not remote_state["fastForward"]:
            raise UpdateError("远程分支未包含本地最新提交，请先把本地提交推送到 Fork。")
    return result


def _idle_status() -> dict[str, Any]:
    return {
        "jobId": None,
        "state": "idle",
        "step": "",
        "detail": "",
        "steps": _steps(),
        "logs": [],
        "startedAt": None,
        "endedAt": None,
        "fromCommit": "",
        "toCommit": "",
        "error": "",
        "source": update_repo(),
        "branch": update_branch(),
        "restart": {"attempted": False, "ok": False, "log": ""},
        "updatedAt": _now(),
    }


def write_status(status: dict[str, Any]) -> None:
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    status["updatedAt"] = _now()
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(6):
        try:
            os.replace(tmp, STATUS_FILE)
            return
        except PermissionError:
            if attempt >= 5:
                raise
            time.sleep(0.05)


def read_status() -> dict[str, Any]:
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _idle_status()
    if not isinstance(data, dict):
        return _idle_status()
    base = _idle_status()
    base.update(data)
    if not isinstance(base.get("steps"), list):
        base["steps"] = _steps()
    if not isinstance(base.get("logs"), list):
        base["logs"] = []
    if not isinstance(base.get("restart"), dict):
        base["restart"] = {"attempted": False, "ok": False, "log": ""}
    pid = base.get("runnerPid")
    updated = base.get("updatedAt")
    if (
        base.get("state") in ACTIVE_STATES
        and isinstance(pid, int)
        and pid > 0
        and isinstance(updated, int)
        and _now() - updated > 15
        and not _pid_alive(pid)
    ):
        message = "更新后台进程已退出，任务未正常完成。请查看日志后重试。"
        base["state"] = "fail"
        base["detail"] = message
        base["error"] = message
        base["endedAt"] = _now()
        step_id = base.get("step")
        for step in base.get("steps") or []:
            if step.get("id") == step_id:
                step["state"] = "fail"
                step["detail"] = message
                break
        base["logs"] = (list(base.get("logs") or []) + [f"[失败] {message}"])[-100:]
        write_status(base)
    elif (
        base.get("state") in ACTIVE_STATES
        and not isinstance(pid, int)
        and isinstance(updated, int)
        and _now() - updated > 60
    ):
        message = "更新后台进程未能启动，请检查日志后重试。"
        base["state"] = "fail"
        base["detail"] = message
        base["error"] = message
        base["endedAt"] = _now()
        step_id = base.get("step")
        for step in base.get("steps") or []:
            if step.get("id") == step_id:
                step["state"] = "fail"
                step["detail"] = message
                break
        base["logs"] = (list(base.get("logs") or []) + [f"[失败] {message}"])[-100:]
        write_status(base)
    return base


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def append_log(status: dict[str, Any], line: str) -> None:
    text = (line or "").rstrip()
    if not text:
        return
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")
    status["logs"] = (list(status.get("logs") or []) + [text])[-100:]
    write_status(status)


def set_step(
    status: dict[str, Any],
    step_id: str,
    state: str,
    detail: str = "",
) -> None:
    for step in status.get("steps") or []:
        if step.get("id") == step_id:
            step["state"] = state
            if detail:
                step["detail"] = detail
            break
    status["step"] = step_id
    status["detail"] = detail
    write_status(status)


def current_snapshot(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """不访问网络的当前状态，供设置页首屏快速展示。"""
    job = read_status()
    data: dict[str, Any] = {
        "source": update_repo(),
        "branch": update_branch(),
        "version": _project_version(root),
        "currentCommit": "",
        "currentShort": "",
        "remoteCommit": "",
        "remoteShort": "",
        "updateAvailable": False,
        "localAhead": 0,
        "remoteAhead": 0,
        "fastForward": False,
        "clean": False,
        "branchMatches": False,
        "dirtyFiles": [],
        "canApply": False,
        "blockedReason": "",
        "job": job,
    }
    try:
        if not _is_git_repo(root):
            data["blockedReason"] = "项目目录不是 Git 仓库"
            return data
        commit = _current_commit(root)
        branch = _current_branch(root)
        dirty = _dirty_files(root)
        data.update({
            "currentCommit": commit,
            "currentShort": _short(commit),
            "clean": not dirty,
            "dirtyFiles": dirty[:30],
            "branchMatches": branch == update_branch(),
        })
        pm, pm_name = package_manager(root)
        data["packageManager"] = pm_name
        data["packageManagerCommand"] = pm[0]
        if branch != update_branch():
            data["blockedReason"] = f"当前分支为 {branch or 'detached HEAD'}，请切换到 {update_branch()}"
        elif dirty:
            data["blockedReason"] = "本地有未提交修改，请先提交或备份"
        elif job.get("state") in ACTIVE_STATES:
            data["blockedReason"] = "已有更新任务正在运行"
        else:
            # 必须先在设置页点一次「检查更新」拿到远程提交，避免未检查就启动更新。
            data["canApply"] = False
    except UpdateError as exc:
        data["blockedReason"] = str(exc)
    return data


def check_update(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """读取 Fork 最新提交；不修改本地工作区。"""
    data = current_snapshot(root)
    if not _is_git_repo(root):
        raise UpdateError(data.get("blockedReason") or "项目目录不是 Git 仓库")
    commit = data["currentCommit"] or _current_commit(root)
    remote_state = _remote_state(update_repo(), update_branch(), root)
    remote = remote_state["remoteCommit"]
    available = not remote_state["sameCommit"]
    data.update({
        "remoteCommit": remote,
        "remoteShort": _short(remote),
        "updateAvailable": available,
        "localAhead": remote_state["localAhead"],
        "remoteAhead": remote_state["remoteAhead"],
        "fastForward": remote_state["fastForward"],
    })
    if data.get("job", {}).get("state") in ACTIVE_STATES:
        data["canApply"] = False
        data["blockedReason"] = "已有更新任务正在运行"
    elif not data.get("clean"):
        data["canApply"] = False
    elif not available:
        data["canApply"] = False
        data["blockedReason"] = ""
    elif not remote_state["fastForward"]:
        data["canApply"] = False
        data["blockedReason"] = "远程分支未包含本地最新提交，请先把本地提交推送到 Fork"
    else:
        data["canApply"] = bool(data.get("branchMatches"))
    return data


def _detached_popen(argv: list[str], *, cwd: Path, log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab")
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": _command_env(),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }
    try:
        if os.name == "nt":
            detached = 0x00000008
            new_group = 0x00000200
            breakaway = 0x01000000
            try:
                proc = subprocess.Popen(argv, creationflags=detached | new_group | breakaway, **kwargs)
            except OSError:
                proc = subprocess.Popen(argv, creationflags=detached | new_group, **kwargs)
        else:
            proc = subprocess.Popen(argv, start_new_session=True, **kwargs)
        return proc
    finally:
        log_handle.close()


def start_update(
    *,
    root: Path = PROJECT_ROOT,
    server_pid: int | None = None,
    port: int = 7860,
) -> dict[str, Any]:
    with _START_LOCK:
        job = read_status()
        if job.get("state") in ACTIVE_STATES:
            raise UpdateError("已有更新任务正在运行，请等待完成。")
        preflight = _preflight(root, require_update=True)
        job_id = uuid.uuid4().hex[:16]
        status = _idle_status()
        status.update({
            "jobId": job_id,
            "state": "running",
            "step": "fetch",
            "detail": "已提交更新任务，正在启动后台工作进程…",
            "startedAt": _now(),
            "fromCommit": preflight["currentCommit"],
            "source": update_repo(),
            "branch": update_branch(),
            "steps": _steps(),
        })
        set_step(status, "fetch", "active", "准备后台更新")
        write_status(status)

        argv = [
            sys.executable,
            "-m",
            "easel.update_runner",
            "--job-id",
            job_id,
            "--from-commit",
            preflight["currentCommit"],
            "--server-pid",
            str(server_pid or 0),
            "--port",
            str(port),
        ]
        try:
            proc = _detached_popen(argv, cwd=root, log_path=UPDATE_DIR / "runner.log")
        except Exception as exc:  # noqa: BLE001
            status["state"] = "fail"
            status["error"] = f"无法启动更新进程：{type(exc).__name__}: {exc}"
            status["endedAt"] = _now()
            set_step(status, "fetch", "fail", status["error"])
            raise UpdateError(status["error"]) from exc
        # 不要在 Popen 后再写一次状态：Windows 启动器 PID 与真实 runner PID 不同，
        # 且 runner 可能已经开始写状态，这里的旧快照会把它覆盖掉。
        status["launcherPid"] = proc.pid
        return status


def protected_paths(paths: list[str]) -> list[str]:
    """从远程变更清单中找出会触碰本机数据的路径。"""
    blocked: list[str] = []
    for raw in paths:
        path = raw.replace("\\", "/").strip()
        if not path:
            continue
        key = path.casefold()
        if key in {".env", ".env.local", ".env.xhs"}:
            blocked.append(path)
        elif key.startswith("outputs/"):
            blocked.append(path)
        elif key.startswith("profiles/") and not key.startswith("profiles/_template/"):
            blocked.append(path)
        elif key in {"cookies.json", "config/trend-sources.json", "config/trend-scripts.json"}:
            blocked.append(path)
        elif key.endswith(".jsonl") or key.startswith(".openclaw/"):
            blocked.append(path)
    return blocked
