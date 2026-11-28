#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
站点级请求流控（对齐 MoviePilot 的站点限流机制）。

三个参数，任一为 0 表示该项不限制：

- limit_seconds   相邻两次请求的最小间隔（秒）
- limit_interval  统计窗口长度（秒）
- limit_count     窗口内允许的最大请求数

三类请求共用同一份额度：抓种子列表、下载种子文件、刷新站点账号数据。
额度按 domain（域名键）隔离，一个站被限流不影响其它站。

额外一层：远端真的回 429 / "访问过于频繁" 时，把整个站点 defer 一段时间
（冷却秒数优先从站点返回的文案里解析，解析不到用默认值），期间所有请求直接等待或放弃，
避免继续撞墙把账号撞进小黑屋。
"""
import re
import threading
import time
from typing import Dict, Tuple

_LOCK = threading.Lock()

# key -> {"last": 上次放行时间, "hits": [窗口内放行时间], "defer_until": 冷却截止时间, "reason": 冷却原因}
_STATE: Dict[str, dict] = {}

_COOLDOWN_PATTERNS = (
    (re.compile(r"(\d{1,5})\s*(?:秒|s)\s*(?:后|之后)"), 1),
    (re.compile(r"(\d{1,5})\s*(?:分钟|min)\s*(?:后|之后)"), 60),
    (re.compile(r"(\d{1,3})\s*(?:小时|h)\s*(?:后|之后)"), 3600),
    (re.compile(r"(\d{1,5})\s*seconds?\b", re.I), 1),
    (re.compile(r"(\d{1,5})\s*minutes?\b", re.I), 60),
)

DEFAULT_COOLDOWN = 300.0
# 冷却超过这个秒数就不再原地等，直接把这一轮放弃掉
MAX_WAIT = 120.0


class SiteRateLimited(Exception):
    """站点处于冷却期，且剩余时间太长，本轮放弃。"""


def _state(key: str) -> dict:
    state = _STATE.get(key)
    if state is None:
        state = {"last": 0.0, "hits": [], "defer_until": 0.0, "reason": ""}
        _STATE[key] = state
    return state


def parse_cooldown(text: str, default: float = DEFAULT_COOLDOWN) -> float:
    """从站点的限流文案里解析冷却秒数。"""
    body = (text or "")[:2000]
    if not body:
        return default
    for pattern, scale in _COOLDOWN_PATTERNS:
        match = pattern.search(body)
        if match:
            try:
                return max(1.0, float(match.group(1)) * scale)
            except (TypeError, ValueError):
                continue
    return default


def acquire(key: str, limit_seconds: float = 0, limit_interval: float = 0, limit_count: int = 0) -> float:
    """
    申请一次请求许可。

    返回 0 表示已放行（内部已记一次请求）；
    返回正数表示还需要等待的秒数，调用方 sleep 之后要重新申请（因为等待期间别人可能又占用了额度）。
    """
    key = key or "default"
    now = time.time()
    with _LOCK:
        state = _state(key)

        # 1. 整站冷却优先
        if state["defer_until"] > now:
            return state["defer_until"] - now

        delay = 0.0

        # 2. 相邻请求最小间隔
        if limit_seconds > 0 and state["last"]:
            gap = now - state["last"]
            if gap < limit_seconds:
                delay = max(delay, limit_seconds - gap)

        # 3. 窗口内最大次数
        if limit_interval > 0 and limit_count > 0:
            hits = [t for t in state["hits"] if now - t < limit_interval]
            state["hits"] = hits
            if len(hits) >= int(limit_count):
                # 最早那次释放出来之后才允许下一次
                release_at = hits[0] + limit_interval
                delay = max(delay, release_at - now)

        if delay > 0:
            return delay

        # 放行，记账
        state["last"] = now
        if limit_interval > 0 and limit_count > 0:
            state["hits"].append(now)
        return 0.0


def defer(key: str, seconds: float, reason: str = "") -> float:
    """把整个站点推迟一段时间（命中远端限流时调用），返回本次设置的截止时间戳。"""
    key = key or "default"
    with _LOCK:
        state = _state(key)
        until = time.time() + max(1.0, float(seconds))
        # 已有更晚的冷却就不缩短
        if until > state["defer_until"]:
            state["defer_until"] = until
            state["reason"] = (reason or "")[:200]
        return state["defer_until"]


def cooldown_left(key: str) -> float:
    """站点剩余冷却秒数，0 表示没在冷却。"""
    with _LOCK:
        state = _state(key)
        left = state["defer_until"] - time.time()
        return left if left > 0 else 0.0


def reset(key: str = "") -> None:
    """清空一个站点（或全部）的流控状态，配置改动后调用。"""
    with _LOCK:
        if key:
            _STATE.pop(key, None)
        else:
            _STATE.clear()


def snapshot() -> Dict[str, Tuple[float, float]]:
    """给 Web 端看的流控快照：key -> (剩余冷却秒数, 最近一次放行距今秒数)。"""
    now = time.time()
    with _LOCK:
        return {
            key: (
                max(0.0, state["defer_until"] - now),
                (now - state["last"]) if state["last"] else -1.0,
            )
            for key, state in _STATE.items()
        }
