#!/usr/bin/env python3
"""Open a headed Xiaohongshu verification window and sync the CLI cookies."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import xhs_publish as xp  # noqa: E402

EXPLORE_URL = "https://www.xiaohongshu.com/explore"
CLI_COOKIE_PATH = Path.home() / ".xiaohongshu-cli" / "cookies.json"
COOKIE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def read_status(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_status(path: Path, state: str, message: str, *, url: str = "",
                 cookies: int | None = None, cause: str = "") -> None:
    previous = read_status(path)
    if not cause:
        cause = previous.get("cause") if isinstance(previous.get("cause"), str) else ""
    payload = {
        "state": state,
        "cause": cause or state,
        "message": message,
        "url": url[:2048],
        "updated_at": int(time.time()),
    }
    if cookies is not None:
        payload["cookies"] = cookies
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def read_cli_cookies() -> dict[str, str]:
    try:
        data = json.loads(CLI_COOKIE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items()
            if isinstance(key, str) and key != "saved_at"
            and isinstance(value, str) and COOKIE_NAME.fullmatch(key)}


def inject_cli_cookies(context, cookies: dict[str, str]) -> None:
    if not cookies:
        return
    rows = [{
        "name": name,
        "value": value,
        "domain": ".xiaohongshu.com",
        "path": "/",
        "expires": -1,
        "httpOnly": False,
        "secure": True,
        "sameSite": "Lax",
    } for name, value in cookies.items()]
    context.add_cookies(rows)


def sync_cli_cookies(context) -> int:
    try:
        current = json.loads(CLI_COOKIE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    changed = 0
    for cookie in context.cookies():
        domain = cookie.get("domain")
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(domain, str) or "xiaohongshu.com" not in domain:
            continue
        if not isinstance(name, str) or not COOKIE_NAME.fullmatch(name):
            continue
        if not isinstance(value, str):
            continue
        if current.get(name) != value:
            current[name] = value
            changed += 1
    current["saved_at"] = int(time.time())
    try:
        CLI_COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = CLI_COOKIE_PATH.with_name(f"{CLI_COOKIE_PATH.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        temporary.replace(CLI_COOKIE_PATH)
    except OSError:
        pass
    return changed


def logged_in(page) -> bool:
    try:
        return bool(page.evaluate("""() => {
          const state = window.__INITIAL_STATE__;
          const user = state?.user?.userPageData || state?.user?.userInfo;
          if (user?.basicInfo || user?.nickname || user?.userId) return true;
          return !!document.querySelector('.user .link-wrapper .channel, .side-bar-component .user, .user-info');
        }"""))
    except Exception:
        return False


def verification_passed(page, current_url: str) -> bool:
    if "website-login/captcha" not in current_url:
        return logged_in(page)
    try:
        return bool(page.evaluate(
            "() => /验证成功|通过验证|验证已完成/.test(document.body.innerText)"))
    except Exception:
        return False


def run(args) -> int:
    status_path = Path(args.status_file).expanduser()
    profile = xp._profile_dir(args.profile_base)
    lock = xp._ProfileLock(profile)
    try:
        lock.acquire(3)
    except TimeoutError:
        write_status(status_path, "busy",
                     "浏览器登录目录正被占用，请关闭账号页的登录/校验窗口后重试")
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        write_status(status_path, "failed", "Playwright 不可用，无法打开验证窗口")
        lock.release()
        return 3

    context = None
    try:
        with sync_playwright() as playwright:
            write_status(status_path, "starting", "正在打开小红书验证窗口")
            context = xp._launch(
                playwright,
                headed=True,
                base=args.profile_base,
                proxy=xp._proxy(args.proxy, args.no_proxy),
                login_assets=False,
            )
            inject_cli_cookies(context, read_cli_cookies())
            context.set_default_timeout(15000)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            page.wait_for_timeout(1200)
            target = args.url if args.url.startswith("https://www.xiaohongshu.com/") else EXPLORE_URL
            if target != EXPLORE_URL:
                try:
                    page.goto(target, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
            write_status(status_path, "waiting",
                         "请在弹出的浏览器中完成小红书验证", url=page.url)

            deadline = time.time() + max(60, min(int(args.timeout), 1800))
            last_heartbeat = 0.0
            while time.time() < deadline:
                if not context.pages or page.is_closed():
                    write_status(status_path, "closed", "验证窗口已关闭，可重新打开")
                    return 4
                current = page.url
                if "xiaohongshu.com" in current and verification_passed(page, current):
                    changed = sync_cli_cookies(context)
                    write_status(status_path, "success",
                                 "验证完成，登录态已同步", url=current, cookies=changed)
                    try:
                        page.wait_for_timeout(1200)
                    except Exception:
                        pass
                    return 0
                if time.time() - last_heartbeat >= 3:
                    write_status(status_path, "waiting",
                                 "请在弹出的浏览器中完成小红书验证", url=current)
                    last_heartbeat = time.time()
                page.wait_for_timeout(1000)

            write_status(status_path, "timeout", "验证超时，可重新打开浏览器继续验证")
            return 5
    except Exception:
        traceback.print_exc()
        write_status(status_path, "failed", "验证窗口启动失败，请查看日志后重试")
        return 6
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        lock.release()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--url", default=EXPLORE_URL)
    parser.add_argument("--timeout", default="600")
    parser.add_argument("--profile-base", default="")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--no-proxy", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
