#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
@File    :   mteam.py
@Time    :   2024/11/02 14:37:48
@Author  :   huihuidehui 
@Desc    :   None
"""


from datetime import datetime, timedelta
import json
from time import sleep
from typing import Generator, Optional

from loguru import logger
from model import Torrent
from ptsite import BaseSiteSpider
from ptsite.base import human_size
import jsonpath


class MTeamSpider(BaseSiteSpider):
    TYPE = "mteam"
    NAME = "M-Team"
    HOST = "api.m-team.cc"
    API = "api/torrent/search"
    TORRENT_API = "api/torrent/genDlToken"
    PAGE_SIZE = 200
    BODYS = [
        # 电影最新
        {
            "categories": [],
            "mode": "movie",
            "visible": 1,
            "pageNumber": 1,
            "pageSize": 200,
            "sortDirection": "DESC",
            "sortField": "CREATED_DATE",
            "discount":"FREE",
        },
        # 成人最新
        {
            "categories": [],
            "mode": "adult",
            "visible": 1,
            "pageNumber": 1,
            "pageSize": 200,
            "sortDirection": "DESC",
            "sortField": "CREATED_DATE",
            "discount":"FREE",
        },
        # 电视最新
        {
            "categories": [],
            "mode": "tvshow",
            "visible": 1,
            "pageNumber": 1,
            "pageSize": 200,
            "sortDirection": "DESC",
            "sortField": "CREATED_DATE",
            "discount":"FREE",
        },
        # 综合最新
        {
            "categories": [],
            "mode": "normal",
            "visible": 1,
            "pageNumber": 1,
            "pageSize": 200,
            "sortDirection": "DESC",
            "sortField": "CREATED_DATE",
            "discount":"FREE",
        },
        # 排行榜 下载数最多
        {
            "categories": [],
            "mode": "rankings",
            "visible": 1,
            "pageNumber": 1,
            "pageSize": 100,
            "sortDirection": "DESC",
            "sortField": "LEECHERS",
        },
    ]

    

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # M-Team 的 Web API 只用 x-api-key 鉴权：同时塞一个裸 Authorization 会被回 code=1「無效的請求」
        api_key = str(self.options.get("api_key") or "").strip()
        if api_key:
            self.headers["x-api-key"] = api_key
        token = str(self.options.get("token") or "").strip()
        if token:
            self.headers["Authorization"] = token
        self.headers.setdefault("Content-Type", "application/json")

    # 出错时日志里只留这么多字：整段响应（尤其是被劫持时拿回的网页）会把日志撑成几十 KB
    ERR_SNIPPET = 200

    def _brief(self, text: str) -> str:
        text = " ".join((text or "").split())
        head = text[:15].lower()
        tag = "[HTML]" if head.startswith(("<!doc", "<html", "<?xml")) else ""
        return f"{tag}{text[:self.ERR_SNIPPET]}"

    @staticmethod
    def _peer_of(resp) -> str:
        """尽力拿对端 IP，方便判断请求到底被导去了哪台机器（拿不到就返回空串）。"""
        try:
            sock = resp.raw._connection.sock
            return str(sock.getpeername()[0]) if sock else ""
        except Exception:
            return ""

    def _search_once(self, body):
        """请求一次搜索接口。返回 (data, err)；
        err 为 None 表示成功，否则形如 ("non_json", 摘要, 原文) 或 ("api", 说明, "")。"""
        resp = self.fetch(
            url=f"https://{self.HOST}/{self.API}",
            method="POST",
            data=json.dumps(body),
        )
        text = resp.text
        try:
            payload = json.loads(text)
        except Exception:
            meta = f"server={resp.headers.get('server', '?')} peer={self._peer_of(resp) or '?'}"
            return None, ("non_json", meta, text)
        code = payload.get("code")
        if code not in (0, "0", None):
            return None, ("api", f"code={code} message={payload.get('message')}", "")
        return (payload.get("data") or {}).get("data"), None

    # 出口被劫持时拿回的是网页而不是 JSON，重试多半就通了；3 次足够把偶发失败压到很低
    SEARCH_TRIES = 3

    def free_torrents(self) -> Generator[Torrent, Torrent, Torrent]:
        for body in self._bodies():
            logger.info(
                f"searching mt body:{json.dumps(body, separators=(',', ':'), ensure_ascii=False)}"
            )
            data, err = None, None
            for attempt in range(1, self.SEARCH_TRIES + 1):
                data, err = self._search_once(body)
                if not err:
                    break
                if err[0] != "non_json":
                    break  # 401/403 这类换几次也没用，直接报出来
                if attempt < self.SEARCH_TRIES:
                    logger.warning(
                        f"mt search 第{attempt}次拿到非 JSON 响应（{err[1]}），2 秒后重试：{self._brief(err[2])}"
                    )
                    sleep(2)
            if err:
                if err[0] == "non_json":
                    logger.error(
                        f"mt search 连续 {self.SEARCH_TRIES} 次拿到非 JSON（{err[1]}），"
                        f"基本可判定出口异常/被劫持：{self._brief(err[2])}"
                    )
                else:
                    logger.error(f"mt search 接口拒绝（401/403 一般是 api_key 权限不足或已过期）：{err[1]}")
                sleep(60)
                continue
            if data:
                for item in data:
                    promotion = self._promotion_of(item)
                    if not self._promotion_allowed(promotion):
                        continue
                    free_end = self._parse_free_end_time(item)
                    if promotion in ("free", "free_2x") and not free_end:
                        # 免费种却拿不到结束时间，按默认小时数估算，避免被当成永久免费
                        yield self._parse_torrent(item, promotion=promotion, fallback_hours=True)
                        continue
                    yield self._parse_torrent(item, promotion=promotion)

            # 睡会，别请求太快
            sleep(60)

    def _bodies(self):
        """按配置决定搜索条件：只要免费时用服务端 discount 过滤，否则放开让客户端筛。"""
        allow_only_free = set(self.promotions) <= {"free", "free_2x"} and "all" not in self.promotions
        bodies = []
        for body in self.BODYS:
            body = dict(body)
            if allow_only_free:
                body["discount"] = "FREE"
            else:
                # 让服务端不要用 FREE 过滤，否则 2X / 半价种子根本搜不出来
                body.pop("discount", None)
            bodies.append(body)
        return bodies

    def _promotion_of(self, item: dict) -> str:
        """把 M-Team 的 discount 归一化成 free / free_2x / 2x / half_down / none。"""
        discount = (jsonpath.jsonpath(item, "$.status.discount") or [""])[0] or ""
        discount = str(discount).upper()
        if "2X" in discount and "FREE" in discount:
            return "free_2x"
        if "FREE" in discount:
            return "free"
        if "2X" in discount:
            return "2x"
        if "50" in discount or "HALF" in discount:
            return "half_down"
        statuses = jsonpath.jsonpath(item, "$.status.mallSingleFree.status") or []
        if statuses and statuses[0] == "ONGOING":
            return "free"
        return "none"

    def _promotion_allowed(self, promotion: str) -> bool:
        if "all" in self.promotions:
            return True
        if promotion == "none":
            return False
        return promotion in self.promotions

    def _is_hit_and_run(self, item: dict) -> bool:
        for path in ("$.status.hr", "$.status.hitAndRun", "$.status.hitandrun", "$.hr"):
            values = jsonpath.jsonpath(item, path)
            if values and values[0] not in (False, 0, "0", "", None, "NO", "no"):
                return True
        return False



    def _parse_free_end_time(self, item:dict)->Optional[str]:
        discount_end_times_1 = jsonpath.jsonpath(item, "$.status.discountEndTime")
        if discount_end_times_1 and discount_end_times_1[0]:
            return discount_end_times_1[0]
        discount_end_times_2 = jsonpath.jsonpath(item, "$.status.mallSingleFree.endDate")
        if discount_end_times_2 and discount_end_times_2[0]:
            return discount_end_times_2[0]
        return None
        

    def _parse_torrent(self, item: dict, promotion: str = "free", fallback_hours: bool = False) -> Torrent:
        free_end_time_str = self._parse_free_end_time(item)
        if free_end_time_str:
            free_end_time = datetime.strptime(free_end_time_str, "%Y-%m-%d %H:%M:%S")
        elif fallback_hours:
            free_end_time = datetime.now() + timedelta(hours=self.default_free_hours)
        else:
            free_end_time = datetime.now() + timedelta(days=7)

        torrent = Torrent(
            name=item.get("name"),
            id=item.get("id"),
            seeders=item.get("status").get("seeders"),
            leechers=item.get("status").get("leechers"),
            size=int(item.get("size")),
            created_time=datetime.strptime(
                item.get("createdDate"), "%Y-%m-%d %H:%M:%S"
            ),
            free_end_time=free_end_time,
            site=self.site_name,
            promotion=promotion,
            hit_and_run=self._is_hit_and_run(item),
        )

        return torrent

    def search(self, keyword: str, limit: int = 60):
        """按关键字搜站内种子（搜片 → 跨站搜资源）。"""
        kw = (keyword or "").strip()
        if len(kw) < 2:
            return []
        body = {
            "keyword": kw,
            "pageNumber": 1,
            "pageSize": min(100, max(20, limit)),
            "visible": 1,
            "categories": [],
        }
        data, err = None, None
        for attempt in range(1, self.SEARCH_TRIES + 1):
            data, err = self._search_once(body)
            if not err or err[0] != "non_json":
                break
            if attempt < self.SEARCH_TRIES:
                sleep(1.5)
        if err:
            logger.warning(f"mt 站内搜索「{kw}」失败：{err[1] if err[0] != 'non_json' else self._brief(err[2])}")
            return []

        base = (self.options.get("base_url") or "https://kp.m-team.cc").rstrip("/")
        results = []
        for item in (data or []):
            status = item.get("status") or {}
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            results.append({
                "id": item.get("id"),
                "name": item.get("name") or "",
                "subtitle": (item.get("smallDescr") or "").strip()[:220],
                "size": size,
                "size_text": human_size(size),
                "seeders": int(status.get("seeders") or 0),
                "leechers": int(status.get("leechers") or 0),
                "promotion": self._promotion_of(item),
                "hr": self._is_hit_and_run(item),
                "created": str(item.get("createdDate") or "")[:16],
                "detail_url": f"{base}/detail/{item.get('id')}",
            })
        results.sort(key=lambda x: x["seeders"], reverse=True)
        return results[:limit]

    def parse_torrent_link(self, torrent_id: str) -> str:
        """
        获取种子下载链接。

        M-Team 的两个接口要求不同的请求体编码：
        - search 要 application/json（headers 里常驻 Content-Type: application/json）
        - genDlToken 只认 form-urlencoded
        两者共用同一份 headers，带着 json 头发 form 体会被一律回 code=1「參數錯誤」，
        这正是馒头候选全部卡在「获取种子下载链接失败」的原因。这里临时摘掉
        Content-Type（不带头时 requests 自动按 form 编码），拿到响应再恢复。
        """
        content_type = self.headers.pop("Content-Type", None)
        try:
            response = self.fetch(
                url=f"https://{self.HOST}/{self.TORRENT_API}",
                method="POST",
                data={"id": str(torrent_id)},
            )
        finally:
            if content_type:
                self.headers["Content-Type"] = content_type
        try:
            payload = json.loads(response.text)
        except Exception:
            logger.error(f"mt genDlToken 返回非 JSON：{self._brief(response.text)}")
            return None
        code = payload.get("code")
        if code not in (0, "0", None):
            logger.error(
                f"mt genDlToken 被拒：code={code} message={payload.get('message')} id={torrent_id}"
            )
            return None
        data = payload.get("data")
        # 正常是字符串 URL，个别版本回 {"url": "..."}
        if isinstance(data, dict):
            return data.get("url") or ""
        return data or ""

    def download_torrent_content(self, torrent_link:str)->Optional[bytes]:
        """
        获取种子内容
        """
        torrent_download_res = self.fetch(torrent_link, verify=False)
        try:
            text = torrent_download_res.text
            json.loads(text)
            logger.error(f"mt download torrent link error:{text} link:{torrent_link}")
            return None
        except:
            return torrent_download_res.content
