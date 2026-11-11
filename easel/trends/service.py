"""Shared caching, single-flight collection and public status for every transport."""
from __future__ import annotations

import threading
import time
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from easel.timeouts import TIMEOUT_TREND_BUSY_WAIT, TIMEOUT_TREND_BUSY_RETRY, TIMEOUT_TREND_FAILURE_RETRY

from .protocol import MAX_BYTES, CollectorError, timestamp, validate_payload
from .runner import collect
from .sources import Source, display_label

ERROR_MESSAGES = {
    "timeout": "采集超时，请检查数据源服务或脚本",
    "start_failed": "无法启动采集脚本，请检查脚本和 Python 解释器路径",
    "process_failed": "采集脚本执行失败，请在本地运行脚本检查日志",
    "invalid_response": "采集结果不符合热点数据协议 v1",
    "invalid_feed": "无法解析 RSS/Atom 订阅，请检查订阅地址和格式",
    "output_too_large": "采集输出超过 2 MiB 限制",
    "http_failed": "HTTP 数据源请求失败，请检查地址、鉴权和网络",
    "missing_environment": "数据源引用的环境变量未设置",
    "expired_data": "数据源返回的数据已超过最大保留时间",
    "collector_failed": "数据源采集失败，请检查采集配置",
    "resource_limit": "采集器超过资源限制或异常退出，其余来源不受影响",
    "isolation_failed": "无法建立采集隔离，已停止执行，请检查本地运行环境",
    "busy": "采集器正在工作，请稍后刷新",
    "invalid_script": "Python 脚本语法校验失败，请检查代码",
    "rate_limited": "新搜索冷却中，请稍后再试",
    "switch_limited": "范围切换过快，请稍后再试",
}

_SCOPE_KEY_PREFIX = "scope:"


@dataclass
class _Entry:
    lock: threading.Lock = field(default_factory=threading.Lock)
    payload: dict | None = None
    attempted_at: float = 0
    error: str | None = None
    size: int = 0


def failure_group(source: Source, code: str = "collector_failed") -> dict:
    return {"platform": source.id, "label": display_label(source.label), "items": [], "ok": False,
            "status": "error", "stale": False, "fetched_at": None, "last_attempt_at": 0,
            "error": {"code": code, "message": ERROR_MESSAGES.get(code, ERROR_MESSAGES["collector_failed"])}}


class TrendService:
    def __init__(self, rate_state_path: Path | None = None):
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(4)
        self._rate_lock = threading.Lock()
        self._rate_state_path = rate_state_path
        self._last_attempts: dict[str, float] = {}
        self._load_rate_state()

    def _load_rate_state(self) -> None:
        if self._rate_state_path is None:
            return
        try:
            data = json.loads(self._rate_state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._last_attempts = {
                    key: float(value) for key, value in data.items()
                    if isinstance(key, str) and isinstance(value, (int, float))
                    and not isinstance(value, bool) and value >= 0
                }
        except (OSError, ValueError, TypeError):
            self._last_attempts = {}

    def _save_rate_state(self) -> None:
        if self._rate_state_path is None:
            return
        try:
            self._rate_state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._rate_state_path.with_suffix(self._rate_state_path.suffix + ".tmp")
            temporary.write_text(json.dumps(self._last_attempts, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self._rate_state_path)
        except OSError:
            pass

    def _reserve_attempt(
        self,
        source_id: str,
        cooldown: float,
        cooldown_key: str,
        min_interval: float,
    ) -> tuple[bool, str, float]:
        now = time.time()
        scope_key = f"{source_id}:{_SCOPE_KEY_PREFIX}{cooldown_key}" if cooldown_key else ""
        with self._rate_lock:
            if scope_key and cooldown:
                last_scope = self._last_attempts.get(scope_key, 0)
                if last_scope and now - last_scope < cooldown:
                    return False, "rate_limited", max(0.0, cooldown - (now - last_scope))
            last_global = self._last_attempts.get(source_id, 0)
            if min_interval and last_global and now - last_global < min_interval:
                return False, "switch_limited", max(0.0, min_interval - (now - last_global))
            self._last_attempts[source_id] = now
            if scope_key:
                self._last_attempts[scope_key] = now
            self._save_rate_state()
            return True, "", 0.0

    def reset(self, source_id: str) -> None:
        with self._lock:
            self._entries.clear()
        with self._rate_lock:
            prefix = f"{source_id}:{_SCOPE_KEY_PREFIX}"
            for key in list(self._last_attempts):
                if key == source_id or key.startswith(prefix):
                    self._last_attempts.pop(key, None)
            self._save_rate_state()

    def get(self, source: Source, environment: dict[str, str], limit: int = 12,
            cooldown_seconds: float = 0, cooldown_key: str = "",
            min_interval_seconds: float = 0) -> dict:
        try:
            return self._get(source, environment, max(1, min(int(limit), 30)),
                             max(0, min(float(cooldown_seconds), 3600)),
                             cooldown_key,
                             max(0, min(float(min_interval_seconds), 3600)))
        except Exception:
            return failure_group(source)

    def _get(self, source: Source, environment: dict[str, str], limit: int,
             cooldown_seconds: float, cooldown_key: str,
             min_interval_seconds: float) -> dict:
        key = source.cache_key(environment)
        with self._lock:
            entry = self._entries.setdefault(key, _Entry())
            self._entries.move_to_end(key)
            # Bound memory when a local config or environment is edited repeatedly.
            if len(self._entries) > 96:
                self._entries.popitem(last=False)
        if not entry.lock.acquire(timeout=TIMEOUT_TREND_BUSY_WAIT):
            return self._render(source, entry, limit, "busy")
        try:
            now = time.time()
            # Failures are retried after at most 30s, rather than on every page request.
            retry = TIMEOUT_TREND_BUSY_RETRY if entry.error == "busy" else min(source.refresh, TIMEOUT_TREND_FAILURE_RETRY) if entry.error else source.refresh
            if not entry.attempted_at or now - entry.attempted_at >= retry:
                try:
                    if not self._slots.acquire(timeout=TIMEOUT_TREND_BUSY_WAIT):
                        raise CollectorError("busy")
                    try:
                        allowed, reason, retry_after = self._reserve_attempt(
                            source.id, cooldown_seconds, cooldown_key,
                            min_interval_seconds)
                        if not allowed:
                            return self._render(
                                source, entry, limit, reason, retry_after)
                        payload = validate_payload(collect(source, environment))
                    finally:
                        self._slots.release()
                    now = time.time()
                    fetched = timestamp(payload["fetched_at"])
                    if fetched > now + 300:
                        raise CollectorError("invalid_response")
                    if now - fetched > source.max_stale:
                        raise CollectorError("expired_data")
                    size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                    if size > MAX_BYTES:
                        raise CollectorError("output_too_large")
                    entry.payload, entry.error, entry.size = payload, None, size
                    with self._lock:
                        total = sum(item.size for item in self._entries.values())
                        for old_key, old in list(self._entries.items()):
                            if total <= 16 * 1024 * 1024:
                                break
                            if old is not entry and not old.lock.locked():
                                total -= old.size
                                self._entries.pop(old_key, None)
                except CollectorError as error:
                    entry.error = error.code
                except Exception:
                    # Never return exception strings containing headers, tokens or stderr.
                    entry.error = "collector_failed"
                entry.attempted_at = time.time()
            return self._render(source, entry, limit)
        finally:
            entry.lock.release()

    def _render(self, source: Source, entry: _Entry, limit: int,
                override: str | None = None, retry_after: float = 0) -> dict:
        now = time.time()
        payload = entry.payload
        fetched = timestamp(payload["fetched_at"]) if payload else None
        usable = payload is not None and now - fetched <= source.max_stale
        stale = usable and (override is not None or entry.error is not None or now - fetched >= source.refresh)
        error = override or entry.error or ("expired_data" if payload and not usable else None)
        error_payload = None
        if error:
            error_payload = {
                "code": error,
                "message": ERROR_MESSAGES.get(error, ERROR_MESSAGES["collector_failed"]),
            }
            if retry_after > 0:
                error_payload["retry_after"] = round(retry_after, 1)
        return {"platform": source.id, "label": display_label(source.label), "items": payload["items"][:limit] if usable else [],
                "ok": usable, "status": "stale" if stale else "fresh" if usable else "error",
                "stale": bool(stale), "fetched_at": payload["fetched_at"] if payload else None,
                "last_attempt_at": int(entry.attempted_at),
                "error": error_payload}
