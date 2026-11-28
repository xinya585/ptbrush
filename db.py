#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   db.py
@Time    :   2024/11/04 09:59:51
@Author  :   huihuidehui 
@Version :   1.0
'''

# here put the import lib
from datetime import datetime
from pathlib import Path
import peewee
from loguru import logger
from paths import data_dir

database = peewee.SqliteDatabase(
    str(data_dir() / 'ptbrush.db'),
    pragmas={
        'journal_mode': 'wal',
        'synchronous': 'normal',
        'cache_size': -32000,
        'mmap_size': 268435456,
        'temp_store': 'memory',
        'wal_autocheckpoint': 10000,
    })


class BaseModel(peewee.Model):
    created_time = peewee.DateTimeField(default=datetime.now)
    updated_time = peewee.DateTimeField(default=datetime.now)

    class Meta:
        database = database


class Torrent(BaseModel):
    name = peewee.CharField()
    site = peewee.CharField(index=True)
    torrent_id = peewee.CharField(index=True)
    leechers = peewee.IntegerField(default=0)
    seeders = peewee.IntegerField(default=0)
    size = peewee.IntegerField(default=0)
    score = peewee.IntegerField(default=0)

    # free结束时间，默认值为当前时间点之后1天
    free_end_time = peewee.DateTimeField()

    # 促销类型：free / free_2x / 2x / half_down，老数据默认按 free 处理
    promotion = peewee.CharField(default="free", index=True)
    # 站点是否给这个种子标了 H&R，老数据默认 False
    hit_and_run = peewee.BooleanField(default=False, index=True)
    # 站点要求的 H&R 做种小时数（彩虹岛这类只写在详情页：H&R: 3day / 5day），0=未知按全局配置
    hit_and_run_hours = peewee.FloatField(default=0)

    brushed = peewee.BooleanField(default=False, index=True)

    class Meta:
        # torrend_id和site联合唯一索引
        indexes = (
            (('torrent_id', 'site'), True),
        )


class BrushTorrent(BaseModel):
    torrent = peewee.ForeignKeyField(Torrent, backref='brushes')
    up_total_size = peewee.BigIntegerField(default=0)  # 上传总大小
    upspeed = peewee.IntegerField(default=0)  # 当前上传速度

    dl_total_size = peewee.BigIntegerField(default=0)  # 下载总大小
    dlspeed = peewee.IntegerField(default=0)  # 当前下载速度


class QBStatus(BaseModel):
    dlspeed = peewee.IntegerField(default=0)    # 当前下载速度
    upspeed = peewee.IntegerField(default=0)    # 当前上传速度

    up_total_size = peewee.BigIntegerField(default=0)  # 上传总大小
    dl_total_size = peewee.BigIntegerField(default=0)  # 下载总大小
    free_space_size = peewee.BigIntegerField(default=0)  # 剩余磁盘空间


class BrushFailRecord(BaseModel):
    """刷流加种失败的记录：用于失败退避，避免同一候选反复消耗站点下载额度。"""

    site = peewee.CharField(index=True)
    torrent_id = peewee.CharField()
    reason = peewee.CharField(default="")

    class Meta:
        database = database
        indexes = (
            (("site", "torrent_id"), True),
        )

    def __str__(self):
        return f"{self.site}.{self.torrent_id}"


# ---------------------------------------------------------------- 站点数据
# 站点配置本身存在 config.toml 的 [[sites]] 里，下面三张表只存"站点侧的运行数据"，
# 全部按 domain（域名键）关联，站点改成中文名不影响任何一条记录。


class SiteUserData(peewee.Model):
    """站点账号快照：每抓一次存一条，按 domain + 日期 + 时间做唯一键。"""

    domain = peewee.CharField(index=True)
    name = peewee.CharField(default="")
    username = peewee.CharField(default="")
    user_level = peewee.CharField(default="")
    # 字节数，抓不到时为 0
    upload = peewee.BigIntegerField(default=0)
    download = peewee.BigIntegerField(default=0)
    ratio = peewee.FloatField(default=0)
    seeding = peewee.IntegerField(default=0)
    leeching = peewee.IntegerField(default=0)
    seeding_size = peewee.BigIntegerField(default=0)
    leeching_size = peewee.BigIntegerField(default=0)
    # 抓取状态与失败原因（成功时为空）
    ok = peewee.BooleanField(default=False)
    err_msg = peewee.CharField(default="")
    updated_day = peewee.CharField(index=True)
    updated_time = peewee.CharField()
    created_time = peewee.DateTimeField(default=datetime.now)

    class Meta:
        database = database
        indexes = (
            (("domain", "updated_day", "updated_time"), True),
        )


class SiteStatistic(peewee.Model):
    """站点健康统计：每站一行累加成功/失败次数与平均耗时。"""

    domain = peewee.CharField(unique=True)
    success = peewee.IntegerField(default=0)
    fail = peewee.IntegerField(default=0)
    seconds = peewee.IntegerField(default=0)
    # 最后一次访问状态：0 成功 / 1 失败
    lst_state = peewee.IntegerField(default=0)
    lst_mod_date = peewee.CharField(default="")
    created_time = peewee.DateTimeField(default=datetime.now)
    updated_time = peewee.DateTimeField(default=datetime.now)

    class Meta:
        database = database


class SiteIcon(peewee.Model):
    """站点图标缓存：favicon 抓一次存 base64，站点挂了也能正常显示。"""

    domain = peewee.CharField(unique=True)
    name = peewee.CharField(default="")
    url = peewee.CharField(default="")
    mime = peewee.CharField(default="image/x-icon")
    base64 = peewee.TextField(default="")
    created_time = peewee.DateTimeField(default=datetime.now)
    updated_time = peewee.DateTimeField(default=datetime.now)

    class Meta:
        database = database


def migrate_database():
    """执行数据库迁移，添加缺少的字段"""
    try:
        # 创建表（如果不存在）
        database.create_tables([
            Torrent, BrushTorrent, QBStatus,
            SiteUserData, SiteStatistic, SiteIcon,
            # 刷流加种失败退避表（对齐 MoviePilot 的失败候选次日重试）
            BrushFailRecord,
            # 每颗种子的当日下载流水（今日下载剔免费用）
            TorrentDayDl,
            # 删种记录（仪表盘「删种记录」模块）
            BrushDelRecord,
            # 种子 hash → 站点/免费时间的持久映射（替代把信息拼进种子名）
            SeedMeta,
        ])

        # 策略页与规则引擎已移除：清掉历史规则 / 任务 / 审计表
        try:
            names = {row[0] for row in database.execute_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            for table in ("rule", "task", "actionlog"):
                if table in names:
                    database.execute_sql(f'DROP TABLE IF EXISTS "{table}"')
                    logger.info(f"策略历史表已清理: {table}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"策略历史表清理失败: {e}")
        
        # torrentdaydl 结构升级：老版本没有 free_bytes / paid_bytes / is_free，重建即可
        try:
            cols = [c[1] for c in database.execute_sql("PRAGMA table_info(torrentdaydl)").fetchall()]
            if cols and "free_bytes" not in cols:
                database.execute_sql("DROP TABLE torrentdaydl")
                database.create_tables([TorrentDayDl])
                logger.info("torrentdaydl 结构升级完成（旧流水已清空重建）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"torrentdaydl 结构检查失败: {e}")

        # 检查QBStatus表是否有free_space_size字段
        cursor = database.execute_sql("PRAGMA table_info(qbstatus)")
        columns = [column[1] for column in cursor.fetchall()]
        
        # 如果没有free_space_size字段，添加它
        if 'free_space_size' not in columns:
            logger.info("正在升级数据库：添加 free_space_size 字段到 QBStatus 表")
            with database.atomic():
                database.execute_sql("ALTER TABLE qbstatus ADD COLUMN free_space_size BIGINT DEFAULT 0")
            logger.info("数据库升级完成")

        # 种子表：促销类型 + H&R 标记（1.2.0 新增）
        cursor = database.execute_sql("PRAGMA table_info(torrent)")
        torrent_columns = [column[1] for column in cursor.fetchall()]
        if 'promotion' not in torrent_columns:
            logger.info("正在升级数据库：添加 promotion 字段到 Torrent 表")
            with database.atomic():
                database.execute_sql("ALTER TABLE torrent ADD COLUMN promotion VARCHAR(32) DEFAULT 'free'")
            database.execute_sql("CREATE INDEX IF NOT EXISTS torrent_promotion ON torrent(promotion)")
        if 'hit_and_run' not in torrent_columns:
            logger.info("正在升级数据库：添加 hit_and_run 字段到 Torrent 表")
            with database.atomic():
                database.execute_sql("ALTER TABLE torrent ADD COLUMN hit_and_run INTEGER DEFAULT 0")
            database.execute_sql("CREATE INDEX IF NOT EXISTS torrent_hit_and_run ON torrent(hit_and_run)")
        if 'hit_and_run_hours' not in torrent_columns:
            logger.info("正在升级数据库：添加 hit_and_run_hours 字段到 Torrent 表")
            with database.atomic():
                database.execute_sql("ALTER TABLE torrent ADD COLUMN hit_and_run_hours REAL DEFAULT 0")
    except Exception as e:
        logger.error(f"数据库迁移失败: {str(e)}")
        raise

# 执行数据库迁移
# migrate_database()

class TorrentDayDl(BaseModel):
    """每颗种子的当日下载流水：用来把站点「毛下载量」里的免费时段剔出去。

    站点侧的下载量把免费流量也计进去了（家园实测如此），所以要靠本地的每分钟采样，
    按当时的促销状态把流量分到 free_bytes / paid_bytes 两个桶里。
    """

    hash = peewee.CharField(index=True)
    day = peewee.CharField(index=True)
    site = peewee.CharField(index=True, default="")
    base_size = peewee.BigIntegerField(default=0)
    last_size = peewee.BigIntegerField(default=0)
    free_bytes = peewee.BigIntegerField(default=0)
    paid_bytes = peewee.BigIntegerField(default=0)
    is_free = peewee.BooleanField(default=False)

    class Meta:
        database = database
        indexes = (
            (("hash", "day"), True),
        )

class BrushDelRecord(BaseModel):
    """一次删种：删了什么、为什么删。仪表盘「删种记录」直接读这张表。"""

    hash = peewee.CharField(index=True, default="")
    name = peewee.TextField(default="")
    site = peewee.CharField(index=True, default="")
    torrent_id = peewee.CharField(default="")
    size = peewee.BigIntegerField(default=0)
    uploaded = peewee.BigIntegerField(default=0)
    ratio = peewee.FloatField(default=0)
    seeding_time = peewee.BigIntegerField(default=0)
    reason = peewee.CharField(default="")          # 一句话原因，界面显示用
    rule = peewee.CharField(default="")            # 规则来源：过期/无活动/促销失效/条件/体积/手动
    detail = peewee.TextField(default="")          # 具体数值


class SeedMeta(BaseModel):
    """qB 种子 ↔ 站点 / 免费截止时间的持久映射，按 infohash 关联。

    加种成功后写一行；读种子时按 hash 查它，站点和免费时间就不再依赖种子名。
    Torrent 表只是候选池（每轮抓取会变），不能拿来长期对账，所以单独一张只增不改的表。
    """

    hash = peewee.CharField(unique=True, index=True)
    site = peewee.CharField(index=True)
    torrent_id = peewee.CharField(default="")
    name = peewee.CharField(default="")
    free_end_time = peewee.DateTimeField()

    class Meta:
        database = database


