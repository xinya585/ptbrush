#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   __init__.py
@Time    :   2024/11/04 10:34:05
@Author  :   huihuidehui 
@Version :   1.0
'''

# here put the import lib
from time import sleep
from loguru import logger
from tasks.services import (
    PtTorrentService,
    QBTorrentService,
    BrushService,
    SiteDataService,
    vacuum_database as _vacuum_database,
)
# 给所有任务加一个装饰器，进行错误捕获
def catch_error(func):
    def wrapper():
        try:
            func()
        except Exception as e:
            # logger.exception 会带上完整 traceback；任务名一并打出来，否则光看一句话没法回溯
            name = getattr(func, "__name__", "unknown")
            logger.exception(f"任务执行出错[{name}]: {e}")
    return wrapper



# 抓取PT站种子
@catch_error
def fetch_pt_torrents():
    PtTorrentService().fetcher()

# 抓取QB中的种子信息
@catch_error
def fetch_qb_torrents():
    QBTorrentService().fetcher()

# 抓取QB的信息
@catch_error
def fetch_qb_status():
    QBTorrentService().fetch_qb_status()

# 刷流
@catch_error
def brush():
    add_torrent_count = BrushService().brush()
    if add_torrent_count > 0:
        logger.info(f"1分钟后,开始拆包任务...")
        sleep(60)
        QBTorrentService().torrent_thinned()
    

# 清理长时间没有上传的种子
@catch_error
def clean_long_time_no_activate_torrents():
    QBTorrentService().clean_long_time_no_activate()

# 清理即将过期种子


# 对大包种子进行瘦身
@catch_error
def torrent_thinned():
    QBTorrentService().torrent_thinned()

# 定期检查刷流种子：到期清理 + 无活跃清理 + 条件删种 + 动态删种（周期由 check_interval 控制）
@catch_error
def check_brush_torrents():
    service = QBTorrentService()
    service.clean_will_expired()
    service.clean_long_time_no_activate()
    # 低速/等待删种：刷流任务里也会跑一遍（清出来的下载位本轮就能补），这里再跑一次
    # 是为了让「检查周期」这段说明成立：轮不到刷流（不在工作时间）时也能按周期清理
    service.clean_by_slow_speed()
    service.check_all()

# 对数据库执行 VACUUM 瘦身（每 7 天一次，避免高频整库重写写废 SSD）
@catch_error
def vacuum_database():
    _vacuum_database()


# 刷新站点账号数据（用户名 / 上传下载量 / 分享率 / 做种数 / 图标）
@catch_error
def refresh_site_data():
    SiteDataService().refresh()


# 只刷「正在执行的刷流站点」的账号数据：仪表盘那张卡每 10 分钟读一次，数据得跟上
@catch_error
def refresh_brush_sites():
    _refresh_brush_sites()


def _refresh_brush_sites():
    """只刷参与刷流的站点（外加 qB 里还挂着刷流种的站），避免 14 站全量刷新太频繁。"""
    from config.config import PTBrushConfig, normalize_target_sites, site_key

    cfg = PTBrushConfig()
    sites = list(cfg.sites or [])
    only: list = []
    for key in normalize_target_sites(list(getattr(cfg.brush, "target_sites", None) or []), sites):
        only.append(str(key))
    try:
        qb_sites = QBTorrentService().brush_site_names()
        for name in qb_sites:
            for site in sites:
                if getattr(site, "name", "") == name:
                    key = site_key(site)
                    if key not in only:
                        only.append(key)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"取 qB 里的刷流站点失败（不影响刷新）: {e}")
    if not only:
        return
    SiteDataService().refresh(only=only)


# 定期更新 pre-dessert-sites 站点配置库：同步站点图标 + 对比适配规则变化
@catch_error
def predessert_update():
    from tasks.predessert import update_library

    update_library(reason="计划任务")
