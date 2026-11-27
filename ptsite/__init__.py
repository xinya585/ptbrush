#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
站点适配器入口。

对外只暴露两件事：
- BaseSiteSpider：所有适配器的基类（实现在 ptsite.base）
- TorrentFetch：按配置里的 site.type 选出对应适配器

选择适配器的优先级：
1. 配置里写了 type（支持 m-team / mt / nexusphp / nexus / rss 等别名）
2. 没写 type，但站点名叫 M-Team（老配置零改动继续可用）
3. 填了 base_url -> nexusphp
4. options 里填了 rss_url -> rss
5. 都没有 -> 报错并列出可用类型
"""
from typing import Any, Dict, Generator, List, Optional

from ptsite.base import BaseSiteSpider, build_cookie_jar, strip_tags, to_int  # noqa: F401

TYPE_ALIASES = {
    "mteam": "mteam",
    "m-team": "mteam",
    "m_team": "mteam",
    "mt": "mteam",
    "nexusphp": "nexusphp",
    "nexus": "nexusphp",
    "nexus_php": "nexusphp",
    "php": "nexusphp",
    "rss": "rss",
    "rssfeed": "rss",
    "feed": "rss",
}

# 给 Web 界面用的类型元数据
SITE_TYPE_META = [
    {
        "value": "mteam",
        "label": "M-Team",
        "description": "使用 x-api-key 请求头鉴权",
        "needs_cookie": False,
        "needs_base_url": False,
        "needs_rss_url": False,
        "default_name": "M-Team",
        "default_headers": [{"key": "x-api-key", "value": ""}],
    },
    {
        "value": "nexusphp",
        "label": "NexusPHP",
        "description": "填站点地址 + 浏览器 Cookie，适配绝大多数国内 PT 站",
        "needs_cookie": True,
        "needs_base_url": True,
        "needs_rss_url": False,
        "default_name": "新站点",
        "default_headers": [],
    },
]


def normalize_type(type_hint: str) -> str:
    hint = (type_hint or "").strip().lower().replace(" ", "")
    return TYPE_ALIASES.get(hint, "")


def site_type_options() -> List[Dict[str, Any]]:
    return [dict(item) for item in SITE_TYPE_META]


class TorrentFetch:
    # 放在类体里 import：此时 ptsite.base 已加载完，不会循环导入
    from ptsite.mteam import MTeamSpider
    from ptsite.nexusphp import NexusPhpSpider
    from ptsite.rss import RssSpider

    # 老接口：按站点名找适配器（M-Team 老配置继续可用）
    SITE_SPIDER_MAP = {"M-Team": MTeamSpider}
    # 新接口：按类型找适配器
    ADAPTERS = {
        "mteam": MTeamSpider,
        "nexusphp": NexusPhpSpider,
        "rss": RssSpider,
    }

    def __init__(
        self,
        site: Any = None,
        cookie: str = "",
        headers=None,
        promotions: Optional[List[str]] = None,
        exclude_hr: Optional[bool] = None,
        min_free_hours: Optional[float] = None,
    ):
        """
        两种用法：
        - TorrentFetch(site_model, promotions=[...], exclude_hr=True)：site_model 是 config.config.SiteModel（推荐），
          promotions / exclude_hr / min_free_hours 是全局选种策略；站点 options 里写了就优先用站点的
        - TorrentFetch("M-Team", cookie=..., headers=...)：兼容老写法
        """
        if isinstance(site, str):
            name = site
            type_hint = ""
            base_url = ""
            options: Dict[str, Any] = {}
        elif site is not None:
            name = getattr(site, "name", "") or ""
            type_hint = getattr(site, "type", "") or ""
            base_url = getattr(site, "base_url", "") or ""
            options = dict(getattr(site, "options", None) or {})
            if getattr(site, "cookie", None):
                cookie = site.cookie
            if getattr(site, "headers", None):
                headers = site.headers
        else:
            name, type_hint, base_url, options = "", "", "", {}

        # 全局选种策略注入 options，站点自己配了就以站点为准
        if promotions is not None:
            options.setdefault("promotions", list(promotions))
        if exclude_hr is not None:
            options.setdefault("exclude_hr", bool(exclude_hr))
        if min_free_hours is not None:
            options.setdefault("min_free_hours", float(min_free_hours))

        resolved = self.resolve_type(name, type_hint, base_url, options)
        self.site_name = name or resolved
        self.site_type = resolved
        spider_class = self.ADAPTERS[resolved]

        if base_url and not options.get("base_url"):
            options["base_url"] = base_url

        # 把站点级配置注入适配器：域名键、流控三元组、代理开关、排除关键词、备用令牌
        if site is not None and not isinstance(site, str):
            options.setdefault("domain", getattr(site, "domain", "") or "")
            options.setdefault("proxy", bool(getattr(site, "proxy", False)))
            options.setdefault("filter", getattr(site, "filter", "") or "")
            options.setdefault("limit_seconds", int(getattr(site, "limit_seconds", 0) or 0))
            options.setdefault("limit_interval", int(getattr(site, "limit_interval", 0) or 0))
            options.setdefault("limit_count", int(getattr(site, "limit_count", 0) or 0))
            token = getattr(site, "token", "") or ""
            if token and not options.get("token"):
                options["token"] = token
            # api_key 在站点里是独立字段，得转进 options 让适配器拼成鉴权头，
            # 否则走 API 的站点（如馒头）每次请求都是 401
            api_key = getattr(site, "api_key", "") or ""
            if api_key and not options.get("api_key"):
                options["api_key"] = api_key

        self._spider: BaseSiteSpider = spider_class(
            cookie=cookie or "",
            headers=headers or [],
            site_name=self.site_name,
            options=options,
        )

    @classmethod
    def resolve_type(cls, name: str, type_hint: str, base_url: str, options: Optional[Dict[str, Any]]) -> str:
        options = options or {}
        normalized = normalize_type(type_hint)
        if normalized:
            return normalized
        if name in cls.SITE_SPIDER_MAP:
            return "mteam"
        # 填了 API Key 的站点基本就是 M-Team 这类走 API 的站：
        # 只看 base_url 会把 zp.m-team.io 判成 NexusPHP，取链必然失败
        if str(options.get("api_key") or "").strip():
            return "mteam"
        if base_url:
            return "nexusphp"
        if options.get("rss_url") or options.get("rss_urls"):
            return "rss"
        raise ValueError(
            f"无法判断站点 {name!r} 的解析器类型，请在配置里补 type；"
            f"可用类型：{', '.join(cls.ADAPTERS.keys())}"
        )

    @property
    def free_torrents(self) -> Generator[Any, Any, Any]:
        """
        逐条产出种子。

        站点级排除关键词在这里统一过一道：命中就不往下走，
        这样所有适配器都不用各自实现一遍。
        """
        keywords = getattr(self._spider, "exclude_keywords", []) or []
        skipped = 0
        for torrent in self._spider.free_torrents():
            if keywords:
                title = str(getattr(torrent, "name", "") or "").lower()
                if any(kw in title for kw in keywords):
                    skipped += 1
                    continue
            yield torrent
        if skipped:
            from loguru import logger

            logger.info(f"站点 {self.site_name} 按排除关键词跳过 {skipped} 个种子")

    def parse_torrent_link(self, torrent_id) -> str:
        return self._spider.parse_torrent_link(torrent_id)

    def search(self, keyword: str, limit: int = 60):
        """站内按关键字搜索（搜片页跨站搜资源用）。"""
        searcher = getattr(self._spider, "search", None)
        if not searcher:
            return []
        return searcher(keyword, limit=limit)

    def download_torrent_content(self, torrent_link: str) -> Optional[bytes]:
        return self._spider.download_torrent_content(torrent_link)

    def close(self):
        self._spider.close()
