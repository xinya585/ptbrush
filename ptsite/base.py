#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
站点适配器公共基类。

把各站通用的能力抽到这里：
- cookie 字符串 -> RequestsCookieJar，并同时塞进请求头
- 默认 UA、重试、请求间隔限速
- 错误页识别（站点出错时往往返回 JSON / HTML 登录页，而不是种子）
- HTML 文本工具

子类只需要关心「这个站怎么解析」。
"""
import re
from abc import ABC, abstractmethod
from http.cookies import SimpleCookie
from time import sleep
from typing import Any, Dict, Generator, Iterable, Optional

import requests
from loguru import logger
from requests.cookies import RequestsCookieJar

from model import Torrent
from ptsite.throttle import (
    MAX_WAIT,
    SiteRateLimited,
    acquire as acquire_slot,
    cooldown_left,
    defer as defer_site,
    parse_cooldown,
)
from config.config import site_domain_of

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\u3000]+")
_CHARSET_RE = re.compile(r"charset=([\w\-]+)", re.I)


def fix_encoding(response: requests.Response) -> requests.Response:
    """
    修正响应编码。

    站点响应头不带 charset 时 requests 会退到 ISO-8859-1，整页中文变乱码，
    "免费" / "做种数" 这类关键词一个都匹配不到。这里按字节判定真实编码：
    先严格 UTF-8，再 GB18030，都失败才用 requests 自己的判定。
    """
    try:
        raw = response.content or b""
        ctype = response.headers.get("content-type") or ""
        if "image/" in ctype or "octet-stream" in ctype:
            return response
        declared = _CHARSET_RE.search(ctype)
        if declared and declared.group(1).lower() not in ("iso-8859-1", "latin-1"):
            response.encoding = declared.group(1)
            return response
        for enc in ("utf-8", "gb18030"):
            try:
                raw.decode(enc)
                response.encoding = enc
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            response.encoding = response.apparent_encoding or "utf-8"
    except Exception:  # noqa: BLE001
        pass
    return response


def build_cookie_jar(cookie: str) -> RequestsCookieJar:
    """把浏览器里复制出来的整条 Cookie 字符串转成 cookie jar。"""
    jar = RequestsCookieJar()
    if not cookie:
        return jar
    cookie = cookie.strip().strip(";")
    try:
        parsed = SimpleCookie()
        parsed.load(cookie)
        for key, morsel in parsed.items():
            jar.set(key, morsel.value)
        if jar:
            return jar
    except Exception:  # noqa: BLE001
        pass
    # 退化路径：手工切分，容忍畸形字段
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if key:
            jar.set(key, value)
    return jar


def strip_tags(html: str) -> str:
    return _SPACE_RE.sub(" ", _TAG_RE.sub(" ", html or "")).strip()


def to_int(text: str, default: int = 0) -> int:
    if text is None:
        return default
    digits = re.sub(r"[^\d]", "", str(text))
    return int(digits) if digits else default


def human_size(num_bytes) -> str:
    """把字节数写成 1.2 GB 这种给人看的样式。"""
    try:
        value = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


class BaseSiteSpider(ABC):
    """所有站点适配器的基类。"""

    #: 适配器类型标识，对应配置里的 sites.type
    TYPE = ""

    def __init__(
        self,
        cookie: str = "",
        headers: Optional[Iterable[Any]] = None,
        site_name: str = "",
        options: Optional[Dict[str, Any]] = None,
    ):
        self.site_name = site_name or self.TYPE
        self.options = dict(options or {})
        self.cookie = cookie if isinstance(cookie, str) else ""
        self.cookie_jar = build_cookie_jar(self.cookie)

        self.headers = {
            "accept": "*/*",
            "user-agent": self.options.get("user_agent") or DEFAULT_UA,
        }
        for h in headers or []:
            key = getattr(h, "key", None) or (h.get("key") if isinstance(h, dict) else None)
            if not key:
                continue
            value = getattr(h, "value", None)
            if value is None:
                value = h.get("value", "") if isinstance(h, dict) else ""
            self.headers[key] = value

        # UA 留空（配置里没填、或 headers 里给了空值）会让中间设备把请求当成异常流量：
        # 馒头站实测 UA 为空时 api.m-team.cc 会被导到 Google 首页（server: gws），UA 有值即正常
        if not str(self.headers.get("user-agent") or "").strip():
            self.headers["user-agent"] = DEFAULT_UA

        self.timeout = float(self.options.get("timeout", 30))
        self.request_interval = float(self.options.get("request_interval", 3))
        self.retries = int(self.options.get("retries", 3))
        self.verify = bool(self.options.get("verify", True))
        # 选种策略：允许的促销类型 / 免费剩余小时估算 / 免费剩余不足多久不下载
        self.promotions = [
            str(p).strip().lower()
            for p in (self.options.get("promotions") or ["free", "free_2x"])
        ]
        self.default_free_hours = float(self.options.get("default_free_hours", 24))
        self.min_free_hours = float(self.options.get("min_free_hours", 3))
        self._last_request_at = 0.0
        self._session = requests.Session()

        # 站点域名键：流控隔离、账号快照、图标缓存都用它，站点改中文名不影响
        self.domain_key = (
            str(self.options.get("domain") or "")
            or site_domain_of(self.options.get("base_url") or self.options.get("rss_url") or "")
            or self.site_name
        )
        # 流控三元组。limit_seconds 没配时退回老的 request_interval，保持老配置行为不变
        self.limit_seconds = float(self.options.get("limit_seconds") or self.request_interval or 0)
        self.limit_interval = float(self.options.get("limit_interval") or 0)
        self.limit_count = int(self.options.get("limit_count") or 0)
        # 站点排除关键词：命中即跳过（逗号 / 换行分隔）
        self.exclude_keywords = [
            kw.strip().lower()
            for kw in re.split(r"[,，\n]", str(self.options.get("filter") or ""))
            if kw.strip()
        ]
        # 代理开关：关掉时强制直连，不读容器的 HTTP_PROXY / HTTPS_PROXY
        self.use_proxy = bool(self.options.get("proxy", False))
        self._session.trust_env = self.use_proxy

    # ---------------- 请求层 ---------------------------

    def _throttle(self):
        """
        申请一次请求许可。

        走站点级三元组限流（limit_seconds / limit_interval + limit_count），
        命中远端 429 后还会有一段时间的整站冷却；冷却太长时直接放弃本轮，不把线程挂死。
        """
        if not (self.limit_seconds or self.limit_interval or self.limit_count):
            left = cooldown_left(self.domain_key)
            if left <= 0:
                return
            if left > MAX_WAIT:
                raise SiteRateLimited(
                    f"站点 {self.site_name} 正在限流冷却中，剩余 {int(left)} 秒，稍后再试"
                )
            sleep(left)
            return

        while True:
            delay = acquire_slot(
                self.domain_key, self.limit_seconds, self.limit_interval, self.limit_count
            )
            if delay <= 0:
                return
            if delay > MAX_WAIT:
                raise SiteRateLimited(
                    f"站点 {self.site_name} 请求过于频繁，需要再等 {int(delay)} 秒"
                )
            # 多等 30ms，避免刚好卡在边界上又被判一次超限
            sleep(delay + 0.03)

    def fetch(self, url: str, method: str = "GET", data: Any = None, *args, **kwargs) -> requests.Response:
        """带重试和限速的请求。签名保持与老版本兼容。"""
        kwargs.setdefault("timeout", (self.timeout, self.timeout))
        kwargs.setdefault("verify", self.verify)
        cookies = self.cookie_jar if self.cookie_jar else None
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                import time as _time

                self._last_request_at = _time.time()
                response = self._session.request(
                    method, url, headers=self.headers, cookies=cookies, data=data, *args, **kwargs
                )
                # 远端直接限流：按它给的冷却时间把整个站点推开，后面的请求不再硬撞
                if response.status_code == 429:
                    seconds = parse_cooldown(response.text)
                    defer_site(self.domain_key, seconds, f"HTTP 429 @ {url}")
                    logger.warning(
                        f"[{self.site_name}] 触发站点限流（429），整站冷却 {int(seconds)} 秒后再请求"
                    )
                    if attempt >= self.retries:
                        return fix_encoding(response)
                    sleep(min(seconds, MAX_WAIT))
                    continue
                if response.status_code in (403, 429, 502, 503, 504) and attempt < self.retries:
                    logger.warning(f"[{self.site_name}] {url} 返回 {response.status_code}，第{attempt}次重试")
                    sleep(1.5 * attempt)
                    continue
                return fix_encoding(response)
            except SiteRateLimited:
                raise
            except Exception as e:  # noqa: BLE001
                last_error = e
                logger.warning(f"[{self.site_name}] 请求失败 {url}（第{attempt}次）：{e}")
                sleep(1.5 * attempt)
        raise Exception(f"[{self.site_name}] 请求失败：{url}（{last_error}）")

    def close(self):
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 通用工具 ---------------------------



    @staticmethod
    def _looks_like_torrent(content: Optional[bytes]) -> bool:
        if not content:
            return False
        head = content[:200]
        return b"announce" in head or head.startswith(b"d8:")

    @abstractmethod
    def free_torrents(self) -> Generator[Torrent, Torrent, Torrent]:
        raise NotImplementedError

    def search(self, keyword: str, limit: int = 60):
        """按关键字搜站内种子（「搜片 → 跨站搜资源」用）。

        默认实现返回空：RSS 这类没有站内搜索能力的适配器直接跳过，
        上层只用关心「这个站有没有搜到」，不用判断适配器类型。
        """
        return []

    @abstractmethod
    def parse_torrent_link(self, torrent_id: str) -> str:
        raise NotImplementedError

    def download_torrent_content(self, torrent_link: str) -> Optional[bytes]:
        """默认实现：下载并校验是不是真的种子。"""
        try:
            resp = self.fetch(torrent_link)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.site_name}] 下载种子失败：{e}")
            return None
        content = resp.content or b""
        if not self._looks_like_torrent(content):
            logger.error(f"[{self.site_name}] 下载链接返回的不是种子，可能是错误页：{strip_tags(resp.text)[:120]}")
            return None
        return content
