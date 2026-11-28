#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
NexusPHP 通用适配器。

国内绝大多数 PT 站（含各种改版、换皮站）的种子列表都是 NexusPHP 结构：
一行一个种子，标题链到 details.php?id=xxx，下载走 download.php?id=xxx。

解析策略（不按固定列号取值，避免 rowspan 折叠分类列导致错位）：
1. 以 <tr> 切块，块内出现 details.php?id= 才算种子行
2. 找到「文本形如 1.23 GB」的单元格下标 i，则 seeders=cells[i+1]、leechers=cells[i+2]
   （NexusPHP 列序固定为 大小 -> 种子 -> 下载 -> 完成）
3. 促销识别三路并行：属性标记 / 正文中文标记 / 标题里的【免费】
4. 免费结束时间：优先解析「剩余时间 3天5小时」，解析不到按 default_free_hours 估算
"""
import html
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Generator, List, Optional
from urllib.parse import urlencode, urljoin

from loguru import logger

from model import Torrent
from ptsite.base import BaseSiteSpider, human_size, strip_tags, to_int

TR_OPEN_RE = re.compile(r"<tr\b[^>]*>", re.I)
# 必须排除 userdetails.php?id=xxx（页头"欢迎回来"里的用户链接），它的尾巴
# 恰好含有 details.php?id=... 这段子串，会把整块页头误判成种子行
DETAIL_RE = re.compile(r"(?<![A-Za-z])(?:plugin_)?details\.php\?id=(\d+)", re.I)
# 少数 NexusPHP 改版站不挂 details.php，改挂 download.php?id= 或
# torrents.php?action=download&id=（海豚/DIC Music 这类音乐站）。
# 取种子 id 时按「详情 → 下载链接」的顺序找，保证拿到的是种子 id 而不是分组 id。
ALT_ID_RES = [
    re.compile(r"(?<![A-Za-z])download\.php\?id=(\d+)", re.I),
    re.compile(r"action=download(?:&amp;|&)id=(\d+)", re.I),
    re.compile(r"(?<![A-Za-z])torrents\.php\?id=(\d+)", re.I),
]
# 「种子名」链接：details.php?id=NNN 的 <a> 里带 <b> 且文本够长。
# 做种数/下载数/评论数也是 details 链接（<b>414</b>），靠长度与纯数字区分，
# 用来判断「下一片段是不是已经换成了另一个种子」。
NAME_LIKE_RE = re.compile(
    r"""<a\b[^>]*(?<![A-Za-z])(?:plugin_)?details\.php\?id=\d+[^>]*>\s*<b>\s*([^<]{6,})""", re.I
)
# 任意「种子锚点」：用来在没有 <tr> 的 div 版式站里切行
ANY_ID_RE = re.compile(
    r"(?<![A-Za-z])(?:plugin_)?details\.php\?id=\d+"
    r"|(?<![A-Za-z])download\.php\?id=\d+"
    r"|action=download(?:&amp;|&)id=\d+",
    re.I,
)
SIZE_CELL_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB|B)$", re.I)
CELL_SPLIT_RE = re.compile(r"<td\b[^>]*>", re.I)

SIZE_UNITS = {
    "B": 1,
    "KB": 1000,
    "MB": 1000 ** 2,
    "GB": 1000 ** 3,
    "TB": 1000 ** 4,
    "KIB": 1024,
    "MIB": 1024 ** 2,
    "GIB": 1024 ** 3,
    "TIB": 1024 ** 4,
}

# 按促销强度从强到弱匹配，先命中先返回（"pro_free2up" 里也含 free，必须排前面）
PROMO_PATTERNS = [
    (re.compile(r"twoupfree|pro_free2up|free2up|2xfree|2x_free|free_2x", re.I), "free_2x"),
    (re.compile(r"halfdown|pro_50|50pctdown|half_down|half_down_free", re.I), "half_down"),
    (re.compile(r"twoup|pro_2up|doubleup|pro_2x", re.I), "2x"),
    (
        re.compile(
            r"freeleech|pro_free"
            r"|class\s*=\s*[\"'][^\"']*free[^\"']*[\"']"
            r"|alt\s*=\s*[\"'][^\"']*free[^\"']*[\"']",
            re.I,
        ),
        "free",
    ),
]
# H&R（Hit and Run）标记：多数站用 hitandrun 图标 / class，个别写在标题
HIT_AND_RUN_RE = re.compile(r"hitandrun|hit_and_run|hit-run|hit\s*and\s*run|h\s*&\s*r", re.I)
# 个别站（彩虹岛 ptchdbits）列表页不带任何 HR 标记，只在详情页写一行
# `<b>H&R: </b>3day` / `5day`；这里按「N day」判有没有 HR 考核
HR_DETAIL_RE = re.compile(r"H(?:&amp;|&)R\s*:?\s*(?:&nbsp;)?\s*</b>\s*([^<]{1,20})", re.I)
HR_DAYS_RE = re.compile(r"([\d.]+)\s*(?:day|d|天)", re.I)
# 详情页判定结果按 (站点, 种子 id) 缓存，避免每轮都去请求详情页
_HR_DETAIL_CACHE: Dict[tuple, tuple] = {}
HR_DETAIL_CACHE_TTL = 6 * 3600
TAG_ANY_RE = re.compile(r"<[^>]+>")
TITLE_ATTR_RE = re.compile(r"""title\s*=\s*["']([^"']+)["']""", re.I)
DATETIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*(\d{2}:\d{2}(?::\d{2})?)")
REMAIN_RE = re.compile(r"(?:剩余时间|剩余|time left|left)[:：]?\s*([0-9天日时分秒小时\s]+)", re.I)
REMAIN_DAY_RE = re.compile(r"(\d+)\s*[天日]")
REMAIN_HOUR_RE = re.compile(r"(\d+)\s*(?:小时|hour|h)")
REMAIN_MIN_RE = re.compile(r"(\d+)\s*(?:分钟|min|m)")

LOGIN_RE = re.compile(r"(takelogin\.php|login\.php\?returnto|你还没有登录|请先登录|please login)", re.I)

# 种子名单元格里通常两行：粗体主标题 + `<br>` 后的副标题/标签。
# 副标题里常有「中字」「特效字幕」「简繁英」这类信息，主标题是英文时只有这里能看到。
SUB_BR_RE = re.compile(r"<br\s*/?>(.*?)(?:</td>|<td\b|</tr>)", re.S | re.I)


def split_tr_chunks(text: str) -> List[str]:
    """把页面按每个 `<tr` 的出现位置切成片段（内层 `<tr` 也算切点）。

    为什么不用 `<tr>…</tr>`：改版站（HDHome / CHD 这类）把种子名塞在内嵌
    小表格里（`<td><table class="torrentname"><tr><td>`），内层 `<tr>` 会把
    外层行切断，而且这类页面常常缺 `</tr>`，配平也配不平。切片段 + 由调用方
    把「含 details 链接的片段」与其后紧邻的片段拼回来，才不依赖闭合标签。
    """
    starts = [m.start() for m in TR_OPEN_RE.finditer(text)]
    if not starts:
        return []
    ends = starts[1:] + [len(text)]
    return [text[s:e] for s, e in zip(starts, ends)]


class NexusPhpSpider(BaseSiteSpider):
    TYPE = "nexusphp"

    def __init__(self, cookie: str = "", headers=None, site_name: str = "", options: Optional[Dict[str, Any]] = None):
        super().__init__(cookie, headers, site_name, options)
        opts = self.options
        self.base_url = (opts.get("base_url") or "").rstrip("/")
        if not self.base_url:
            raise ValueError(f"[{self.site_name}] nexusphp 适配器需要 base_url")
        paths = opts.get("browse_paths") or opts.get("browse_path") or "torrents.php"
        # TTG（totheglory.im）的种子列表挂在 browse.php，torrents.php 直接 404
        if paths == "torrents.php" and "totheglory" in self.base_url:
            paths = "browse.php"
        self.browse_paths = [paths] if isinstance(paths, str) else list(paths)
        self.pages = int(opts.get("pages", 1))
        self.page_param = opts.get("page_param", "page")
        self.page_start = int(opts.get("page_start", 0))
        self.default_free_hours = float(opts.get("default_free_hours", 24))
        self.download_url_template = opts.get("download_url_template") or ""
        self.free_texts = list(opts.get("free_texts") or ["免费", "限时免费", "促销", "２Ｘ免费", "2X免费"])
        # HR 判定位置：list=按列表页标记（默认）；detail=列表页没有标记，逐条查详情页
        # 彩虹岛（ptchdbits）属于后者，列表页连 hitandrun 类名都没有，只有详情页写 H&R: Nday
        mode = str(opts.get("hr_check") or "").strip().lower()
        if mode not in ("list", "detail"):
            mode = "detail" if "ptchdbits" in self.base_url.lower() else "list"
        self.hr_check_detail = mode == "detail"

    # ---------------- 地址拼装 ---------------------------

    def _promotion_variants(self) -> List[Dict[str, Any]]:
        """按促销筛选拼出的列表页参数变体。

        NexusPHP 家族用 `spstate` 过滤促销，但**各站编号含义不统一**：
        HDHome 实测 spstate=1 是无促销、2 是免费、3 是 2X、4 是 2X免费；
        另一些站的 0/1/2 含义又不一样。所以这里不猜语义，把候选编号挨个抓
        一遍，最后统一交给 `_promotion_of` 按真实标记过滤 —— 宁可多请求几页，
        也不要因为映射猜错而漏掉免费种。

        站点可在 options 里用 promotion_params 覆盖（例如 [{"spstate": 2}]）。
        """
        if "all" in self.promotions:
            return []  # 不限促销时抓默认列表页就够
        conf = self.options.get("promotion_params")
        if conf:
            return [dict(p) for p in conf]
        return [{"spstate": n} for n in (1, 2, 3, 4)]

    def _browse_urls(self) -> List[str]:
        browse_params = self.options.get("browse_params")
        extra = self.options.get("extra_params") or {}
        if isinstance(extra, str):
            extra = dict(p.split("=", 1) for p in extra.split("&") if "=" in p)
        base_variants: List[Dict[str, Any]] = []
        if browse_params:
            for item in browse_params:
                params = dict(item) if isinstance(item, dict) else {}
                params.update(extra)
                base_variants.append(params)
        else:
            base_variants.append(dict(extra))

        promo_variants = self._promotion_variants()
        variants: List[Dict[str, Any]] = []
        if promo_variants:
            for pv in promo_variants:
                for bv in base_variants:
                    params = dict(bv)
                    params.update(pv)
                    variants.append(params)
        else:
            variants = base_variants

        urls = []
        for path in self.browse_paths:
            base = urljoin(self.base_url + "/", path)
            for params in variants:
                for page in range(self.page_start, self.page_start + self.pages):
                    query = dict(params)
                    if self.pages > 1 or page > self.page_start:
                        query[self.page_param] = page
                    full = f"{base}?{urlencode(query)}" if query else base
                    urls.append(full)
        return urls

    # ---------------- 抓取 ---------------------------

    def _rows_of(self, text: str) -> List[str]:
        """把 HTML 拼回「一行一个种子」。

        HDHome / CHD 这类改版站的一个种子会横跨两个外层 <tr>：名字与促销图标在
        `<tr class="sticky_top">`，大小 / 做种 / 下载在紧接着的下一段。所以从
        含种子链接的片段开始，往后累积，直到出现容量格为止；一旦下一片段里
        出现了「长标题链接」就说明已经换到下一个种子了，立刻收手。

        单层结构的站（绝大多数 NexusPHP），拼第一片就能找到容量格，行为与
        原来的一行一段完全一致。
        """
        row_split = str(self.options.get("row_split") or "").strip()
        if row_split:
            # 站点给了 row_split（行首标记的正则）就精确切行：按锚点留余量的切法会把
            # 「上一行末尾的促销标签 / 下一行开头的图标」滚进本行窗口，造成促销错配。
            # TTG 的 <tr> 里一个种子的信息横跨多段，通用合并逻辑会把相邻两个种子并成
            # 一行（840870 的行里混进了 32850、842226 的 free 图标），所以必须按行首切。
            chunks = []
            starts = [m.start() for m in re.finditer(row_split, text, re.I)]
            for k, start in enumerate(starts):
                end = starts[k + 1] if k + 1 < len(starts) else len(text)
                seg = text[start:end]
                if DETAIL_RE.search(seg) or any(r.search(seg) for r in ALT_ID_RES):
                    chunks.append(seg)
            # 每一段本身就是完整一行，不能再走下面的「向后合并」逻辑，否则会把相邻两行
            # 拼成一个种子（憨憨 100 行会被合并成 50 行）
            return chunks
        chunks = split_tr_chunks(text)
        if not chunks:
            # div 版式站（憨憨这类 Tailwind 改版 NexusPHP）没有 <tr>，退回「种子锚点」
            # 切片：每个锚点向前留 400 字符余量（名字通常在链接里），一直切到下一个
            # 锚点的余量位置为止。
            anchors = sorted({max(0, m.start() - 400) for m in ANY_ID_RE.finditer(text)})
            for k, start in enumerate(anchors):
                end = anchors[k + 1] if k + 1 < len(anchors) else len(text)
                seg = text[start:end]
                if ANY_ID_RE.search(seg):
                    chunks.append(seg)
        rows: List[str] = []
        i = 0
        while i < len(chunks):
            if not (DETAIL_RE.search(chunks[i]) or any(r.search(chunks[i]) for r in ALT_ID_RES)):
                i += 1
                continue
            joined = chunks[i]
            j = i + 1
            while j < len(chunks) and j <= i + 4:
                if NAME_LIKE_RE.search(chunks[j]):
                    break  # 下一片段是下一个种子的标题，别吞进来
                joined += chunks[j]
                j += 1
                if self._parse_numbers(self._split_cells(joined))[0] > 0:
                    break
            rows.append(joined)
            i = j
        return rows

    # 详情页上的促销标记写在 <font class='free' ...> / class='twoupfree' 这类属性里。
    # 列表页的推断会因为「剩余时间」估不出来而误判，详情页是判断是否真免费的唯一权威信号。
    #
    # 但详情页不能全文扫描：不少站的底部有「同片其他版本」表格，那些版本各带自己的
    # 促销图标（天空 642737 的免费图标属于 1080p/720p 版本，本种 2160p 并不免费）。
    # 所以只扫「本种子标题 + 促销标签」附近的一小段窗口，锚点取以下几类元素最早出现处。
    DETAIL_ANCHORS = ["<h1", 'class="title"', 'class="torrent-title"', "torrent-title"]

    DETAIL_PROMO = [
        (re.compile(r"class=['\"](?:twoupfree|pro_free2up|free2up|2xfree|free_2x)['\"]", re.I), "free_2x"),
        # 憨憨（Tailwind 改版）：标题下方的促销标签，2X免费 与 免费 是不同类名，必须排在前面
        (re.compile(r'class=["\'][^"\']*promotion-tag-2xfree', re.I), "free_2x"),
        (re.compile(r"class=['\"](?:halfdown|half_down|pro_50)['\"]", re.I), "half_down"),
        (re.compile(r"class=['\"](?:twoup|pro_2up)['\"]", re.I), "2x"),
        (re.compile(r"class=['\"](?:free|freeleech|pro_free)['\"]", re.I), "free"),
        # 憨憨：标题后的免费标签（非免费种这里是空的 div，CSS 里同名文本不会被 class=" 前缀命中）
        (re.compile(r'class=["\'][^"\']*promotion-tag-free', re.I), "free"),
    ]

    # 明确指向「本种子」的站内专属文案，出现在详情页正文（介绍最下方）而不是标题区，
    # 允许全文匹配。TTG 的免费种会在介绍末尾追加一行红色 Freeleech 说明。
    DETAIL_PROMO_TEXT = [
        (re.compile(r"限时不计流量", re.I), "free"),
    ]

    @classmethod
    def _detail_window(cls, html: str, torrent_id) -> str:
        """截取详情页里属于「本种子」的标题窗口，避免把别的种子/别的版本的促销图标算进来。"""
        anchors = list(cls.DETAIL_ANCHORS) + [
            f"download.php?id={torrent_id}",
            f"details.php?id={torrent_id}",
        ]
        positions = sorted({p for p in (html.find(a) for a in anchors) if p >= 0})
        if not positions:
            return ""
        start = positions[0]
        return html[max(0, start - 600): start + 2600]

    def promotion_of_torrent(self, torrent_id) -> Optional[str]:
        """读种子详情页，返回站点当前给的促销类型；读不到或页面异常返回 None。"""
        try:
            tid = int(torrent_id)
            url = f"{self.base_url}/details.php?id={tid}"
            resp = self.fetch(url)
            html = resp.text or ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{self.site_name}] 详情页取促销失败 id={torrent_id}: {e}")
            return None
        window = self._detail_window(html, tid) or html
        for pattern, name in self.DETAIL_PROMO:
            if pattern.search(window):
                return name
        for pattern, name in self.DETAIL_PROMO_TEXT:
            if pattern.search(html):
                return name
        return "none"

    def free_torrents(self) -> Generator[Torrent, Torrent, Torrent]:
        urls = self._browse_urls()
        rows_total = 0
        last_text = ""
        seen_pages = set()
        for idx, url in enumerate(urls):
            logger.info(f"[{self.site_name}] 抓取列表页({idx + 1}/{len(urls)})：{url}")
            try:
                resp = self.fetch(url)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[{self.site_name}] 列表页请求失败：{e}")
                continue
            last_text = resp.text or ""
            rows = self._rows_of(last_text)
            ids = set()
            for row in rows:
                m = DETAIL_RE.search(row)
                if m:
                    ids.add(m.group(1))
            signature = frozenset(ids)
            if signature and signature in seen_pages:
                # 不支持促销筛选的站，几个变体会返回同一页内容，不必重复解析
                logger.info(f"[{self.site_name}] 该页与已抓页面内容重复，跳过")
                continue
            if signature:
                seen_pages.add(signature)
            logger.info(f"[{self.site_name}] 本页解析到 {len(rows)} 个种子行")
            rows_total += len(rows)
            for row in rows:
                try:
                    torrent = self._parse_row(row)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[{self.site_name}] 行解析失败：{e}")
                    continue
                if torrent is not None:
                    yield torrent
        if rows_total == 0 and LOGIN_RE.search(last_text):
            raise Exception(f"[{self.site_name}] cookie 可能已失效（返回的是登录页），请重新复制 Cookie")

    # ---------------- 行解析 ---------------------------

    def _torrent_id(self, row: str) -> Optional[int]:
        """按「详情 → download.php → action=download → 分组链接」的顺序取种子 id。"""
        m = DETAIL_RE.search(row)
        if m:
            return int(m.group(1))
        for pattern in ALT_ID_RES:
            m = pattern.search(row)
            if m:
                return int(m.group(1))
        return None

    def _parse_row(self, row: str) -> Optional[Torrent]:
        torrent_id = self._torrent_id(row)
        if torrent_id is None:
            return None

        # 行里常混着页面级脚本/样式（TTG 每行尾部带 freeleech-hint.js 之类的 <script>，
        # 文件名里的 free 会被促销匹配当成免费标签）。判定前先把脚本与样式整体剥掉。
        clean = re.sub(r"<script\b.*?</script>", " ", row, flags=re.S | re.I)
        clean = re.sub(r"<style\b.*?</style>", " ", clean, flags=re.S | re.I)
        plain = strip_tags(clean)
        promotion = self._promotion_of(clean, plain)
        if not self._promotion_allowed(promotion):
            return None
        hit_and_run = self._is_hit_and_run(clean, plain)

        cells = self._split_cells(row)
        size, seeders, leechers = self._parse_numbers(cells)
        if size <= 0:
            return None

        name = self._parse_name(row)
        if not name:
            return None

        # 列表页没有 HR 标记的站：等这条种子真的通过前几道筛选了，再花一次详情页请求确认
        hr_hours = 0.0
        if self.hr_check_detail and not hit_and_run:
            hit_and_run, hr_hours = self._hr_from_detail(torrent_id)
        created_time = self._parse_created_time(row, plain)
        if promotion in ("free", "free_2x"):
            free_end = self._parse_free_end(plain, created_time)
        else:
            # 非免费促销没有「免费截止」的概念，给个占位值，避免被当成过期种子
            free_end = datetime.now() + timedelta(days=7)

        return Torrent(
            id=torrent_id,
            name=name,
            size=size,
            seeders=seeders,
            leechers=leechers,
            created_time=created_time,
            free_end_time=free_end,
            site=self.site_name,
            promotion=promotion,
            hit_and_run=hit_and_run,
            hr_hours=hr_hours,
        )

    def _split_cells(self, row: str) -> List[str]:
        parts = CELL_SPLIT_RE.split(row)[1:]
        cells: List[str] = []
        for part in parts:
            # &nbsp; 必须解码成空白：NexusPHP 常见 "8.44&nbsp;GB"，
            # 不解码的话大小正则会认为整格不是容量，一行就废了
            text = html.unescape(strip_tags(part)).replace("\xa0", " ")
            cells.append(re.sub(r"\s+", " ", text).strip())
        if len(cells) < 3:
            # div 版式站没有 <td>：退回按 </div> 切块，至少让大小/做种数能落到独立格
            parts = re.split(r"</div>", row)
            cells = []
            for part in parts:
                text = html.unescape(strip_tags(part)).replace("\xa0", " ")
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    cells.append(text)
        return cells

    def _parse_numbers(self, cells: List[str]) -> tuple:
        for i, cell in enumerate(cells):
            m = SIZE_CELL_RE.match(cell.replace(",", "").strip())
            if not m:
                continue
            size = int(float(m.group(1)) * SIZE_UNITS[m.group(2).upper()])
            tail = cells[i + 1: i + 4]
            numbers = [to_int(c, -1) for c in tail]
            numbers = [n for n in numbers if n >= 0]
            if len(numbers) >= 2:
                return size, numbers[0], numbers[1]
            if len(numbers) == 1:
                return size, numbers[0], 0
            return size, 0, 0
        return 0, 0, 0

    def _promotion_of(self, row: str, plain: str) -> str:
        """返回 free / free_2x / 2x / half_down / none。"""
        # 1) 标签属性优先（class / alt / title / 图标文件名都算）
        for tag in TAG_ANY_RE.findall(row):
            for pattern, promotion in PROMO_PATTERNS:
                if pattern.search(tag):
                    return promotion

        # 2) 正文中文标记
        lowered = plain.lower()
        if any(token and token.lower() in lowered for token in ("2x免费", "２ｘ免费", "免费 2x", "双倍免费")):
            return "free_2x"
        if re.search(r"【\s*(免费|限时免费|2X免费)\s*】", plain, re.I):
            return "free_2x" if "2x" in plain.lower() else "free"
        for token in self.free_texts:
            if token and token.lower() in lowered:
                return "free"
        if "半价" in plain or "50%" in plain:
            return "half_down"
        return "none"

    def _promotion_allowed(self, promotion: str) -> bool:
        if "all" in self.promotions:
            return True
        if promotion == "none":
            return False
        return promotion in self.promotions

    def _is_hit_and_run(self, row: str, plain: str) -> bool:
        for tag in TAG_ANY_RE.findall(row):
            if HIT_AND_RUN_RE.search(tag):
                return True
        return bool(HIT_AND_RUN_RE.search(plain))

    def _detail_url(self, torrent_id: int) -> str:
        return urljoin(self.base_url + "/", "details.php?id={}&hit=1".format(int(torrent_id)))

    def _hr_from_detail(self, torrent_id: int) -> tuple:
        """查详情页的 `H&R: Nday`，返回 (是否 H&R, 要求做种小时数)。

        结果按 (域名, 种子 id) 缓存 6 小时：一轮刷流里同一条种子会被重复解析，
        跨轮也没必要重查。请求失败时当作「没有 HR」，宁可漏标也不要误判成 HR。
        """
        key = (self.domain_key, int(torrent_id))
        now = time.time()
        cached = _HR_DETAIL_CACHE.get(key)
        if cached and now - cached[2] < HR_DETAIL_CACHE_TTL:
            return cached[0], cached[1]

        has_hr, hours = False, 0.0
        try:
            resp = self.fetch(self._detail_url(torrent_id))
            text = resp.text or ""
            m = HR_DETAIL_RE.search(text)
            if m:
                days = HR_DAYS_RE.search(m.group(1))
                if days:
                    hours = float(days.group(1)) * 24
                    has_hr = hours > 0
            if has_hr:
                logger.debug(f"[{self.site_name}] 种子 {torrent_id} 详情页显示 H&R：{m.group(1).strip()}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{self.site_name}] 查种子 {torrent_id} 详情页失败，按无 H&R 处理：{e}")

        if len(_HR_DETAIL_CACHE) > 5000:
            _HR_DETAIL_CACHE.clear()
        _HR_DETAIL_CACHE[key] = (has_hr, hours, now)
        return has_hr, hours

    def _clean_name(self, text: str) -> str:
        """标题里的 <br />、&nbsp; 会带出换行和多余空白，统一压成单空格。"""
        return re.sub(r"\s+", " ", html.unescape(strip_tags(text))).strip()

    @staticmethod
    def _looks_like_name(text: str) -> bool:
        # 8 字符起算：TTG 的日系短标题（BADガール 逢沢りな）只有 11 个字符，
        # 阈值定得太高会把这类行整行丢掉（漏抓）。锚点本身都是种子详情链接，不会误取导航文字。
        if len(text) < 8 or "\n" in text or "\r" in text:
            return False
        return bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fa5A-Za-z]{3,}", text))

    def _parse_name(self, row: str) -> str:
        best = ""
        for pattern in (
            r"""<a\b([^>]*[^A-Za-z](?:plugin_)?details\.php\?id=\d+[^>]*)>(.*?)</a>""",
            r"""<a\b([^>]*href=["']?(?<![A-Za-z])/t/\d+/[^>]*)>(.*?)</a>""",  # TTG 短链 /t/<id>/
        ):
            for m in re.finditer(pattern, row, re.S | re.I):
                attrs, inner = m.group(1), m.group(2)
                text = self._clean_name(inner)
                title_attr = TITLE_ATTR_RE.search(attrs)
                if title_attr:
                    titled = self._clean_name(title_attr.group(1))
                    if len(titled) > len(text):
                        text = titled
                if self._looks_like_name(text) and len(text) > len(best):
                    best = text
        if best:
            return best
        # 没有 details.php 的站（音乐站/改版站）：名字挂在 download 或分组链接上，
        # 这类锚点里常带 title 属性，优先取 title，其次取链接文本
        for attrs, inner in re.findall(r"""<a\b([^>]*)>(.*?)</a>""", row, re.S | re.I):
            if not re.search(
                r"(?:download\.php\?id=|action=download|torrents\.php\?id=)", attrs, re.I
            ):
                continue
            text = self._clean_name(inner)
            title_attr = TITLE_ATTR_RE.search(attrs)
            if title_attr:
                titled = self._clean_name(title_attr.group(1))
                if len(titled) > len(text):
                    text = titled
            if self._looks_like_name(text) and len(text) > len(best):
                best = text
        if best:
            return best
        # 兜底：列表里最长的那格就是种子名（会带上格式/促销尾巴，但比空标题安全）
        for cell in self._split_cells(row):
            text = self._clean_name(cell)
            if self._looks_like_name(text) and len(text) > len(best):
                best = text
        return best

    def _parse_created_time(self, row: str, plain: str) -> datetime:
        for value in TITLE_ATTR_RE.findall(row):
            m = DATETIME_RE.search(value)
            if m:
                return self._to_datetime(m.group(1), m.group(2))
        m = DATETIME_RE.search(plain)
        if m:
            return self._to_datetime(m.group(1), m.group(2))
        return datetime.now()

    @staticmethod
    def _to_datetime(date_str: str, time_str: str) -> datetime:
        fmt = "%Y-%m-%d %H:%M:%S" if time_str.count(":") == 2 else "%Y-%m-%d %H:%M"
        try:
            return datetime.strptime(f"{date_str} {time_str}", fmt)
        except ValueError:
            return datetime.now()

    def _parse_free_end(self, plain: str, created_time: datetime) -> datetime:
        m = REMAIN_RE.search(plain)
        if m:
            chunk = m.group(1)
            days = sum(int(x) for x in REMAIN_DAY_RE.findall(chunk))
            hours = sum(int(x) for x in REMAIN_HOUR_RE.findall(chunk))
            minutes = sum(int(x) for x in REMAIN_MIN_RE.findall(chunk))
            delta = timedelta(days=days, hours=hours, minutes=minutes)
            if delta.total_seconds() > 0:
                return datetime.now() + delta
        return datetime.now() + timedelta(hours=self.default_free_hours)

    # ---------------- 站内搜索（跨站搜资源） ---------------------------

    # 各站搜索参数不统一：主流 NexusPHP 用 search，少数（TTG 等）用 searchstr，
    # 按顺序试，命中就停，避免为一个参数差异把整站都搜不到
    SEARCH_VARIANTS = (
        {"search": "{kw}", "search_area": "0", "search_mode": "0", "incldead": "1"},
        {"searchstr": "{kw}", "search_area": "0", "search_mode": "0", "incldead": "1"},
        {"search": "{kw}"},
        {"searchstr": "{kw}"},
    )

    def _search_path(self) -> str:
        return urljoin(self.base_url + "/", self.browse_paths[0])

    def _parse_subtitle(self, row: str, name: str = "") -> str:
        """取种子名单元格 `<br>` 之后的副标题（含站点标签文字），只作字幕/匹配判断用。

        只在「含详情链接的那一格」里找 `<br>`：整行的第一个 `<br>` 常常落在剩余时间/
        分类格里（会抓到「13时」这种碎片），那类单元格里没有 details.php 链接，直接跳过。
        """
        name_head = (name or "")[:24]
        for cell in self._split_cells(row):
            m = SUB_BR_RE.search(cell)
            if not m:
                continue
            head = cell[:m.start()]
            if "details.php?id=" not in head and "/t/" not in head:
                continue
            # 名字只写在 <a title> / onmouseover 里时 strip_tags 后看不到，所以两处都比一遍；
            # 完全对不上名字的格子（分类、简介）不拿来当副标题
            if name_head not in head and name_head not in self._clean_name(head):
                continue
            text = self._clean_name(m.group(1))
            # 我堡这类站的副标题藏在 onmouseover 的 JS 字符串里，尾部会拖出
            # ', 'trail', true, 'delay' … 这种参数，截断掉
            text = re.split(r"'\s*,\s*'", text, maxsplit=1)[0]
            text = text.strip(" ',\";")
            if not text or len(text) < 3:
                continue
            if text.startswith(name) and len(text) > len(name) + 4:
                text = text[len(name):].strip()
            if re.fullmatch(r"[\[\]（）()·|\-—\s]+", text):
                continue
            return text[:220]
        return ""

    def _parse_list_row(self, row: str) -> Optional[Dict[str, Any]]:
        """列表行 → 搜索结果字典。不做促销筛选，搜到什么都给用户看。"""
        torrent_id = self._torrent_id(row)
        if torrent_id is None:
            return None
        name = self._parse_name(row)
        if not name:
            return None
        plain = strip_tags(row)
        size, seeders, leechers = self._parse_numbers(self._split_cells(row))
        created = self._parse_created_time(row, plain)
        return {
            "id": torrent_id,
            "name": name,
            "subtitle": self._parse_subtitle(row, name),
            "tags": self._parse_row_tags(row, name),
            "size": size,
            "size_text": human_size(size),
            "seeders": seeders,
            "leechers": leechers,
            "promotion": self._promotion_of(row, plain),
            "hr": self._is_hit_and_run(row, plain),
            "created": created.strftime("%Y-%m-%d %H:%M"),
            "detail_url": urljoin(self.base_url + "/", f"details.php?id={torrent_id}"),
        }

    def _parse_row_tags(self, row: str, name: str = "") -> str:
        """取「名字之后」那一小段纯文字，用作字幕标记的补充依据。

        名字后面紧跟的通常是站点的标签区（中字 / 特效字幕 / 国语 / HDR…）和 onmouseover
        里带出来的中文副标题；从名字往后的这一段属于本行，不会误取上一行的尾巴。
        """
        pos = row.find(name[:24]) if name else -1
        if pos < 0:
            return ""
        tail = strip_tags(row[pos:])
        return re.sub(r"\s+", " ", tail).strip()[:260]

    def search(self, keyword: str, limit: int = 60) -> List[Dict[str, Any]]:
        kw = (keyword or "").strip()
        if len(kw) < 2:
            return []
        seen = set()
        results: List[Dict[str, Any]] = []
        last_text = ""
        for variant in self.SEARCH_VARIANTS:
            params = {k: (kw if v == "{kw}" else v) for k, v in variant.items()}
            url = f"{self._search_path()}?{urlencode(params)}"
            try:
                resp = self.fetch(url)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{self.site_name}] 搜索请求失败 {url}：{e}")
                continue
            last_text = resp.text or ""
            for row in self._rows_of(last_text):
                item = self._parse_list_row(row)
                if not item or item["id"] in seen:
                    continue
                seen.add(item["id"])
                results.append(item)
            if results:
                break
        if not results:
            # 页面是登录/二次验证页时，别当成「本站没有这个资源」糊过去
            if LOGIN_RE.search(last_text) or "take2fa.php" in last_text:
                raise Exception("站点要求重新登录或二次验证，cookie 已失效")
            if last_text and len(kw) >= 4 and "搜索" not in last_text:
                # 页面能打开但结构异常：把情况抛出去，界面上能看出来是哪一站的问题
                logger.warning(f"[{self.site_name}] 搜索页未解析到任何种子行，可能是版式差异")
        results.sort(key=lambda x: x["seeders"], reverse=True)
        return results[:limit]

    def parse_torrent_link(self, torrent_id: str) -> str:
        if self.download_url_template:
            return self.download_url_template.format(
                base_url=self.base_url,
                id=torrent_id,
                passkey=self.options.get("passkey", ""),
            )
        # TTG 走 /dl/<id>/<token>、天空走带 sign 的 download.php，凭空拼 download.php?id=
        # 会被站点当 404（点下载就回「下载到的内容是空的」）。详情页里其实带着完整链接，
        # 先取一次真实链接，取不到再退回老写法。
        link = self._download_link_from_details(torrent_id)
        if link:
            return link
        return f"{self.base_url}/download.php?id={torrent_id}"

    # 详情页里的下载入口：TTG 是 /dl/<id>/<token>，天空/多数 NexusPHP 是 download.php?...
    DETAIL_DL_RES = (
        re.compile(r'href="([^"]*?/dl/[0-9]+/[^"\'<>]+)"', re.I),
        re.compile(r'(?:action|href)="([^"]*?download\.php\?[^"\'<>]+)"', re.I),
    )

    def _download_link_from_details(self, torrent_id: str) -> str:
        tid = str(torrent_id)
        try:
            resp = self.fetch(f"{self.base_url}/details.php?id={tid}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{self.site_name}] 详情页取下载链接失败 id={tid}: {e}")
            return ""
        text = resp.text or ""
        # 第一轮只认「能直接下的」链接：/dl/<id>/<token>、带 sign= 或 passkey= 的 download.php
        for pattern in self.DETAIL_DL_RES:
            for m in pattern.finditer(text):
                url = html.unescape(m.group(1)).strip()
                if "/dl/" in url:
                    return urljoin(self.base_url + "/", url)
                if f"id={tid}" in url and ("sign=" in url or "passkey=" in url):
                    return urljoin(self.base_url + "/", url)
        # 第二轮放宽：同一 id 的 download.php 就行（部分站不校验签名）
        for pattern in self.DETAIL_DL_RES:
            for m in pattern.finditer(text):
                url = html.unescape(m.group(1)).strip()
                if f"id={tid}" in url:
                    return urljoin(self.base_url + "/", url)
        return ""
