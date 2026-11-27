#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   model.py
@Time    :   2024/11/02 14:25:38
@Author  :   huihuidehui 
@Desc    :   None
'''
from datetime import datetime
from math import sqrt
import math
from pydantic import BaseModel, computed_field





# 促销类型对「抢上传」的权重：上传双倍的最值钱，非免费的要扣下载量，不该优先
PROMO_WEIGHT = {
    "free_2x": 1.5,
    "2x": 1.1,
    "free": 1.0,
    "half_down": 0.7,
    "normal": 0.5,
    "": 1.0,
}


def brush_score(leechers, seeders, size, created_time=None, promotion="free", now=None) -> int:
    """
    抢上传评分。分高 = 这个种子更可能在短时间内吃到大量上传。

    - 需求比：下载人数 / sqrt(做种人数+1)。下载的人多、抢的人少，上传才轮得到你。
    - 新鲜度：2 小时内满权重，之后按约 24 小时衰减——刚发布的种才有大批下载者，
      老种即使 leech 数看着多，也基本被前面的人吃干净了。
    - 促销：免费双倍权重最高；非免费要扣自己的下载量，抢上传不划算，权重压低。
    - 体积：越大越能吃上传，但下完要更久、占槽位更久，所以只用 log 温和加权，
      避免大包把「同时下载数」全占满、迟迟进不了做种状态。
    """
    now = now or datetime.now()
    try:
        leechers = max(0, int(leechers or 0))
        seeders = max(0, int(seeders or 0))
        size = max(0, int(size or 0))
    except (TypeError, ValueError):
        return 0
    if size <= 0 or leechers <= 0:
        return 0

    demand = leechers / sqrt(seeders + 1)

    fresh = 0.5
    if created_time is not None:
        try:
            age_hours = max(0.0, (now - created_time).total_seconds() / 3600.0)
        except TypeError:
            age_hours = 0.0
        fresh = 1.0 if age_hours <= 2 else max(0.15, math.exp(-(age_hours - 2) / 24.0))

    promo = PROMO_WEIGHT.get(str(promotion or "free").lower(), 1.0)
    size_gb = size / 1024.0 ** 3
    size_factor = 1.0 + math.log10(max(size_gb, 0.1))

    score = int(60 * demand * fresh * promo * size_factor)
    return max(0, min(score, 2_000_000_000))


class Torrent(BaseModel):
    id:int
    leechers:int = 0
    seeders:int = 0
    name:str
    created_time:datetime
    free_end_time:datetime
    size: int # 字节大小
    site:str
    # 促销类型：free / free_2x / 2x / half_down（见 config.config.PROMOTION_META）
    promotion: str = "free"
    # 站点是否给这个种子标了 H&R（Hit and Run）
    hit_and_run: bool = False
    # 站点要求的 H&R 做种小时数（详情页 H&R: 3day → 72），0 表示站点没写、按全局设置
    hr_hours: float = 0
    @computed_field
    def score(self)->int:
        """抢上传评分（算法说明见上面的 brush_score）。"""
        return brush_score(
            self.leechers, self.seeders, self.size,
            self.created_time, self.promotion,
        )
        