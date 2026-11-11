"""Collect real Xiaohongshu notes through the locally authenticated xhs CLI."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from easel.trends.protocol import utc_now, validate_payload

MAX_SCOPES = 8
MAX_SCOPE_LENGTH = 40
MAX_ITEMS = 180
MAX_SECONDS = 55
COMMAND_TIMEOUT = 20
VERIFY_STATE_PATH = (Path(__file__).resolve().parents[2] / "outputs" / "_trends"
                     / "xiaohongshu-verification.json")
VERIFY_URL = "https://www.xiaohongshu.com/website-login/captcha"
EXPLORE_URL = "https://www.xiaohongshu.com/explore"


class XhsVerificationRequired(RuntimeError):
    def __init__(self, state: str, message: str):
        self.state = state
        self.message = message
        super().__init__(message)


def _write_verification(state: str, *, verify_uuid: str = "", verify_type: str = "",
                        message: str = "", scope: str = "") -> None:
    if state == "required" and verify_uuid and verify_type:
        query = urlencode({
            "redirectPath": EXPLORE_URL,
            "verifyUuid": verify_uuid,
            "verifyType": verify_type,
            "verifyBiz": "461",
        })
        url = f"{VERIFY_URL}?{query}"
    else:
        url = EXPLORE_URL
    payload = {
        "state": state,
        "cause": state,
        "message": message,
        "url": url,
        "verify_uuid": verify_uuid,
        "verify_type": verify_type,
        "scope": scope,
        "updated_at": int(time.time()),
    }
    try:
        VERIFY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = VERIFY_STATE_PATH.with_name(
            f"{VERIFY_STATE_PATH.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(VERIFY_STATE_PATH)
    except OSError:
        pass


def _clear_verification() -> None:
    try:
        VERIFY_STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _cli_path() -> Path:
    configured = os.environ.get("EASEL_XHS_CLI", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path.home() / ".local" / "bin" / "xhs.exe",
        Path(shutil.which("xhs.exe") or ""),
        Path(shutil.which("xhs") or ""),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    raise RuntimeError("xhs CLI is not installed")


def _repair_search_cache() -> None:
    """Discard only the disposable search-session cache when a legacy code page corrupted it."""
    path = Path.home() / ".xiaohongshu-cli" / "search_sessions.json"
    if not path.is_file():
        return
    try:
        path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError):
        try:
            path.unlink()
        except OSError:
            pass


def _cli_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONLEGACYWINDOWSSTDIO": "0",
        "NO_COLOR": "1",
    })
    return environment


def _run_json(cli: Path, arguments: list[str], timeout: float) -> dict | list:
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = subprocess.run(
            [str(cli), *arguments, "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(2, timeout),
            env=_cli_environment(),
            shell=False,
            check=False,
            **flags,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("xhs CLI request failed") from error
    if not result.stdout:
        raise RuntimeError("xhs CLI returned an error")
    try:
        payload = json.loads(result.stdout.decode("utf-8-sig").strip())
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("xhs CLI returned invalid JSON") from error
    if isinstance(payload, dict) and payload.get("ok") is False:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = error.get("code") if isinstance(error.get("code"), str) else ""
        message = error.get("message") if isinstance(error.get("message"), str) else ""
        if code == "verification_required":
            uuid_match = re.search(r"uuid=([A-Za-z0-9-]+)", message)
            type_match = re.search(r"type=([A-Za-z0-9_-]+)", message)
            _write_verification(
                "required",
                verify_uuid=uuid_match.group(1) if uuid_match else "",
                verify_type=type_match.group(1) if type_match else "",
                message="小红书触发安全验证，请在打开的浏览器窗口完成验证",
            )
            raise XhsVerificationRequired(
                "required", "小红书触发安全验证")
        if code == "ip_blocked":
            _write_verification(
                "blocked", message="小红书当前限制了本机网络，请切换网络后重试")
            raise XhsVerificationRequired(
                "blocked", "小红书当前限制了本机网络")
        if code == "not_authenticated":
            _write_verification(
                "login_required", message="小红书登录态已失效，请重新登录")
            raise XhsVerificationRequired(
                "login_required", "小红书登录态已失效")
        raise RuntimeError("xhs CLI rejected the request")
    if result.returncode != 0:
        raise RuntimeError("xhs CLI returned an error")
    if isinstance(payload, dict) and "data" in payload:
        payload = payload["data"]
    if not isinstance(payload, (dict, list)):
        raise RuntimeError("xhs CLI returned an unexpected payload")
    return payload


def _number(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if not isinstance(value, str):
        return 0
    text = value.strip().replace(",", "").replace("+", "")
    match = re.match(r"^(\d+(?:\.\d+)?)\s*(万|w|W|亿)?", text)
    if not match:
        return 0
    number = float(match.group(1))
    unit = match.group(2)
    if unit in ("万", "w", "W"):
        number *= 10_000
    elif unit == "亿":
        number *= 100_000_000
    return max(0, int(number))


def _format_count(value: int) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return str(value)


def _items(payload: dict | list) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    for key in ("items", "notes"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
    return []


def _note(item: dict, scope: str) -> dict | None:
    card = item.get("note_card") if isinstance(item.get("note_card"), dict) else {}
    title = card.get("display_title") or item.get("display_title") or card.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    identifier = item.get("id") or card.get("note_id")
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    interaction = card.get("interact_info") if isinstance(card.get("interact_info"), dict) else {}
    likes = _number(interaction.get("liked_count"))
    collected = _number(interaction.get("collected_count"))
    comments = _number(interaction.get("comment_count"))
    token = item.get("xsec_token") or card.get("xsec_token")
    token = token if isinstance(token, str) else ""
    source = "pc_search" if scope else "pc_feed"
    url = f"https://www.xiaohongshu.com/explore/{quote(identifier, safe='')}?xsec_source={source}"
    if token:
        url += f"&xsec_token={quote(token, safe='')}"
    context = " · ".join(part for part in (
        scope,
        f"藏{_format_count(collected)}" if collected else "",
        f"评{_format_count(comments)}" if comments else "",
    ) if part)
    heat_text = f"{_format_count(likes)}赞" + (f" · {context}" if context else "")
    return {
        "id": identifier.strip(),
        "title": title.strip(),
        "url": url,
        "published_at": None,
        "heat": {"value": likes, "text": heat_text, "unit": "赞"},
    }


def _merge(results: list[list[dict]]) -> list[dict]:
    by_id: dict[str, dict] = {}
    for rows in results:
        for row in rows:
            existing = by_id.get(row["id"])
            if existing is None or row["heat"]["value"] > existing["heat"]["value"]:
                by_id[row["id"]] = row
    return sorted(
        by_id.values(),
        key=lambda row: (-row["heat"]["value"], row["title"]),
    )[:MAX_ITEMS]


def _valid_scopes(values: list[str]) -> list[str]:
    scopes: list[str] = []
    for value in values:
        scope = value.strip()
        if not scope or len(scope) > MAX_SCOPE_LENGTH or any(ord(char) < 32 for char in scope):
            raise ValueError("invalid scope")
        if scope not in scopes:
            scopes.append(scope)
    if len(scopes) > MAX_SCOPES:
        raise ValueError("too many scopes")
    return scopes


def collect(scopes: list[str]) -> dict:
    cli = _cli_path()
    _repair_search_cache()
    deadline = time.monotonic() + MAX_SECONDS
    requests = scopes or [""]
    collected: list[list[dict]] = []
    failures = 0
    for scope in requests:
        remaining = deadline - time.monotonic()
        if remaining <= 2:
            failures += 1
            break
        arguments = ["search", scope, "--sort", "popular"] if scope else ["hot", "-c", "home"]
        try:
            payload = _run_json(cli, arguments, min(COMMAND_TIMEOUT, remaining))
            rows = [note for item in _items(payload) if (note := _note(item, scope))]
            collected.append(rows)
        except XhsVerificationRequired:
            raise
        except RuntimeError:
            failures += 1
    items = _merge(collected)
    if not items:
        raise RuntimeError("no Xiaohongshu notes collected")
    _clear_verification()
    payload = {"version": 1, "fetched_at": utc_now(), "items": items}
    validate_payload(payload)
    if failures:
        print(f"xiaohongshu: {failures} scope request(s) failed", file=sys.stderr)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--scope", action="append", default=[])
    try:
        arguments = parser.parse_args()
        payload = collect(_valid_scopes(arguments.scope))
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError):
        print("xiaohongshu collector failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
