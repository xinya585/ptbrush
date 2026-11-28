#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   task.py
@Time    :   2024/11/04 09:56:17
@Author  :   huihuidehui 
@Version :   1.0
'''


from datetime import datetime, timedelta
import json
import time
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from paths import data_dir

# 未完成、但不该占「下载位」的状态：等待（没有速度）/ 排队 / 暂停
_IDLE_DOWNLOAD_STATES = {"stalledDL", "queuedDL", "pausedDL", "stoppedDL"}

# 低速计时：{hash: 首次低于阈值的秒级时间戳}。
# 放模块级而不是实例上——BrushService 每轮刷流都会重新 new 一个，
# 挂在实例上等于每轮清零，「持续低速」永远判不出来。
_SLOW_SINCE: Dict[str, float] = {}

# 等待计时：{hash: 进入等待/排队状态的秒级时间戳}，给「低速下载持续就删」数等待时长用
_WAITING_SINCE: Dict[str, float] = {}

# 低速上传计时：{hash: 首次低于上传阈值的秒级时间戳}，给「低速上传持续就删（盒子专用）」用。
# 和下载侧的三本表完全独立：一个种可能下载很快、上传却一直没人拉。
_SLOW_UP_SINCE: Dict[str, float] = {}

# 上面两本表是「低速下载持续就删」的计时（阈值 slow_delete_speed），
# 下面两本是「低速/等待不占下载位」的计时（阈值 download_speed_min / download_cycle）。
# 必须分开：以前共用一本，删种分支每轮都会把「速度够删种阈值」的条目 pop 掉，
# 结果 1~10 MiB/s 的种子永远攒不满 10 分钟，『持续低速不占位』对它们完全失效。
_SLOW_IDLE_SINCE: Dict[str, float] = {}
_WAIT_IDLE_SINCE: Dict[str, float] = {}

# 删种记录的短时去重表：{hash: 记录时间戳}。
# 「刷流」（10 分钟）与「删种与状态检查」（5 分钟）会在同一秒同时触发，
# 两个任务读到的是同一份种子列表，同一个种子会被删两次、记两条记录。
_DELETED_RECENT: Dict[str, float] = {}

from loguru import logger
from config.config import (
    PTBrushConfig,
    SiteModel,
    enabled_sites,
    filter_target_sites,
    site_key,
    target_site_names,
)
from model import Torrent, brush_score
from db import (
    BrushFailRecord,
    BrushTorrent,
    QBStatus,
    SeedMeta,
    Torrent as TorrentDB,
    TorrentDayDl,
    BrushDelRecord,
    database,
)
from qbittorrent import QBittorrent
from infohash import infohash_v1
from ptsite import TorrentFetch


def filter_candidates(candidates, promotions, exclude_hr=True, min_free_hours=3, now=None):
    """
    选出真正可以下载的种子（纯函数，方便离线测试）。

    规则：
    1. 促销类型必须在白名单里（promotions 含 all 表示不限制）
    2. exclude_hr 为真时，丢弃站点标记了 H&R 的种子
    3. 免费 / 2X免费 的种子，剩余免费时间必须还够 min_free_hours 小时
       （非免费促销没有免费截止，跳过这条检查）
    """
    now = now or datetime.now()
    allowed = set(str(p).lower() for p in (promotions or ["free", "free_2x"]))
    allowed = set(["free", "free_2x"]) if not allowed else allowed
    result = []
    for item in candidates:
        promotion = str(getattr(item, "promotion", "free") or "free").lower()
        if "all" not in allowed and promotion not in allowed:
            continue
        if exclude_hr and bool(getattr(item, "hit_and_run", False)):
            continue
        free_end = getattr(item, "free_end_time", None)
        if promotion in ("free", "free_2x") and min_free_hours and free_end:
            try:
                if free_end < now + timedelta(hours=float(min_free_hours)):
                    continue
            except TypeError:
                pass
        result.append(item)
    return result


# 从PT站获取种子
class PtTorrentService():

    def __init__(self):
        self._config = PTBrushConfig()

    def fetcher(self):
        """
        抓取种子，并进行存储
        """
        config = self._config
        sites = enabled_sites(config.sites)
        brush = config.brush
        # 只抓参与刷流的站点：target_sites 为空表示全部站点（target_none=True 表示一个都不刷）
        # 键是站点域名，站点改名不影响选择（同时也兼容存量按站点名存的写法）
        target = {str(n) for n in (getattr(brush, "target_sites", None) or []) if str(n).strip()}
        if bool(getattr(brush, "target_none", False)):
            logger.info("「参与刷流的站点」一个都没开，本轮跳过抓种")
            sites = []
        elif target:
            before = len(sites)
            sites = filter_target_sites(target, sites)
            logger.info(
                f"已按「参与刷流的站点」筛选：配置 {before} 个站点，本次只处理 {len(sites)} 个"
                f"（{'、'.join(s.name for s in sites) or '无'}）"
            )
        logger.info(
            f"开始抓取PT站点种子，准备处理{len(sites)}个站点"
            f"（促销类型:{'/'.join(brush.promotions)}，排除H&R:{brush.exclude_hr}）"
        )
        count = 0
        for site in sites:
            logger.info(f'开始处理站点:{site.name}, 正在初始化抓取器...')
            torrent_fetcher = None
            site_count = 0
            try:
                torrent_fetcher = TorrentFetch(
                    site,
                    promotions=brush.promotions,
                    exclude_hr=brush.exclude_hr,
                    min_free_hours=brush.min_free_hours,
                )
                for torrent in torrent_fetcher.free_torrents:
                    logger.info(
                        f"从{site.name}抓取到种子: {torrent.name}, 促销: {torrent.promotion}, "
                        f"H&R: {'是' if torrent.hit_and_run else '否'}, 大小: {torrent.size/1024/1024:.2f} MiB, "
                        f"做种数: {torrent.seeders}, 下载数: {torrent.leechers}, 评分: {torrent.score}"
                    )
                    self._insert_or_update_torrent(torrent)
                    site_count += 1
                    count += 1
                logger.info(f"站点{site.name}处理完成，已抓取{site_count}个种子")
            except Exception as e:
                # 单个站点出错不拖垮其它站
                logger.error(f"站点{site.name}抓取失败，跳过：{e}")
            finally:
                if torrent_fetcher is not None:
                    torrent_fetcher.close()
        logger.info(f"抓取PT站点种子完成，本轮共抓取到{count}个种子")

    def _insert_or_update_torrent(self, torrent: Torrent):
        updated_time = datetime.now()
        logger.info(f"正在更新种子信息: 站点={torrent.site}, ID={torrent.id}, 名称={torrent.name}")
        TorrentDB.insert(name=torrent.name, site=torrent.site, torrent_id=torrent.id, leechers=torrent.leechers, seeders=torrent.seeders,
                         size=torrent.size, free_end_time=torrent.free_end_time, score=torrent.score,
                         promotion=torrent.promotion, hit_and_run=torrent.hit_and_run,
                         hit_and_run_hours=float(getattr(torrent, "hr_hours", 0) or 0),
                         ).on_conflict(conflict_target=[TorrentDB.site, TorrentDB.torrent_id], update=dict(
                             updated_time=updated_time, leechers=torrent.leechers, seeders=torrent.seeders,
                             free_end_time=torrent.free_end_time, score=torrent.score,
                             promotion=torrent.promotion, hit_and_run=torrent.hit_and_run,
                             hit_and_run_hours=float(getattr(torrent, "hr_hours", 0) or 0))).execute()


# 从qb获取种子状态、以及下载器状态、 清理临近过期的种子
class QBTorrentService():

    def __init__(self):
        self._config = PTBrushConfig()
        # 多下载器：[[downloaders]] 每条建一个客户端，清理/瘦身要逐个下载器跑一遍；
        # 状态采集与限额判断仍以主下载器（列表第一条）为准
        self._qbs = []
        self._qb_of = {}
        for dl in self._config.downloader_list():
            client = QBittorrent(
                dl.url,
                dl.username,
                dl.password,
                dl.auth_type,
                dl.api_key,
                dl.save_path,
                # 分类名可配（默认 ptbrush），删种/统计都只认这个分类
                getattr(self._config.brush, "qb_category", "ptbrush"),
            )
            self._qbs.append(client)
            self._qb_of[dl.alias] = client
        self._qb = self._qbs[0] if self._qbs else None


    def fetch_qb_status(self):
        """
        获取qb状态
        """
        if not self._qb:
            logger.warning("没有配置任何下载器，跳过QB状态采集")
            return
        qb_status = self._qb.status
        logger.info(f"正在记录QB状态 - 上传速度: {qb_status.upspeed/1024/1024:.2f}MiB/s, 下载速度: {qb_status.dlspeed/1024/1024:.2f}MiB/s, 剩余空间: {qb_status.free_space_size/1024/1024/1024:.2f}GiB")
        QBStatus.create(
            dlspeed=qb_status.dlspeed,
            upspeed=qb_status.upspeed,
            free_space_size=qb_status.free_space_size,
            up_total_size=qb_status.up_total_size,
            dl_total_size=qb_status.dl_total_size,

        )
        pass

    @staticmethod
    def _record_deletion(torrent, reason, rule="", detail=""):
        """记一条删种记录，仪表盘的「删种记录」模块读的就是这张表。"""
        try:
            # 「刷流」和「删种与状态检查」两个任务每 10 分钟会在同一秒撞在一起，
            # 两边都读到同一份种子列表，会把同一个种子记两遍（22:49:36 的 Matilda
            # 就是两条只差 1 毫秒的记录）。这里按 hash 做 3 分钟去重。
            h = str(getattr(torrent, "hash", "") or "")
            if h:
                now = time.time()
                last = _DELETED_RECENT.get(h)
                if last and now - last < 180:
                    return
                _DELETED_RECENT[h] = now
                for k, v in list(_DELETED_RECENT.items()):
                    if now - v > 600:
                        _DELETED_RECENT.pop(k, None)
            BrushDelRecord.create(
                hash=str(getattr(torrent, "hash", "") or ""),
                # qB 里的名字带 __meta.站点.id.endTime.时间 后缀，记录里只留干净名
                name=str(getattr(torrent, "name", "") or "").split("__meta.")[0][:200],
                site=str(getattr(torrent, "site", "") or ""),
                torrent_id=str(getattr(torrent, "torrent_id", "") or ""),
                size=int(getattr(torrent, "total_size", 0) or getattr(torrent, "size", 0) or 0),
                uploaded=int(getattr(torrent, "up_total_size", 0) or 0),
                ratio=float(getattr(torrent, "ratio", 0) or 0),
                seeding_time=int(getattr(torrent, "seeding_time", 0) or 0),
                reason=str(reason or "")[:100],
                rule=str(rule or "")[:40],
                detail=str(detail or "")[:500],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"记录删种记录失败: {e}")

    def brush_site_names(self):
        """qB 里当前还挂着刷流种的站点名（中文站名），给「正在执行的刷流站点」用。"""
        names = set()
        for qb in self._qbs:
            for t in qb.torrents:
                if getattr(t, "site", ""):
                    names.add(str(t.site))
        return sorted(names)

    def clean_by_slow_speed(self):
        """
        低速下载持续 N 分钟就删（独立阈值，只按本规则的 slow_delete_speed 判断；只看下载侧，不涉及上传）

        两类都算「下不动」：
        - 速度低于 slow_delete_speed（MiB/s）并持续 N 分钟；
        - 一直卡在等待/排队/暂停状态（界面显示「等待」）持续 N 分钟还没动起来。
        填 0 分钟或阈值填 0 都表示关闭。H&R 种子跳过（删了要挨站点处罚）。
        """
        try:
            minutes = int(getattr(self._config.brush, "slow_delete_minutes", 0) or 0)
        except Exception:
            minutes = 0
        try:
            threshold_mbps = float(getattr(self._config.brush, "slow_delete_speed", 0) or 0)
        except Exception:
            threshold_mbps = 0.0
        if minutes <= 0 or threshold_mbps <= 0:
            return 0
        threshold = threshold_mbps * 1024 * 1024
        window = minutes * 60
        hr_keys = self._hit_and_run_keys()
        now = time.time()
        count = 0
        logger.info(
            f"开始清理下不动的种子：低于 {threshold_mbps:.2f} MiB/s 或一直等待，"
            f"持续 {minutes} 分钟以上就删"
        )
        alive = set()
        for qb in self._qbs:
            for torrent in qb.torrents:
                if torrent.completed:
                    continue
                alive.add(torrent.hash)
                state = (getattr(torrent, "state", "") or "")
                if state in _IDLE_DOWNLOAD_STATES:
                    # 等待中：速度本来就是 0，单独按「卡在等待多久」判断
                    since = _WAITING_SINCE.setdefault(torrent.hash, now)
                    _SLOW_SINCE.pop(torrent.hash, None)
                    if now - since < window:
                        continue
                    reason, rule = "一直卡在等待状态，没开始下载", "等待"
                    detail = f"{state} 已持续 {int((now - since) / 60)} 分钟未动（阈值 {minutes} 分钟）"
                    speed_mb = 0.0
                else:
                    _WAITING_SINCE.pop(torrent.hash, None)
                    speed_mb = (getattr(torrent, "dlspeed", 0) or 0) / 1024 / 1024
                    if speed_mb >= threshold_mbps:
                        _SLOW_SINCE.pop(torrent.hash, None)
                        continue
                    since = _SLOW_SINCE.setdefault(torrent.hash, now)
                    if now - since < window:
                        continue
                    reason, rule = "下载速度持续过低", "低速"
                    detail = (
                        f"{speed_mb:.2f} MiB/s 低于 {threshold_mbps:.2f} MiB/s，"
                        f"已持续 {int((now - since) / 60)} 分钟未下完"
                    )

                if (str(torrent.site or ""), str(torrent.torrent_id or "")) in hr_keys:
                    logger.info(f"下不动但带 H&R 标记，保留继续处理: {torrent.name}")
                    continue

                logger.info(f"{reason}，删除种子: {torrent.name} | {detail}")
                try:
                    qb.delete_torrents([torrent.hash])
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"删除下不动的种子失败: {e}")
                    continue
                self._record_deletion(torrent, reason, rule=rule, detail=detail)
                _SLOW_SINCE.pop(torrent.hash, None)
                _WAITING_SINCE.pop(torrent.hash, None)
                count += 1

        # 已经删掉/下完的，计时一并清掉
        for book in (_SLOW_SINCE, _WAITING_SINCE):
            for h in list(book.keys()):
                if h not in alive:
                    book.pop(h, None)
        logger.info(f"清理下不动的种子完成，本次删除种子数:{count}")
        return count

    def clean_by_slow_upload(self):
        """
        低速上传持续 N 分钟就删（盒子专用）

        只判**已经下载完成**的种子：下载中的种还在抢带宽、做种价值还没成形，
        拿它当下的上传速度去判"没价值"会误删抢手种。已下完的种挂着只有
        0.0x MiB/s，才说明这个种真没人要。

        上传速度一直低于 slow_up_delete_speed（MiB/s），持续 slow_up_delete_minutes
        分钟就删掉，把保种位和上传带宽让给新热门种。盒子上位子有限，一个种挂着
        只有 0.0x MiB/s，留着纯占坑。

        - H&R 种子跳过（删了要挨站点处罚，由它自己的做种时长规则回收）；
        - 独立开关 slow_up_delete_enabled：关着就完全不判（阈值/分钟只是草稿），
          打开后还要求阈值和分钟都 > 0，双保险；
        - 计时从上传掉到阈值以下那一刻起算，中途回升就清零。
        """
        if not bool(getattr(self._config.brush, "slow_up_delete_enabled", False)):
            # 关着的时候把计时清空：否则关了几小时再打开，旧时间戳会让种子立刻被判超时删掉
            _SLOW_UP_SINCE.clear()
            return 0
        try:
            minutes = int(getattr(self._config.brush, "slow_up_delete_minutes", 0) or 0)
        except Exception:
            minutes = 0
        try:
            threshold_mbps = float(getattr(self._config.brush, "slow_up_delete_speed", 0) or 0)
        except Exception:
            threshold_mbps = 0.0
        if minutes <= 0 or threshold_mbps <= 0:
            _SLOW_UP_SINCE.clear()
            return 0
        window = minutes * 60
        hr_keys = self._hit_and_run_keys()
        now = time.time()
        count = 0
        logger.info(
            f"开始清理低速上传的种子（盒子专用，只判已下载完成的种子）：低于 "
            f"{threshold_mbps:.2f} MiB/s 持续 {minutes} 分钟以上就删"
        )
        alive = set()
        for qb in self._qbs:
            for torrent in qb.torrents:
                # 只看下载完成的种子：还在下的种上传低是正常的，不能据此删
                if not torrent.completed:
                    continue
                alive.add(torrent.hash)
                upspeed_mbps = (getattr(torrent, "upspeed", 0) or 0) / 1024 / 1024
                if upspeed_mbps >= threshold_mbps:
                    _SLOW_UP_SINCE.pop(torrent.hash, None)
                    continue
                since = _SLOW_UP_SINCE.setdefault(torrent.hash, now)
                if now - since < window:
                    continue
                if (str(torrent.site or ""), str(torrent.torrent_id or "")) in hr_keys:
                    logger.info(f"低速上传但带 H&R 标记，保留继续做种: {torrent.name}")
                    continue
                detail = (
                    f"{upspeed_mbps:.2f} MiB/s 低于 {threshold_mbps:.2f} MiB/s，"
                    f"已持续 {int((now - since) / 60)} 分钟（阈值 {minutes} 分钟）"
                )
                logger.info(f"上传速度持续过低，删除种子: {torrent.name} | {detail}")
                try:
                    # 删前先汇报一次做种数据，别让站点少记一轮上传
                    qb.reannounce([torrent.hash])
                    qb.delete_torrents([torrent.hash])
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"删除低速上传的种子失败: {e}")
                    continue
                self._record_deletion(
                    torrent, "长时间没有有效上传", rule="低速上传", detail=detail
                )
                _SLOW_UP_SINCE.pop(torrent.hash, None)
                count += 1
        # 已经删掉/掉出列表的种子清掉计时，别让表越攒越大
        for h in list(_SLOW_UP_SINCE.keys()):
            if h not in alive:
                _SLOW_UP_SINCE.pop(h, None)
        logger.info(f"低速上传清理完成，本次删除种子数:{count}")
        return count

    def clean_will_expired(self):
        """
        清理临近过期的种子（配了多个下载器时每个都清一遍）
        """
        minutes = int(getattr(self._config.brush, "free_end_delete_minutes", 30) or 0)
        if minutes <= 0:
            logger.info("免费期提前删种已关闭（阈值 0 分钟），跳过")
            return 0
        logger.info(f'开始清理即将过期的种子：免费截止前 {minutes} 分钟')
        count = 0
        current_timestamp = datetime.now().timestamp()
        # 截止时间戳：免费结束前 N 分钟
        expire_timestamp = current_timestamp + minutes * 60
        # H&R 种子不能按这个逻辑删，否则会吃站点的 H&R 处罚
        hr_keys = self._hit_and_run_keys()
        for qb in self._qbs:
            for torrent in qb.torrents:
                if torrent.completed:
                    continue

                free_end = getattr(torrent, "free_end_time", None)
                if not free_end:
                    continue
                free_timestamp = free_end.timestamp()
                if free_timestamp > expire_timestamp:
                    continue

                is_hr = (torrent.site, str(torrent.torrent_id)) in hr_keys
                if is_hr:
                    # H&R 种即使免费要结束也得留着下完并做种，删了要挨罚
                    logger.info(
                        f"H&R 种子免费期即将结束但仍未下完，保留继续处理: {torrent.name}")
                    continue

                # 抢上传视角：免费期结束还没下完，就已经抢不到这波上传了，
                # 留着半截文件和种子只会占磁盘、占并发位，连种子带文件一起删。
                logger.info(
                    f"免费期即将结束仍未下完，删除种子与文件: {torrent.name}, Free结束时间:{torrent.free_end_time}")
                qb.reannounce([torrent.hash])
                qb.delete_torrents([torrent.hash])
                self._record_deletion(
                    torrent, "免费期结束还没下完", rule="免费到期",
                    detail=f"免费截止 {free_end.strftime('%m-%d %H:%M')}（阈值 {minutes} 分钟），"
                           f"已下 {getattr(torrent, 'dl_total_size', 0) / 1024 ** 3:.1f} GiB",
                )
                count += 1

        logger.info(f"清理即将过期的种子完成，本次删除种子数:{count}")
        return count

    def fetcher(self):
        """
        获取所有正在刷流的种子，记录其信息（配了多个下载器时逐个采集）
        """
        logger.info(f"开始抓取QB中种子状态")
        count = 0
        for qb in self._qbs:
            for torrent in qb.torrents:
                count += 1
                self._record_torrent_stat(torrent)
        logger.info(f"抓取QB中种子状态完成，本轮共记录{count}个种子状态")

    def _track_day_download(self, torrent):
        """把每分钟的下载增量按当前促销状态记进免费/计费两个桶。"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            size = int(torrent.dl_total_size or 0)
            row = TorrentDayDl.get_or_none(hash=torrent.hash, day=today)
            if row is None:
                # 加种时就记下了促销，先用它初始化；后面每 5 分钟的详情页复核会覆盖
                # 「免费」只认免费/2X免费；白名单里可能还有 2X、50% 下载，用白名单判会把计费流量算成免费
                is_free = False
                try:
                    cand = TorrentDB.get_or_none(
                        site=str(torrent.site or ""), torrent_id=str(torrent.torrent_id or "")
                    )
                    if cand is not None:
                        promo = str(cand.promotion or "").strip().lower()
                        is_free = (not promo) or promo in ("free", "free_2x")
                except Exception:
                    pass
                TorrentDayDl.create(
                    hash=torrent.hash, day=today, site=str(torrent.site or ""),
                    base_size=size, last_size=size, is_free=is_free,
                )
                return
            delta = max(0, size - int(row.last_size or 0))
            if delta:
                if row.is_free:
                    row.free_bytes = int(row.free_bytes or 0) + delta
                else:
                    row.paid_bytes = int(row.paid_bytes or 0) + delta
            row.last_size = size
            if torrent.site:
                row.site = str(torrent.site)
            row.updated_time = datetime.now()
            row.save()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"记录当日下载流水失败: {e}")

    def _record_torrent_stat(self, torrent):
        """把一个 QB 种子落库：TorrentDB 标记已刷流，并留一条 BrushTorrent 采样。"""
        torrent_db, flag = TorrentDB.get_or_create(
            site=torrent.site, torrent_id=torrent.torrent_id,
            defaults={'name': torrent.name, 'brushed': True, 'free_end_time': torrent.free_end_time})
        torrent_db.brushed = True
        torrent_db.save()
        BrushTorrent.create(
            torrent=torrent_db,
            up_total_size=torrent.up_total_size,
            upspeed=torrent.upspeed,
            dl_total_size=torrent.dl_total_size,
            dlspeed=torrent.dlspeed,
        )
        self._track_day_download(torrent)
        logger.info(f"记录种子状态: {torrent.name} - 上传速度: {torrent.upspeed/1024/1024:.2f} MiB/s, 下载速度: {torrent.dlspeed/1024/1024:.2f} MiB/s, 已上传: {torrent.up_total_size/1024/1024:.2f} MiB, 已下载: {torrent.dl_total_size/1024/1024:.2f} MiB")

    def clean_long_time_no_activate(self):
        """
        清理长时间没有上传的种子（页面上的「无上传超时」）

        只看上传速度：已下完的种子连续 N 分钟上传速度都是 0，就认定没人拉、删掉腾位置。
        还没下完的种子不受这条影响（下不动的由「低速下载持续就删」管）。
        """
        if self._config.brush.max_no_activate_time <= 0:
            logger.info("无上传超时清理已关闭，跳过本次任务")
            return

        logger.info("开始清理长时间没有上传的种子")
        # 多下载器：种子池合并所有下载器，删除时按 hash 找回它所属的客户端
        qb_torrents = []
        hash_owner = {}
        for qb in self._qbs:
            for t in qb.torrents:
                qb_torrents.append(t)
                hash_owner[t.hash] = qb

        # 统计出所有正在刷流以及历史刷流的种子ID
        brush_torrents = BrushTorrent.select().group_by(BrushTorrent.torrent)
        torrents = set([brush_torrent.torrent for brush_torrent in brush_torrents])
        logger.info(f"当前数据库中共有{len(torrents)}个种子记录需要检查")
        
        # 处理每个torrent
        cleaned_count = 0
        for torrent in torrents:
            # 查询出所有记录
            brush_torrent_records = list(BrushTorrent.select().where(BrushTorrent.torrent == torrent).order_by(BrushTorrent.created_time.asc()))
            if not brush_torrent_records:
                continue

            # 查询对应的qb中的种子
            target_qb_torrent = [i for i in qb_torrents if f"{torrent.site}.{torrent.torrent_id}" in i.name]
            if not target_qb_torrent:
                # qb中已经删除了此种子，则删除记录
                logger.info(f"种子 {torrent.name} 在QB中已不存在，清理相关记录")
                BrushTorrent.delete().where(BrushTorrent.torrent == torrent).execute()
                continue
            target_qb_torrent = target_qb_torrent[0]
            if not target_qb_torrent.completed:
                # 只按上传判断，未下完的种子交给下载侧规则，这里不动
                continue
            
            latest_record = brush_torrent_records.pop()

            if latest_record.upspeed != 0:
                # 还在上传的种子跳过
                continue
            
            end_time = latest_record.created_time
            # 统计出无活动的持续时间
            while brush_torrent_records:
                latest_record = brush_torrent_records.pop()
                if latest_record.upspeed != 0:
                    break
            start_time = latest_record.created_time
            if self._config.brush.max_no_activate_time < 5:
                logger.warning(f"max_no_activate_time配置值小于5分钟，使用默认值5分钟")
                max_no_activate_time = 5
            else:
                max_no_activate_time = self._config.brush.max_no_activate_time
            inactive_duration = (end_time - start_time).total_seconds() / 60
            if inactive_duration > max_no_activate_time:
                logger.info(f"清理无上传种子: {torrent.name}, 无上传时长: {inactive_duration:.1f}分钟, 超过配置阈值: {max_no_activate_time}分钟")
                BrushTorrent.delete().where(BrushTorrent.torrent == torrent).execute()
                owner = hash_owner.get(target_qb_torrent.hash)
                if owner is not None:
                    # 和其它删种分支一致：删前先汇报一次做种数据，别让站点少记一次
                    owner.reannounce([target_qb_torrent.hash])
                    owner.delete_torrent(target_qb_torrent.hash)
                self._record_deletion(
                    target_qb_torrent, "长时间没有上传", rule="无上传",
                    detail=f"已 {inactive_duration:.0f} 分钟上传速度为 0（阈值 {max_no_activate_time} 分钟）",
                )
                cleaned_count += 1
            
        logger.info(f"开始清理brushtorrent表7天前的历史记录...")
        old_records = BrushTorrent.delete().where(BrushTorrent.created_time < (datetime.now() - timedelta(days=7))).execute()
        logger.info(f"已清理{old_records}条7天前的历史记录")

        logger.info(f"长时间没有上传的种子清理完成，本次共清理{cleaned_count}个")

    def torrent_thinned(self):
        """
        对下载中的种子进行瘦身（配了多个下载器时每个都过一遍）
        """
        # 单个种子最大体积填 0 = 不瘦身（界面上这项已经下线）
        if not self._config.brush.torrent_max_size or int(self._config.brush.torrent_max_size) <= 0:
            return
        logger.info(f"开始瘦身种子任务...")
        thinned_count = 0
        for qb in self._qbs:
            for torrent in qb.torrents:
                # 跳过已完成任务
                if torrent.completed:
                    continue

                # 跳过非大包种子
                if torrent.size < self._config.brush.torrent_max_size:
                    continue

                logger.info(f"正在处理大包种子: {torrent.name}, 大小: {torrent.size/1024/1024/1024:.2f}GiB")
                files = qb.get_torrent_files(torrent.hash)
                all_file_ids = [file['index'] for file in files]

                current_size = sum([file['size']
                                   for file in files if file['priority'] != 0])

                download_file_ids = []
                total_files = len(files)
                selected_files = 0
                for file in files:
                    if file['priority'] != 0:
                        if current_size > self._config.brush.torrent_max_size:
                            # 超出了大小限制，将文件设置为不下载
                            file['priority'] = 0
                            current_size -= file['size']
                        else:
                            download_file_ids.append(file['index'])
                            selected_files += 1

                no_download_file_ids = list(
                    set(all_file_ids) - set(download_file_ids))

                qb.set_no_download_files(torrent.hash, no_download_file_ids)
                logger.info(
                    f"种子{torrent.name}瘦身完成 - 选择下载: {selected_files}/{total_files}个文件, 预计大小: {current_size/1024/1024:.2f} MiB")
                thinned_count += 1

        logger.info(f"瘦身种子任务完成，本次共处理{thinned_count}个大包种子")

    # ------------------------------------------------ 条件删种 / 动态删种

    def _hit_and_run_keys(self) -> Dict[tuple, float]:
        """所有带 H&R 标记的种子 (站点, 种子ID) → 站点要求的做种小时数（0=按全局设置）。

        返回 dict 也能直接当 set 用（`key in hr_keys` 行为不变），只是多带了小时数，
        彩虹岛的 5day 种才不会被 72 小时规则提前删掉。
        """
        try:
            rows = TorrentDB.select().where(TorrentDB.hit_and_run == True)  # noqa: E712
            return {
                (r.site, str(r.torrent_id)): float(r.hit_and_run_hours or 0)
                for r in rows
            }
        except Exception as e:
            logger.warning(f"读取 H&R 标记失败: {e}")
            return {}

    @staticmethod
    def _rule_of_reason(reason: str) -> str:
        """条件删种的三条规则各自给一个规则名，供仪表盘「删种记录」区分。"""
        reason = str(reason or "")
        if reason.startswith("H&R"):
            return "H&R"
        if reason.startswith("做种"):
            return "做种时长"
        if reason.startswith("分享率"):
            return "分享率"
        return "条件"

    def _delete_reason(self, t, is_hr: bool, hr_hours: float = 0) -> Optional[str]:
        """
        单个种子的删除理由，返回 None 表示不删。
        顺序对齐 MoviePilot：H&R 种单独分支 → 做种时长 → 分享率（仅已完成）。
        「累计上传量」「未完成下载耗时」「平均上传速度」三条已下线，配置字段保留只为兼容旧 config.toml。
        """
        cfg = self._config.brush
        # H&R 种子无论如何都走单独分支，绝不落进普通条件，避免被站点记 H&R
        if is_hr:
            # 站点自己在详情页写了要求（彩虹岛 3day / 5day），取它和全局设置里更长的那个
            need_hours = max(float(cfg.hr_seed_time or 0), float(hr_hours or 0))
            if need_hours:
                # H&R 只看做种时长，分享率再高也不提前删，避免没做满站点要求被判 H&R
                if t.seeding_time >= need_hours * 3600:
                    return f"H&R 做种达到 {need_hours:g} 小时"
                return None
            if cfg.seed_ratio and t.completed and t.ratio >= float(cfg.seed_ratio):
                return f"H&R 分享率达到 {cfg.seed_ratio}"
            return None
        if cfg.seed_time and t.seeding_time >= float(cfg.seed_time) * 3600:
            return f"做种达到 {cfg.seed_time} 小时"
        if cfg.seed_ratio and t.completed and t.ratio >= float(cfg.seed_ratio):
            return f"分享率达到 {cfg.seed_ratio}"
        return None

    @staticmethod
    def _set_day_promo(hash_value, site_name, is_free):
        """把某个种子今天的促销状态记进下载流水表，后面按它把流量分到免费/计费桶。"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            row = TorrentDayDl.get_or_none(hash=hash_value, day=today)
            if row is None:
                TorrentDayDl.create(
                    hash=hash_value, day=today, site=str(site_name or ""), is_free=bool(is_free),
                )
            elif bool(row.is_free) != bool(is_free):
                row.is_free = bool(is_free)
                row.updated_time = datetime.now()
                row.save()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"记录当日促销状态失败: {e}")

    def verify_free_status(self) -> int:
        """
        促销复核：在下的种子每轮向站点详情页确认是否仍然免费。

        列表页给的「剩余免费」有时是估算值（站点没写剩余时间时按 24 小时猜），
        免费其实已经结束还继续下，站点就把流量记进下载量 —— 白烧分享率。
        详情页是权威信号：一旦不再是免费/2X免费，立刻删种止损。
        """
        cfg = self._config.brush
        allowed = list(cfg.promotions or [])
        if not allowed:
            return 0
        # 勾了「不限制」= 什么促销都接受，就没有「不再免费」这个概念，只刷新标记、绝不删种
        allow_all = "all" in allowed
        site_map = {str(getattr(s, "name", "") or ""): s for s in (self._config.sites or [])}
        clients = {}
        count = 0
        for qb in self._qbs:
            for torrent in qb.torrents:
                if torrent.completed:
                    continue
                site = site_map.get(str(getattr(torrent, "site", "") or ""))
                if site is None or not getattr(torrent, "torrent_id", None):
                    continue
                client = clients.get(site.name)
                if client is None:
                    try:
                        client = TorrentFetch(site, promotions=None, exclude_hr=False, min_free_hours=0)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[{site.name}] 建站点客户端失败，跳过促销复核: {e}")
                        client = False
                    clients[site.name] = client
                if client is False or not hasattr(client, "promotion_of_torrent"):
                    continue
                try:
                    promo = client.promotion_of_torrent(torrent.torrent_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[{site.name}] 促销复核异常: {e}")
                    continue
                if promo is None:
                    # 详情页没取到（网络/解析问题），不下结论，避免误删
                    continue
                self._set_day_promo(torrent.hash, torrent.site, allow_all or promo in allowed)
                if allow_all or promo in allowed:
                    continue
                # 其余情况（none / half_down / 2x 等）都视为不再免费
                logger.info(
                    f"站点已不再免费（当前促销: {promo}），删除种子止损: {torrent.name}")
                try:
                    qb.reannounce([torrent.hash])
                    qb.delete_torrents([torrent.hash])
                    self._record_deletion(
                        torrent, "促销已经结束，再下要计入下载量", rule="促销失效",
                        detail=f"详情页当前促销：{promo}",
                    )
                    count += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"删除非免费种子失败: {e}")
        if count:
            logger.info(f"促销复核完成，本轮因「不再免费」删除 {count} 个种子")
        return count

    def clean_by_conditions(self) -> int:
        """条件删种：做种时长 / 分享率（H&R 另有单独分支）。"""
        cfg = self._config.brush
        enabled = any([cfg.seed_time, cfg.hr_seed_time, cfg.seed_ratio])
        if not enabled:
            logger.info("条件删种全部为 0（未启用），跳过本次")
            return 0
        hr_keys = self._hit_and_run_keys()
        deleted = 0
        for qb in self._qbs:
            try:
                torrents = qb.torrents
            except Exception as e:
                logger.error(f"读取下载器种子失败，条件删种跳过该下载器: {e}")
                continue
            to_delete = []
            hits = []
            for t in torrents:
                hr_hours = hr_keys.get((t.site, str(t.torrent_id)), 0)
                is_hr = (t.site, str(t.torrent_id)) in hr_keys
                reason = self._delete_reason(t, is_hr, hr_hours)
                if reason:
                    to_delete.append(t.hash)
                    hits.append((t, reason))
            if not to_delete:
                continue
            # 删之前先汇报一次，让站点尽快记下这一轮做种数据
            qb.reannounce(to_delete)
            if qb.delete_torrents(to_delete):
                deleted += len(to_delete)
                for t, reason in hits:
                    logger.info(f"条件删种: {t.name}（{reason}）")
                    # 三条规则写各自的规则名，别都笼统记成「条件」，仪表盘按规则能直接分辨
                    self._record_deletion(t, reason, rule=self._rule_of_reason(reason))
        logger.info(f"条件删种完成，本次共删除 {deleted} 个种子")
        return deleted

    def check_all(self) -> dict:
        """一次检查里跑完条件删种 + 促销复核 + 低速上传清理，供 check_interval 调度。
        「保种体积上限（动态删种）」已下线，volume 固定返回 0，字段保留给前端兼容。"""
        conditions = self.clean_by_conditions()
        promo = self.verify_free_status()
        # 盒子专用：做种中但长期不上传的种，腾保种位
        slow_up = self.clean_by_slow_upload()
        return {"conditions": conditions, "volume": 0, "promo": promo, "slow_up": slow_up}


# 刷流逻辑
class BrushService():
    def __init__(self):
        self._config = PTBrushConfig()
        # 站点可以各自绑定下载器：按别名建客户端，加种时按站点自己的配置选
        self._qbs = []
        self._qb_of = {}
        for dl in self._config.downloader_list():
            client = QBittorrent(
                dl.url,
                dl.username,
                dl.password,
                dl.auth_type,
                dl.api_key,
                dl.save_path,
                # 分类名可配（默认 ptbrush），删种/统计都只认这个分类
                getattr(self._config.brush, "qb_category", "ptbrush"),
            )
            self._qbs.append(client)
            self._qb_of[dl.alias] = client
        # 主下载器：剩余空间/任务数/限速这些全局判断以它为准
        self._qb = self._qbs[0] if self._qbs else None
        # 本轮刷流的诊断数据：候选数 / 被拒原因 / 实际加种数
        self.last_stats: Dict[str, object] = {"candidates": 0, "filtered": 0, "added": 0, "reasons": {}}

    def _qb_for_site(self, site_config):
        """按站点绑定的别名取下载器；没绑定或别名失效时退回主下载器。"""
        if not self._qbs:
            return None
        alias = (getattr(site_config, "downloader", "") or "").strip()
        if alias and alias in self._qb_of:
            return self._qb_of[alias]
        return self._qb

    
    @property
    def qb_free_space_size(self) -> int:
        """
        当前qb剩余空间
        """
        if not self._qb:
            return 0
        return self._qb.status.free_space_size

    def _downloading_breakdown(self):
        """
        未完成种子按「是不是真的在下」拆开数，返回 (占下载位的, 等待中的, 持续低速的)。

        等待（stalledDL，qB 里显示「等待」）、排队（queuedDL）、暂停都不占下载位；
        下载速度低于 download_speed_min（MiB/s）并且持续 download_cycle 秒以上也不占位。
        统计跨全部下载器合并计数——站点可以绑定不同下载器，只看主下载器会让别的下载器上的种子白占位。
        """
        if not self._qbs:
            return 0, 0, 0
        cfg = self._config.brush
        try:
            threshold = float(getattr(cfg, "download_speed_min", 0) or 0) * 1024 * 1024
        except Exception:
            threshold = 0.0
        try:
            window = max(60, int(getattr(cfg, "download_cycle", 600) or 600))
        except Exception:
            window = 600
        active = waiting = slow = 0
        now = time.time()
        alive = set()
        for qb in self._qbs:
            try:
                torrents = qb.torrents
            except Exception as e:  # noqa: BLE001
                logger.warning(f"读取下载器种子失败，下载位统计跳过该下载器: {e}")
                continue
            for i in torrents:
                if i.completed:
                    continue
                alive.add(i.hash)
                state = (getattr(i, "state", "") or "")
                if state in _IDLE_DOWNLOAD_STATES:
                    _SLOW_IDLE_SINCE.pop(i.hash, None)
                    _WAIT_IDLE_SINCE.setdefault(i.hash, now)
                    waiting += 1
                    continue
                _WAIT_IDLE_SINCE.pop(i.hash, None)
                if threshold > 0 and int(getattr(i, "dlspeed", 0) or 0) < threshold:
                    since = _SLOW_IDLE_SINCE.setdefault(i.hash, now)
                    if now - since >= window:
                        slow += 1
                        continue
                else:
                    _SLOW_IDLE_SINCE.pop(i.hash, None)
                active += 1
        # 已经把种子删掉/下完的，计时也清掉，避免字典越来越大
        for book in (_SLOW_IDLE_SINCE, _WAIT_IDLE_SINCE):
            for h in list(book.keys()):
                if h not in alive:
                    book.pop(h, None)
        return active, waiting, slow

    @property
    def uncompleted_count(self) -> int:
        """
        当前真正在下载的刷流任务数（等待中、持续低速的不算）
        """
        return self._downloading_breakdown()[0]

    def get_brush_torrent(self, count: int = 10) -> List[Torrent]:
        """
        从种子库挑出可以下载的种子。

        先按评分从高到低取一个较大的候选池，再逐站套用选种策略
        （全局 [brush].promotions / exclude_hr / min_free_hours，站点 options 里写了则覆盖），
        这样多站可以各用各的规则。
        """
        cfg = self._config.brush
        # 存量候选的评分是按老公式算的，这里按「抢上传」公式重算一遍，避免旧分把老种顶上来
        self._refresh_candidate_scores()
        pool_size = max(count * 30, 200)
        # 只从参与刷流的站点里挑种：target_sites 为空表示全部站点（target_none=True 表示一个都不刷）
        # 选择项按域名存，这里翻译成当前站点名（数据库里 site 列存的是站点名）
        if bool(getattr(cfg, "target_none", False)):
            logger.info("「参与刷流的站点」一个都没开，本轮不挑种")
            return []
        target = {str(n) for n in (getattr(cfg, "target_sites", None) or []) if str(n).strip()}
        query = TorrentDB.select().where(TorrentDB.brushed == False)  # noqa: E712
        if target:
            names = target_site_names(target, enabled_sites(self._config.sites or []))
            if not names:
                logger.warning("「参与刷流的站点」没匹配到任何站点，本次不挑种")
                return []
            query = query.where(TorrentDB.site.in_(names))
        candidates = list(
            query.order_by(TorrentDB.score.desc()).limit(pool_size)
        )
        if not candidates:
            return []

        # 每个站点各自的策略
        policies: dict = {}
        for site in self._config.sites or []:
            opts = site.options or {}
            promotions = opts.get("promotions") or cfg.promotions
            exclude_hr = opts.get("exclude_hr", cfg.exclude_hr)
            if exclude_hr is None:
                exclude_hr = cfg.exclude_hr
            min_free_hours = opts.get("min_free_hours", cfg.min_free_hours)
            policies[site.name] = (promotions, bool(exclude_hr), float(min_free_hours))

        # 没有配置的站点用全局策略
        fallback = (cfg.promotions, bool(cfg.exclude_hr), float(cfg.min_free_hours))

        reasons: Dict[str, int] = {}
        # qB 里还没下完的同名种子（按站点归类），用于跨站同名保护
        unfinished = self._unfinished_titles_by_site()

        selected_db = []
        checked = 0
        for row in candidates:
            checked += 1
            promotions, exclude_hr, min_free_hours = policies.get(row.site, fallback)
            if not filter_candidates([row], promotions, exclude_hr, min_free_hours):
                reasons["促销/H&R/免费剩余时间不符"] = reasons.get("促销/H&R/免费剩余时间不符", 0) + 1
                continue
            # 失败退避：上一轮加种失败过的候选，短期内不再重试，避免反复烧站点下载额度
            if self._in_backoff(row.site, row.torrent_id):
                reasons["失败退避中"] = reasons.get("失败退避中", 0) + 1
                continue
            # 选种增强：包含/排除正则、体积区间、做种人数区间、发布时间区间
            ok, why = self._pass_extra_filters(row)
            if not ok:
                reasons[why] = reasons.get(why, 0) + 1
                continue
            # 跨站同名未完成保护：别的站点正在下这个片子，先别重复下
            site_of_title = unfinished.get(str(row.name))
            if site_of_title and row.site not in site_of_title:
                reasons["其他站点同名种子未下完"] = reasons.get("其他站点同名种子未下完", 0) + 1
                continue
            selected_db.append(row)
            if len(selected_db) >= count:
                break

        # 只统计真正检查过的候选：选满即停，没看过的不能算成「被筛掉」
        self.last_stats = {
            "candidates": len(candidates),
            "checked": checked,
            "filtered": checked - len(selected_db),
            "added": 0,
            "added_items": [],
            "reasons": reasons,
            "selected": [
                {"name": r.name, "site": r.site, "size_gb": round((r.size or 0) / 1024 ** 3, 2)}
                for r in selected_db
            ],
        }
        skipped = checked - len(selected_db)
        if skipped > 0:
            logger.info(
                f"检查候选种子 {checked} 个（候选池 {len(candidates)} 个），按选种策略筛掉 {skipped} 个，"
                f"原因明细: {reasons}"
            )

        # 抢上传：同样是「能下」，先下体积小的——下完就能进做种，槽位周转更快；
        # 大包让它排队等下一轮，避免五个并发位被大包长期占住、一个都出不了种。
        selected_db.sort(key=lambda r: (r.size or 0))
        logger.info(
            "本轮选种（按抢上传排序）: " + " | ".join(
                f"{r.name[:40]}({r.size / 1024 ** 3:.1f}GiB)" for r in selected_db[:5]
            )
        )

        return [
            Torrent(
                id=i.torrent_id,
                leechers=i.leechers,
                name=i.name,
                seeders=i.seeders,
                created_time=i.created_time,
                free_end_time=i.free_end_time,
                size=i.size,
                site=i.site,
                promotion=i.promotion or "free",
                hit_and_run=bool(i.hit_and_run),
            )
            for i in selected_db
        ]

    def _refresh_candidate_scores(self) -> None:
        """按新公式重算未刷种子的评分（只改分数变了的行，避免每轮全表重写）。"""
        try:
            rows = list(TorrentDB.select().where(TorrentDB.brushed == False))  # noqa: E712
        except Exception as e:
            logger.warning(f"重算候选评分时读取种子库失败（跳过）: {e}")
            return
        changed = 0
        for r in rows:
            try:
                new_score = brush_score(r.leechers, r.seeders, r.size, r.created_time, r.promotion)
            except Exception:
                continue
            if r.score != new_score:
                r.score = new_score
                r.save(only=[TorrentDB.score])
                changed += 1
        if changed:
            logger.info(f"已按抢上传公式重算 {changed} 个候选种子的评分")

    def _get_site_config(self, site: str) -> Optional[SiteModel]:
        for i in self._config.sites:
            if i.name == site:
                return i
        return None

    def _set_brushed(self, torrent: Torrent):
        torrent_db = TorrentDB.get_or_none(
            TorrentDB.site == torrent.site, TorrentDB.torrent_id == torrent.id)
        torrent_db.brushed = True
        torrent_db.save()

    def add_brush_torrent(self, torrents: List[Torrent]):

        for torrent in torrents:
            # 下载前再兜一道：配置可能刚刚被改过
            if torrent.hit_and_run and self._config.brush.exclude_hr:
                logger.info(f"跳过 H&R 种子（按排除H&R设置）: {torrent.name} (站点:{torrent.site})")
                self._set_brushed(torrent)
                continue

            site_config = self._get_site_config(torrent.site)
            if not site_config:
                logger.error(f"获取站点{torrent.site}配置失败，跳过种子{torrent.name}")
                continue
            
            logger.info(f"正在处理种子: {torrent.name} (站点:{torrent.site}, ID:{torrent.id})")
            torrent_fetch = TorrentFetch(site_config)
            try:
                torrent_link = torrent_fetch.parse_torrent_link(torrent.id)
                if not torrent_link:
                    logger.error(f"获取种子下载链接失败，跳过种子{torrent.name}")
                    self._record_failure(torrent.site, torrent.id, "获取下载链接失败")
                    continue
                # 站点没给免费截止时间时不能直接 strftime（会 AttributeError 打断整轮加种），
                # 用一个远期占位，免费到期规则自然就不会碰它
                end_time = getattr(torrent, "free_end_time", None)
                if not end_time:
                    end_time = datetime.now() + timedelta(days=365)
                    logger.warning(f"站点没给免费截止时间，暂按 1 年后处理（免费到期规则不会动它）: {torrent.name}")
                # 种子名保持站点原名。站点、免费截止时间加种后按 infohash 记进数据库，
                # 不再拼进名字（拼了会让转种/转发到其他站点时文件名多出 garbage 后缀）
                torrent_rename = torrent.name
                torrent_content = torrent_fetch.download_torrent_content(torrent_link)
                if not torrent_content:
                    logger.error(f"下载种子内容失败，跳过种子{torrent.name}")
                    self._record_failure(torrent.site, torrent.id, "下载种子内容失败")
                    continue
            finally:
                torrent_fetch.close()
            # 站点绑定哪个下载器就推给哪个；没绑定就用主下载器
            qb = self._qb_for_site(site_config)
            if qb is None:
                logger.error(f"没有可用的下载器，跳过种子{torrent.name}")
                continue
            # 站点填了保存路径就用它，没填则用下载器的默认保存路径（分类路径）
            site_save_path = (getattr(site_config, "save_path", "") or "").strip()
            target_path = site_save_path or (getattr(qb, "default_save_path", "") or "")
            # 记下加种前的 hash 集合：万一算不出 infohash（比如 v2-only 种子），用差集兜底
            before_hashes = None
            try:
                before_hashes = {x.get("hash") for x in qb.qb.torrents_info(category=qb.category)}
            except Exception as e:  # noqa: BLE001
                logger.debug(f"加种前读取种子列表失败（忽略）: {e}")
            res = qb.download_torrent_url(torrent_content, torrent_rename, save_path=site_save_path)
            if res:
                logger.info(
                    f"成功添加种子到QB: {torrent.name} (大小:{torrent.size/1024/1024:.2f} MiB, "
                    f"保存路径:{target_path or 'qB默认(_ptbrush)'})"
                )
                self._set_brushed(torrent)
                self._remember_hash(qb, torrent, torrent_content, end_time, before_hashes)
                self.last_stats["added"] = int(self.last_stats.get("added") or 0) + 1
                self.last_stats.setdefault("added_items", []).append(
                    {
                        "name": torrent.name,
                        "site": torrent.site,
                        "size_gb": round((torrent.size or 0) / 1024 ** 3, 2),
                    }
                )
            else:
                logger.error(f"添加种子到QB失败: {torrent.name}")
                self._record_failure(torrent.site, torrent.id, "下载器拒绝加种")

            # return res

    def _remember_hash(self, qb, torrent: Torrent, torrent_content: bytes, end_time, before_hashes=None):
        """加种成功后把 infohash 与该种子的站点、免费截止时间记进 seedmeta，读取时不再依赖种子名。"""
        target_hash = infohash_v1(torrent_content)
        if target_hash:
            for _ in range(3):
                try:
                    if qb.qb.torrents_info(torrent_hashes=target_hash):
                        break
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"校验 infohash 出错: {e}")
                time.sleep(0.3)
            else:
                target_hash = ""
        if not target_hash and before_hashes is not None:
            # 算不出 v1 hash（v2-only 种子等）：拿加种前后的差集，多出来那一个就是它
            try:
                after = {x.get("hash") for x in qb.qb.torrents_info(category=qb.category)}
                added = [h for h in (after - before_hashes) if h]
                if len(added) == 1:
                    target_hash = added[0]
            except Exception as e:  # noqa: BLE001
                logger.debug(f"按差集定位新种子失败: {e}")
        if not target_hash:
            # v2 种子算不出 v1 hash，或刚加进去还没被 qB 收录：按名字在分类里反查一次
            try:
                for t in qb.qb.torrents_info(category=qb.category):
                    if str(t.get("name") or "").split("__meta.")[0] == torrent.name:
                        target_hash = t.get("hash") or ""
                        if target_hash:
                            break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"按名字反查 infohash 失败: {e}")
        if not target_hash:
            logger.warning(f"没能拿到种子的 infohash，这颗种子暂时按名字兜底识别: {torrent.name}")
            return
        try:
            SeedMeta.insert(
                hash=target_hash,
                site=torrent.site,
                torrent_id=str(torrent.id),
                name=torrent.name,
                free_end_time=end_time,
            ).on_conflict_replace().execute()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"写入种子 hash 映射失败: {e}")

    # ------------------------------------------------------------ 选种辅助

    def _unfinished_titles_by_site(self) -> Dict[str, Set[str]]:
        """qB 里还没下完的种子 → 标题到站点的映射，用于跨站同名保护。"""
        result: Dict[str, Set[str]] = {}
        for qb in self._qbs:
            try:
                for t in qb.torrents:
                    if t.completed:
                        continue
                    title = str(t.name).split("__meta.")[0]
                    if title:
                        result.setdefault(title, set()).add(t.site)
            except Exception as e:
                logger.warning(f"读取下载器种子用于重复保护失败: {e}")
        return result

    def _in_backoff(self, site: str, torrent_id) -> bool:
        """这个候选是否还在失败退避窗口内。"""
        minutes = int(getattr(self._config.brush, "fail_backoff_minutes", 0) or 0)
        if minutes <= 0:
            return False
        cutoff = datetime.now() - timedelta(minutes=minutes)
        try:
            row = (
                BrushFailRecord.select()
                .where(
                    (BrushFailRecord.site == site)
                    & (BrushFailRecord.torrent_id == str(torrent_id))
                    & (BrushFailRecord.created_time >= cutoff)
                )
                .first()
            )
        except Exception:
            return False
        return row is not None

    def _record_failure(self, site: str, torrent_id, reason: str) -> None:
        """记录一次加种失败，供失败退避使用。"""
        try:
            row, _ = BrushFailRecord.get_or_create(
                site=site, torrent_id=str(torrent_id), defaults={"reason": reason[:200]}
            )
            row.reason = reason[:200]
            row.created_time = datetime.now()
            row.save()
        except Exception as e:
            logger.warning(f"记录加种失败退避失败（不影响主流程）: {e}")

    def _pass_extra_filters(self, row) -> tuple:
        """
        选种增强条件（对齐 MoviePilot）：
        - include/exclude：对标题做正则匹配
        - size：单值是下限，区间要求落在区间内（单位 GiB）
        - seeder：单值是上限，区间要求落在区间内
        - pubtime：单值是上限（只看最近多少分钟），区间要求落在区间内
        """
        cfg = self._config.brush
        name = str(getattr(row, "name", "") or "")
        if cfg.include:
            try:
                if not re.search(cfg.include, name, re.I):
                    return False, "不符合包含规则"
            except re.error:
                pass
        if cfg.exclude:
            try:
                if re.search(cfg.exclude, name, re.I):
                    return False, "命中排除规则"
            except re.error:
                pass
        if cfg.size:
            gb = float(getattr(row, "size", 0) or 0) / 1024 ** 3
            parts = [float(x) for x in str(cfg.size).split("-")]
            if len(parts) == 1:
                if gb < parts[0]:
                    return False, f"体积 {gb:.1f} GiB 低于下限 {parts[0]:g} GiB"
            elif not (parts[0] <= gb <= parts[1]):
                return False, f"体积 {gb:.1f} GiB 不在 {cfg.size} GiB 区间内"
        if cfg.seeder:
            seeders = int(getattr(row, "seeders", 0) or 0)
            parts = [float(x) for x in str(cfg.seeder).split("-")]
            if len(parts) == 1:
                if seeders > parts[0]:
                    return False, f"做种人数 {seeders} 超过上限 {parts[0]:g}"
            elif not (parts[0] <= seeders <= parts[1]):
                return False, f"做种人数 {seeders} 不在 {cfg.seeder} 区间内"
        if cfg.pubtime:
            created = getattr(row, "created_time", None)
            if created:
                minutes = (datetime.now() - created).total_seconds() / 60
                parts = [float(x) for x in str(cfg.pubtime).split("-")]
                if len(parts) == 1:
                    if minutes > parts[0]:
                        return False, f"发布已 {minutes:.0f} 分钟，超过上限 {parts[0]:g} 分钟"
                elif not (parts[0] <= minutes <= parts[1]):
                    return False, f"发布已 {minutes:.0f} 分钟，不在 {cfg.pubtime} 区间内"
        return True, ""

    def _site_ratio_block(self) -> tuple:
        """站点分享率闸已下线（界面不再提供该项），恒定放行；相关配置字段保留只为兼容旧 config.toml。"""
        return False, ""

    def _target_site_labels(self) -> List[str]:
        """把「参与刷流站点」的域名键翻译成中文站名，空选择表示全部站点参与。"""
        cfg = self._config.brush
        sites = list(getattr(self._config, "sites", None) or [])
        try:
            if bool(getattr(cfg, "target_none", False)):
                return []
            if cfg.target_sites:
                return [n for n in target_site_names(cfg.target_sites, sites) if n]
            return [str(getattr(x, "name", "") or "") for x in sites if getattr(x, "name", None)]
        except Exception:
            return list(cfg.target_sites or [])

    def _judge_payload(self) -> Dict[str, object]:
        """选种阈值快照：写进每一轮诊断报告，方便核对「界面配置」与「实际生效」是否一致。"""
        cfg = self._config.brush
        return {
            "promotions": list(cfg.promotions or []),
            "size_range": cfg.size,
            "seeder_range": cfg.seeder,
            "pubtime": cfg.pubtime,
            "min_free_hours": cfg.min_free_hours,
            "exclude_hr": bool(cfg.exclude_hr),
            "target_sites": list(cfg.target_sites or []),
            # 界面显示用中文站名；target_sites 留域名键备查
            "target_site_names": self._target_site_labels(),
        }

    def _write_report(self, **extra) -> None:
        """把本轮刷流结果写成 data/brush_report.json，供 Web 端展示运行诊断。"""
        data = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        data.update(self.last_stats)
        data.update(extra)
        try:
            path = data_dir() / "brush_report.json"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"写刷流诊断报告失败（不影响刷流）: {e}")

    def brush(self)->int:
        """
        刷流入口,返回添加种子的个数
        """
        logger.info(f"刷流任务开始...")
        # 没配置下载器或站点时不要跑刷流：否则每轮都会写一份「0 候选」的诊断报告，
        # 看起来像历史记录一直残留。初始化状态下仪表盘应当保持空白。
        if not self._qbs:
            logger.info("没有配置下载器，跳过刷流")
            return 0
        if not self._config.sites:
            logger.info("没有配置站点，跳过刷流")
            return 0
        # 站点分享率闸：达到目标就暂停新增
        blocked, block_reason = self._site_ratio_block()
        if blocked:
            logger.info(block_reason)
            self.last_stats = {"candidates": 0, "filtered": 0, "added": 0, "reasons": {}}
            self._write_report(result="site_ratio_blocked", message=block_reason)
            return 0
        # 先把「一直没速度」的种子按 slow_delete_minutes 清掉，清出来的下载位本轮就能补；
        # 盒子专用那条（低速上传就删）一并跑，保种位也能同时腾出来。
        # 删种逻辑在 QBTorrentService（H&R 判定、删种记录都在那边），这里借它的实例跑一遍
        cleaner = QBTorrentService()
        cleaner.clean_by_slow_speed()
        cleaner.clean_by_slow_upload()

        # 检查当前qb下载器的剩余空间
        if self.qb_free_space_size < self._config.brush.min_disk_space:
            free_gb = self.qb_free_space_size / 1024 / 1024 / 1024
            min_gb = self._config.brush.min_disk_space / 1024 / 1024 / 1024
            logger.info(f"qb剩余空间不足，停止刷流，当前剩余空间为:{free_gb:.2f}GiB")
            self.last_stats = {"candidates": 0, "filtered": 0, "added": 0, "reasons": {}}
            self._write_report(
                result="low_disk",
                message=f"下载盘剩余 {free_gb:.1f} GiB，低于下限 {min_gb:.1f} GiB，本轮没有加种",
                disk_free_gb=round(free_gb, 1),
                min_disk_gb=round(min_gb, 1),
                **self._judge_payload(),
            )
            return 0

        # 检查当前刷流任务个数, 同时计算出还需要添加的刷流任务数
        uncompleted_count, waiting_count, slow_count = self._downloading_breakdown()
        if waiting_count or slow_count:
            logger.info(
                f"下载位统计：真正在下载 {uncompleted_count} 个，"
                f"等待中 {waiting_count} 个、持续低速 {slow_count} 个不占下载位"
            )
        if uncompleted_count >= self._config.brush.max_downloading_torrents:
            limit = self._config.brush.max_downloading_torrents
            logger.info(f"qb中刷流任务数已达到配置上限({limit})，停止刷流")
            self.last_stats = {"candidates": 0, "filtered": 0, "added": 0, "reasons": {}}
            self._write_report(
                result="full",
                message=f"下载位已占满（{uncompleted_count}/{limit}），等有种子下完或删种后再加",
                uncompleted=uncompleted_count,
                limit=limit,
                disk_free_gb=round(self.qb_free_space_size / 1024 ** 3, 1),
                **self._judge_payload(),
            )
            return 0
        need_add_count = self._config.brush.max_downloading_torrents - uncompleted_count
        logger.info(f"qb中刷流任务数:{uncompleted_count}, 还需要添加的种子数:{need_add_count}")

        # 从种子库中取出评分较高的种子添加至QB中进行刷流
        torrents = self.get_brush_torrent(need_add_count)
        logger.info(f"从种子库中获取到{len(torrents)}个待添加种子，开始添加到QB中...")
        self.add_brush_torrent(torrents)

        # 真实成功数：加种失败（取链/内容/下载器拒收）的不能算进「成功添加」
        added = int(self.last_stats.get("added") or 0)
        logger.info(f"刷流任务完成，候选 {len(torrents)} 个，实际添加到下载器 {added} 个")
        self._write_report(
            result="completed",
            uncompleted=uncompleted_count,
            limit=self._config.brush.max_downloading_torrents,
            need_add=need_add_count,
            disk_free_gb=round(self.qb_free_space_size / 1024 ** 3, 1),
            **self._judge_payload(),
        )
        return added


# 单独的 VACUUM 任务：原本每分钟在 clean_long_time_no_activate 里执行，
# 改为每 7 天调度一次，避免对 SSD 造成高频整库重写。
def vacuum_database():
    logger.info("开始对数据库进行 VACUUM 瘦身（每 7 天一次）...")
    database.execute_sql('VACUUM;')
    logger.info("VACUUM 完成")


# 站点运行数据：账号快照 + 健康统计 + 图标（对齐 MoviePilot 的站点数据刷新机制）
class SiteDataService:
    def __init__(self):
        self._config = PTBrushConfig()

    def refresh(self, only: Optional[List[str]] = None):
        """
        刷新站点账号数据。

        - 默认刷全部「已启用」的站点；only 里给域名/站点名时只刷这几个
        - 每站的结果都会写进 siteuserdata（快照）与 sitestatistic（成功/失败）
        - 图标只在本地还没有缓存时去抓，站点挂了也继续用旧图标
        """
        from concurrent.futures import ThreadPoolExecutor

        from config.config import site_key
        from ptsite import store as site_store
        from ptsite.profile import clear_cache, fetch_site_icon, fetch_sites_overview

        sites = enabled_sites(self._config.sites or [])
        if only:
            wanted = {str(x) for x in only if str(x).strip()}
            sites = [s for s in sites if site_key(s) in wanted or s.name in wanted]
        if not sites:
            logger.info("没有需要刷新数据的站点，跳过本次站点数据刷新")
            return 0

        logger.info(f"开始刷新 {len(sites)} 个站点的账号数据...")
        clear_cache()
        rows = fetch_sites_overview(sites, ttl=0, force=True)

        # 图标：只补还没缓存过的，避免每次刷新都把 favicon 重抓一遍
        todo_icon = [s for s in sites if not site_store.has_icon(site_key(s))]
        if todo_icon:
            logger.info(f"有 {len(todo_icon)} 个站点还没有图标，开始抓取...")
            with ThreadPoolExecutor(max_workers=min(4, len(todo_icon))) as pool:
                for site, ok in zip(todo_icon, pool.map(fetch_site_icon, todo_icon)):
                    if ok:
                        logger.info(f"站点 {site.name} 图标已缓存")

        ok_count = sum(1 for r in rows if r.get("ok"))
        logger.info(f"站点数据刷新完成：成功 {ok_count} 个，失败 {len(rows) - ok_count} 个")
        for row in rows:
            if not row.get("ok"):
                logger.warning(f"站点 {row.get('name')} 数据抓取失败：{row.get('message')}")
        site_store.cleanup(days=90)
        return len(rows)
