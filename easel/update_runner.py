"""独立更新进程：拉取、安装、构建并在成功后重启 Easel Web。"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from easel import update


class Runner:
    def __init__(self, job_id: str, from_commit: str, server_pid: int, port: int) -> None:
        self.root = update.PROJECT_ROOT
        self.job_id = job_id
        self.from_commit = from_commit
        self.server_pid = server_pid
        self.port = port
        self.status = update.read_status()
        if self.status.get("jobId") != job_id:
            raise RuntimeError("更新任务状态文件不匹配")
        # Windows 的 .venv\Scripts\python.exe 可能是启动器，外层 Popen PID 会立即退出。
        # 以真正执行更新的当前进程 PID 为准，避免被误判成僵尸任务。
        self.status["runnerPid"] = os.getpid()
        self.save()
        self.merged = False
        self.restart_started = False

    def save(self) -> None:
        update.write_status(self.status)

    def log(self, line: str) -> None:
        update.append_log(self.status, line)

    def step(self, step_id: str, state: str, detail: str = "") -> None:
        update.set_step(self.status, step_id, state, detail)

    def run(self, argv: list[str], *, cwd: Path | None = None, timeout: int = 3600) -> None:
        printable = " ".join(argv)
        self.log(f"$ {printable}")
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd or self.root),
            env=update._command_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        lines: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        threading.Thread(target=read_output, daemon=True).start()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait(timeout=10)
                raise update.UpdateError(f"命令超时（{timeout}s）：{printable}")
            try:
                line = lines.get(timeout=min(1.0, remaining))
            except queue.Empty:
                continue
            if line is None:
                break
            self.log(line.rstrip())
        rc = proc.wait()
        if rc != 0:
            raise update.UpdateError(f"命令失败（退出码 {rc}）：{printable}")

    def fail(self, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}" if not isinstance(error, update.UpdateError) else str(error)
        self.status["state"] = "fail"
        self.status["error"] = message
        self.status["detail"] = message
        self.status["endedAt"] = update._now()
        active = self.status.get("step")
        if active:
            update.set_step(self.status, active, "fail", message)
        else:
            self.save()
        self.log(f"[失败] {message}")

    def rollback(self) -> None:
        if not self.merged:
            return
        self.log(f"安装或构建失败，回滚源码到 {update._short(self.from_commit)}…")
        try:
            if update._dirty_files(self.root):
                self.log("检测到安装期间出现新的工作区修改，已跳过自动回滚，避免覆盖用户文件。")
                return
            self.run(["git", "-C", str(self.root), "reset", "--hard", self.from_commit])
            self.status["toCommit"] = self.from_commit
            self.save()
        except Exception as exc:  # noqa: BLE001
            self.log(f"源码回滚失败，请手动检查 Git 状态：{exc}")

    def _changed_paths(self) -> list[str]:
        proc = update._git(
            self.root,
            ["diff", "--name-only", "-z", "HEAD", "FETCH_HEAD"],
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise update.UpdateError(detail[-500:] or "无法读取远程变更文件清单")
        return [path for path in (proc.stdout or "").split("\0") if path]

    def _backup_dist(self) -> Path | None:
        dist = self.root / "web" / "frontend" / "dist"
        if not dist.is_dir():
            return None
        backup = update.UPDATE_DIR / "dist-backup"
        if backup.exists():
            shutil.rmtree(backup)
        shutil.copytree(dist, backup)
        return backup

    def _restore_dist(self, backup: Path | None) -> None:
        if backup is None or not backup.is_dir():
            return
        dist = self.root / "web" / "frontend" / "dist"
        if dist.exists():
            shutil.rmtree(dist)
        shutil.copytree(backup, dist)

    def _wait_health(self, timeout: int = 90) -> bool:
        url = f"http://127.0.0.1:{self.port}/api/update/status"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    data = json.load(response)
                    if response.status == 200 and data.get("jobId") == self.job_id:
                        return True
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(1)
        return False

    def _port_open(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", self.port)) == 0

    def _windows_port_owner(self) -> int | None:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if (
                len(parts) >= 5
                and parts[0].upper() == "TCP"
                and parts[1].endswith(f":{self.port}")
                and parts[3].upper().startswith("LISTEN")
                and parts[4].isdigit()
            ):
                return int(parts[4])
        return None

    def _stop_server(self) -> None:
        if self.server_pid <= 0:
            return
        self.log(f"停止旧 Web 进程 PID {self.server_pid}…")
        if os.name == "nt":
            owner = self._windows_port_owner()
            if owner is None:
                if self._port_open():
                    raise update.UpdateError(f"端口 {self.port} 已被未知进程占用，已停止重启。")
                self.log("旧服务已退出，无需再次终止。")
                return
            if owner != self.server_pid:
                raise update.UpdateError(
                    f"端口 {self.port} 当前由 PID {owner} 监听，与预期 PID "
                    f"{self.server_pid} 不一致；为避免误杀其他进程，已停止重启。"
                )
            subprocess.run(
                ["taskkill", "/PID", str(self.server_pid), "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        else:
            try:
                os.kill(self.server_pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and self._port_open():
            time.sleep(0.5)

    def _start_server(self) -> subprocess.Popen:
        restart_log = update.UPDATE_DIR / "web-restart.log"
        restart_log.parent.mkdir(parents=True, exist_ok=True)
        handle = restart_log.open("ab")
        argv = [
            sys.executable,
            "-m",
            "easel.cli",
            "web",
            "--port",
            str(self.port),
        ]
        env = update._command_env()
        env["EASEL_PORT"] = str(self.port)
        # Windows 中文系统的子进程重定向到文件时默认可能回退 GBK；
        # web/app.py 启动横幅含 Unicode 符号，必须显式使用 UTF-8。
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        kwargs = {
            "cwd": str(self.root),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": handle,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            try:
                proc = subprocess.Popen(
                    argv,
                    creationflags=0x00000008 | 0x00000200 | 0x01000000,
                    **kwargs,
                )
            except OSError:
                proc = subprocess.Popen(argv, creationflags=0x00000008 | 0x00000200, **kwargs)
        else:
            proc = subprocess.Popen(argv, start_new_session=True, **kwargs)
        handle.close()
        self.status["restart"]["log"] = str(restart_log)
        self.save()
        return proc

    def restart(self) -> None:
        self.step("restart", "active", "正在重启 Easel Web…")
        self.status["state"] = "restarting"
        self.status["restart"] = {
            "attempted": True,
            "ok": False,
            "log": str(update.UPDATE_DIR / "web-restart.log"),
        }
        self.save()
        self._stop_server()
        proc = self._start_server()
        self.status["restart"]["pid"] = proc.pid
        self.save()
        if not self._wait_health(90):
            raise update.UpdateError(
                f"新服务未在 90 秒内恢复，请查看 {update.UPDATE_DIR / 'web-restart.log'}"
            )
        self.status["restart"]["ok"] = True
        self.step("restart", "done", "服务已恢复")

    def execute(self) -> None:
        backup: Path | None = None
        try:
            self.step("fetch", "active", "正在读取 Fork 最新提交…")
            repo_url = update.update_repo()
            branch = update.update_branch()
            self.run(["git", "-C", str(self.root), "fetch", repo_url, branch], timeout=180)
            remote = update._git_text(self.root, ["rev-parse", "FETCH_HEAD"]).lower()
            self.status["toCommit"] = remote
            self.save()
            if remote == self.from_commit:
                self.step("fetch", "done", "已是最新版本")
                for step_id, _label in update.STEP_DEFS[1:]:
                    update.set_step(self.status, step_id, "skipped", "无需更新")
                self.status.update({
                    "state": "ok",
                    "detail": "当前已是最新版本，无需安装或重启。",
                    "endedAt": update._now(),
                })
                self.save()
                return
            self.step("fetch", "done", f"发现新版本 {update._short(remote)}")

            blocked = update.protected_paths(self._changed_paths())
            if blocked:
                raise update.UpdateError(
                    "更新包含受保护的本机数据路径，已停止：" + "、".join(blocked[:8])
                )

            git_status = update._git_text(self.root, ["status", "--porcelain=v1"])
            if git_status.strip():
                raise update.UpdateError("拉取前检测到工作区变为非干净状态，已停止更新。")

            self.step("pull", "active", f"快进到 {update._short(remote)}")
            self.run(
                ["git", "-C", str(self.root), "merge", "--ff-only", "FETCH_HEAD"],
                timeout=180,
            )
            self.merged = True
            self.step("pull", "done", f"代码已更新到 {update._short(remote)}")

            self.step("python", "active", "正在安装 Python 依赖…")
            self.run(
                [sys.executable, "-m", "pip", "install", "-e", str(self.root), "--prefer-binary"],
                timeout=1800,
            )
            self.step("python", "done", "Python 依赖已就绪")

            pm, pm_name = update.package_manager(self.root)
            frontend = self.root / "web" / "frontend"
            install_args = [*pm, "install"]
            if pm_name == "npm":
                if (frontend / "package-lock.json").is_file():
                    install_args = [*pm, "ci"]
                install_args.extend(["--no-audit", "--no-fund"])
            else:
                install_args.append("--frozen-lockfile")
            self.step("frontend", "active", f"正在通过 {pm_name} 安装前端依赖…")
            self.run(install_args, cwd=frontend, timeout=1800)
            self.step("frontend", "done", "前端依赖已就绪")

            backup = self._backup_dist()
            self.step("build", "active", "正在构建 Web 前端…")
            self.run([*pm, "run", "build"], cwd=frontend, timeout=1200)
            self.step("build", "done", "前端构建完成")

            if self.server_pid > 0:
                self.step("restart", "active", "准备重启 Easel Web…")
                self.restart_started = True
                self.restart()
                detail = f"已更新到 {update._short(remote)}，服务已自动重启。"
            else:
                self.step("restart", "skipped", "未提供旧服务 PID，请手动重启")
                detail = f"已更新到 {update._short(remote)}，请手动重启 Easel Web。"
            self.status.update({
                "state": "ok",
                "detail": detail,
                "endedAt": update._now(),
            })
            self.save()
        except Exception as exc:  # noqa: BLE001
            if not self.restart_started:
                self._restore_dist(backup)
                self.rollback()
            self.fail(exc)
        finally:
            self.status["runnerPid"] = None
            self.save()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="easel.update_runner")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--from-commit", required=True)
    parser.add_argument("--server-pid", type=int, default=0)
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args(argv)
    try:
        Runner(args.job_id, args.from_commit, args.server_pid, args.port).execute()
    except Exception as exc:  # noqa: BLE001
        print(f"update runner failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
