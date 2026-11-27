#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
RSS 适配器。

站点提供带 passkey 的 RSS 时最省事：不用 cookie、不用解析 HTML。
缺点是 RSS 里通常没有做种/下载人数，只能给个兜底值（assume_seeders / assume_leechers），
所以评分会偏低，选种优先级不如页面抓取。
"""
import re
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Generator, List, Optional
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

from loguru import logger

from model import Torrent
from ptsite.base import BaseSiteSpider, strip_tags

ID_RE = re.compile(r"(?:id|torrent_id|tid)=(\d+)", re.I)
SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB|B)$", re.I)
SIZE_UNITS = {
    "B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4,
    "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4,
}


class RssSpider(BaseSiteSpider):
    TYPE = "rss"

    def __init__(self, cookie: str = "", headers=None, site_name: str = "", options: Optional[Dict[str, Any]] = None):
        super().__init__(cookie, headers, site_name, options)
        opts = self.options
        single = opts.get("rss_url") or ""
        multi = opts.get("rss_urls") or []
        self.rss_urls: List[str] = [u for u in ([single] if single else []) + list(multi) if u]
        if not self.rss_urls:
            raise ValueError(f"[{self.site_name}] rss 适配器需要在 options 里填 rss_url")
        self.default_free_hours = float(opts.get("default_free_hours", 24))
        self.assume_seeders = int(opts.get("assume_seeders", 1))
        self.assume_leechers = int(opts.get("assume_leechers", 1))
        self.keep_all = bool(opts.get("keep_all", False))
        if self.keep_all:
            # 老参数 keep_all=True 等价于「不挑促销类型」
            self.promotions = ["all"]
        # 注意：别把 "2X" 放进 free_texts —— 它属于 2X 促销而不是免费，否则 "标题带2X" 会被误判成免费
        self.free_texts = list(opts.get("free_texts") or ["免费", "限时", "Free", "FREE"])
        self.download_template = opts.get("download_template") or ""
        self.passkey = opts.get("passkey") or self._guess_passkey()
        self._link_cache: Dict[str, str] = {}

    # ---------------- 工具 ---------------------------

    def _guess_passkey(self) -> str:
        for url in getattr(self, "rss_urls", []) or []:
            qs = parse_qs(urlparse(url).query)
            for key in ("passkey", "pass_key", "key"):
                if qs.get(key):
                    return qs[key][0]
        return ""

    def _resolve_download_link(self, torrent_id: str, link: str) -> str:
        if self.download_template:
            base = urlparse(self.rss_urls[0])
            return self.download_template.format(
                base_url=f"{base.scheme}://{base.netloc}",
                id=torrent_id,
                passkey=self.passkey,
            )
        if self.passkey:
            base = urlparse(self.rss_urls[0])
            return f"{base.scheme}://{base.netloc}/download.php?id={torrent_id}&passkey={self.passkey}"
        # 兜底：用条目里的 details 链接（部分站允许直接下载）
        return link

    @staticmethod
    def _text(node, tag: str) -> str:
        for child in node:
            if child.tag.split("}")[-1].lower() == tag:
                return (child.text or "").strip()
        return ""

    def _parse_size(self, item) -> int:
        for child in item:
            tag = child.tag.split("}")[-1].lower()
            if tag == "enclosure":
                length = child.attrib.get("length")
                if length and length.isdigit() and int(length) > 0:
                    return int(length)
            if tag in ("size", "contentlength", "length"):
                length = (child.text or "").strip()
                if length.isdigit():
                    return int(length)
                m = SIZE_RE.match(length)
                if m:
                    return int(float(m.group(1)) * SIZE_UNITS[m.group(2).upper()])
        return 0

    # ---------------- 抓取 ---------------------------

    def free_torrents(self) -> Generator[Torrent, Torrent, Torrent]:
        for url in self.rss_urls:
            logger.info(f"[{self.site_name}] 拉取 RSS：{url}")
            try:
                resp = self.fetch(url)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[{self.site_name}] RSS 请求失败：{e}")
                continue
            try:
                root = ET.fromstring(resp.content)
            except ET.ParseError as e:
                logger.error(f"[{self.site_name}] RSS 解析失败：{e}")
                continue

            items = [node for node in root.iter() if node.tag.split("}")[-1].lower() == "item"]
            logger.info(f"[{self.site_name}] RSS 共 {len(items)} 条")
            for item in items:
                try:
                    torrent = self._parse_item(item)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[{self.site_name}] RSS 条目解析失败：{e}")
                    continue
                if torrent:
                    yield torrent

    def _parse_item(self, item) -> Optional[Torrent]:
        title = strip_tags(self._text(item, "title"))
        link = self._text(item, "link")
        enclosure = ""
        for child in item:
            if child.tag.split("}")[-1].lower() == "enclosure":
                enclosure = child.attrib.get("url", "")
                break
        target = enclosure or link
        m = ID_RE.search(target or "") or ID_RE.search(link or "")
        if not m:
            return None
        torrent_id = int(m.group(1))
        if enclosure:
            self._link_cache[str(torrent_id)] = enclosure

        if not title:
            return None
        promotion = self._promotion_of(title)
        if not self._promotion_allowed(promotion):
            return None

        size = self._parse_size(item)
        if size <= 0:
            return None

        created = self._parse_time(item)
        if promotion in ("free", "free_2x"):
            free_end = datetime.now() + timedelta(hours=self.default_free_hours)
        else:
            free_end = datetime.now() + timedelta(days=7)

        return Torrent(
            id=torrent_id,
            name=title,
            size=size,
            seeders=self.assume_seeders,
            leechers=self.assume_leechers,
            created_time=created,
            free_end_time=free_end,
            site=self.site_name,
            promotion=promotion,
            hit_and_run=self._is_hit_and_run(title),
        )

    def _promotion_of(self, title: str) -> str:
        lowered = title.lower()
        if any(t in lowered for t in ("2x免费", "2x free", "free 2x", "2xfree", "双倍免费", "免费2x")):
            return "free_2x"
        for token in self.free_texts:
            if token and token.lower() in lowered:
                return "free_2x" if "2x" in lowered else "free"
        if "50%" in title or "半价" in title:
            return "half_down"
        if "2x" in lowered or "双倍" in title:
            return "2x"
        return "none"

    def _promotion_allowed(self, promotion: str) -> bool:
        if "all" in self.promotions:
            return True
        if promotion == "none":
            return False
        return promotion in self.promotions

    @staticmethod
    def _is_hit_and_run(title: str) -> bool:
        return bool(re.search(r"hitandrun|hit_and_run|h\s*&\s*r|hit\s*and\s*run", title, re.I))



    @staticmethod
    def _parse_time(item) -> datetime:
        raw = RssSpider._text(item, "pubdate") or RssSpider._text(item, "date")
        if raw:
            try:
                return parsedate_to_datetime(raw).replace(tzinfo=None)
            except Exception:  # noqa: BLE001
                try:
                    return datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
        return datetime.now()

    # ---------------- 下载 ---------------------------

    def parse_torrent_link(self, torrent_id: str) -> str:
        cached = self._link_cache.get(str(torrent_id))
        if cached:
            return cached
        return self._resolve_download_link(str(torrent_id), "")
