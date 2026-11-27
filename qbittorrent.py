#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
@File    :   qbittorrent.py
@Time    :   2024/11/03 09:18:15
@Author  :   huihuidehui 
@Desc    :   None
"""
from datetime import datetime, timedelta
from pathlib import Path
import re
from collections.abc import Mapping
from typing import List
import qbittorrentapi
from loguru import logger
from pydantic import BaseModel

from db import SeedMeta, Torrent as TorrentDB


def _chunks(items, size: int):
    """SQLite 的 IN 查询参数有上限，长的 hash 列表分批查。"""
    for i in range(0, len(items), size):
        yield items[i:i + size]


class QBitorrentTorrent(BaseModel):
    site: str
    name: str
    torrent_id: str
    completed: bool = False  # 是否下载完成
    free_end_time: datetime
    upspeed: int     # 上传速度 字节
    up_total_size: int
    dl_total_size: int
    dlspeed: int
    hash:str=""
    size:int=0
    # ---- 删种条件要用的实时字段（对齐 MoviePilot 的 torrent_info）----
    progress: float = 0.0        # 下载进度 0~1
    ratio: float = 0.0           # 分享率
    seeding_time: int = 0        # 下载完成后已做种多少秒
    dltime: int = 0              # 加入后经过多少秒
    iatime: int = 0              # 距上次有上传或下载多少秒
    avg_upspeed: int = 0         # 累计平均上传速度 字节/秒
    total_size: int = 0          # 种子总大小（含未选中文件）
    state: str = ""              # qB 的原始状态，如 downloading / stalledDL / queuedDL

class QBittorrentStatus(BaseModel):
    dl_total_size: int
    up_total_size: int
    upspeed: int
    dlspeed: int
    free_space_size:int


def _is_torrents_add_success(response) -> bool:
    if isinstance(response, str):
        return response != "Fails."

    if not isinstance(response, Mapping):
        return False

    failed_values = [
        value for key, value in response.items()
        if "fail" in str(key).lower() or "error" in str(key).lower()
    ]
    return not any(bool(value) for value in failed_values)


class QBittorrent:

    def close(self):
        self.qb.auth_log_out()

    
    
    def __init__(
        self,
        qb_url: str,
        username: str = "",
        password: str = "",
        auth_type: str = "password",
        api_key: str = "",
        save_path: str = "",
        category: str = "ptbrush",
    ):
        self.qb_url = qb_url
        client_kwargs = {"host": qb_url}
        if auth_type == "api_key":
            client_kwargs["api_key"] = api_key
        else:
            client_kwargs["username"] = username
            client_kwargs["password"] = password
        self.qb = qbittorrentapi.Client(**client_kwargs)
        self.qb.auth_log_in()

        # 该下载器的默认保存路径：站点没单独填就用这里，留空则用 qB 默认目录下的 _ptbrush
        self.default_save_path = (save_path or "").strip()

        # 受此项目管理的种子所带有的分类名（配置里可改，留空按默认 ptbrush）
        self.category = (category or "").strip() or "ptbrush"
        self._create_category(self.category)

    @property
    def status(self) -> QBittorrentStatus:
        result = self.qb.sync_maindata().server_state
        
        return QBittorrentStatus(
            dl_total_size=result.alltime_dl,
            up_total_size=result.alltime_ul,
            free_space_size=result.free_space_on_disk,
            upspeed=result.up_info_speed,
            dlspeed=result.dl_info_speed
        )

    @property
    def torrents(self) -> List[QBitorrentTorrent]:
        # return []
        infos = [i for i in self.qb.torrents_info(category=self.category).data if i.get('hash')]
        if not infos:
            return []

        # 主索引：infohash → 持久映射表。加种成功后会把 hash + 站点 + 免费截止时间记进 seedmeta，
        # 种子名保持站点原名，转种到别的站时文件名不会被 __meta 后缀污染。
        rows = {}
        try:
            hashes = [i.get('hash') for i in infos]
            for chunk in _chunks(hashes, 200):
                for row in SeedMeta.select().where(SeedMeta.hash.in_(chunk)):
                    rows[row.hash] = row
        except Exception as e:  # noqa: BLE001
            logger.warning(f"按 hash 读取种子映射失败，改用名字兜底: {e}")

        # 兜底索引：种子名 → 数据库记录。老种子没回写 hash，或者 v2 种子算不出 v1 hash。
        by_name = {}
        try:
            names = [(i.get('name') or '').split('__meta.')[0] for i in infos]
            names = [n for n in names if n]
            for chunk in _chunks(names, 200):
                for row in TorrentDB.select().where(TorrentDB.name.in_(chunk)):
                    by_name.setdefault(row.name, row)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"按名字读取种子记录失败: {e}")

        result = []
        # 老种子（名字里还带 __meta）反向补 hash：解析出站点/ID 后把 qB 的 hash 写回库，
        # 之后它们也走 hash 关联，名字以后怎么变都不影响识别。幂等，重复请求只是重复写同值。
        backfill = []
        now_ts = int(datetime.now().timestamp())
        for i in infos:
            raw_name = i.get('name') or ''
            row = rows.get(i.get('hash') or '')
            if row is not None:
                site = row.site
                torrent_id = row.torrent_id
                end_time = row.free_end_time or (datetime.now() + timedelta(days=365))
                name = row.name
            else:
                # 老数据：名字里还带着 __meta.站点.id.endTime.时间，照旧从名字里还原一次
                meta = re.search(
                    r'__meta\.(.*?)\.(\d+)\.endTime\.(.*)', raw_name)
                if meta:
                    site, torrent_id, end_str = meta.groups()
                    try:
                        end_time = datetime.strptime(end_str, "%Y-%m-%d-%H:%M:%S")
                    except ValueError:
                        end_time = datetime.now() + timedelta(days=365)
                    name = raw_name.split('__meta.')[0]
                    backfill.append((site, str(torrent_id), name, end_time, i.get('hash') or ''))
                else:
                    fallback = by_name.get(raw_name)
                    if fallback is None:
                        # 既没有 hash 记录、名字也不是本软件加的，跳过，别让整张表抛异常
                        continue
                    site = fallback.site
                    torrent_id = fallback.torrent_id
                    end_time = fallback.free_end_time or (datetime.now() + timedelta(days=365))
                    name = fallback.name
                    if not fallback.hash:
                        backfill.append((site, str(torrent_id), name, end_time, i.get('hash') or ''))
            up_total_size = i.get('uploaded') if i.get('uploaded') else 0
            upspeed = i.get('upspeed') if i.get('upspeed') else 0
            dl_total_size = i.get('downloaded') if i.get('downloaded') else 0
            dlspeed = i.get('dlspeed') if i.get('dlspeed') else 0
            torrent_hash = i.get('hash')
            size = i.get('size', 0)

            # 删种条件用的时间戳换算，qB 给的是 epoch 秒，0 表示还没到那个阶段
            added_on = i.get('added_on') or 0
            completion_on = i.get('completion_on') or 0
            last_activity = i.get('last_activity') or 0
            dltime = now_ts - added_on if added_on > 0 else 0
            seeding_time = now_ts - completion_on if completion_on > 0 else 0
            iatime = now_ts - last_activity if last_activity > 0 else 0

            progress = float(i.get('progress') or 0)
            ratio = float(i.get('ratio') or 0)
            # 完成判定用真实进度，比 completion_on 更贴近实际（对齐 MoviePilot）
            completed = progress >= 1 if progress else completion_on > 0
            total_size = i.get('total_size') or size
            state = i.get('state') or ''
            avg_upspeed = int(up_total_size / dltime) if dltime else 0

            result.append(QBitorrentTorrent(
                hash=torrent_hash,
                name=name,
                site=site,
                torrent_id=torrent_id,
                upspeed=upspeed,
                up_total_size=up_total_size,
                dl_total_size=dl_total_size,
                dlspeed=dlspeed,
                free_end_time=end_time,
                completed=completed,
                size=size,
                progress=progress,
                ratio=ratio,
                seeding_time=seeding_time,
                dltime=dltime,
                iatime=iatime,
                avg_upspeed=avg_upspeed,
                total_size=total_size,
                state=state,
            ))
        if backfill:
            try:
                written = 0
                for site_key, tid, sname, end_t, thash in backfill:
                    if not thash:
                        continue
                    SeedMeta.insert(
                        hash=thash,
                        site=site_key,
                        torrent_id=tid,
                        name=sname or "",
                        free_end_time=end_t,
                    ).on_conflict_replace().execute()
                    written += 1
                if written:
                    logger.info(f"已为 {written} 颗种子补记 hash 映射")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"补记种子 hash 映射失败: {e}")
        return result



    def _create_category(self, category):
        """
        分类不存在就创建；已存在则把分类的保存路径对齐到当前配置。
        站点没单独填保存路径时，种子就是按分类路径落盘的，所以这里必须跟配置一致。
        """
        try:
            save_path = self.default_save_path or str(Path(self.qb.app_default_save_path()) / "_ptbrush")
        except Exception as e:
            logger.warning(f"读取 qB 默认保存路径失败，分类路径交给 qB 自己决定：{e}")
            save_path = self.default_save_path
        if not save_path:
            return
        try:
            self.qb.torrents_create_category(name=category, save_path=save_path)
            return
        except qbittorrentapi.exceptions.Conflict409Error:
            # 分类已存在，下面把路径改过来
            pass
        try:
            self.qb.torrents_edit_category(name=category, save_path=save_path)
        except Exception as e:
            logger.warning(f"分类 {category} 的保存路径没能改成 {save_path}：{e}")
        # try:
        #     self.qb.torrents_edit_category(name=category, save_path=save_path)
        # except qbittorrentapi.exceptions.Conflict409Error:
        #     # 路径冲突或不可访问
        #     pass
        # return None

    def download_torrent_url(
        self,
        torrent_content: bytes,
        torrent_name: str,
        save_path: str = "",
    ) -> bool:
        """
        加种。save_path 是站点级保存路径：填了就显式指定这个目录（同时关掉自动分类路径），
        留空则跟随分类路径（= 下载器的默认保存路径）。

        rename 不传：种子名保持 .torrent 里的原名，不再改成 "原名__meta.站点.id.endTime.时间"。
        注意 qB 的 rename 是「新名字」字符串参数（qbittorrentapi 签名 rename: str），
        传 False 会被当成字符串 "False" 直接改名（1.1.8 踩过这个坑），所以这里必须完全省略。
        站点、免费截止时间改为加种后按 infohash 记进数据库，转种到其他站点时文件名不会被污染。
        """
        target = (save_path or "").strip()
        if target:
            res = self.qb.torrents_add(
                torrent_files=torrent_content,
                category=self.category,
                save_path=target,
                use_auto_torrent_management=False,
            )
        else:
            res = self.qb.torrents_add(
                torrent_files=torrent_content,
                category=self.category,
                use_auto_torrent_management=True,
            )
        return _is_torrents_add_success(res)

    def delete_torrent(self, torrent_hash: str) -> bool:
        """删单个种子，走统一入口，保证连数据文件一起删。"""
        return self.delete_torrents([torrent_hash])

    def delete_torrents(self, hashes: List[str]) -> bool:
        """
        批量删除种子，**一定连已下载的数据文件一起删**（delete_files=True）。
        删完向 qB 反查这些 hash 是否真的不在了；还在就再删一次，两次都不行记 error 并返回 False，
        绝不假装成功。
        """
        targets = [h for h in (hashes or []) if h]
        if not targets:
            return True
        all_targets = list(targets)

        contents = self._content_paths(targets)

        for attempt in (1, 2):
            try:
                self.qb.torrents_delete(delete_files=True, torrent_hashes=list(targets))
            except Exception as e:
                logger.warning(f"删除种子失败（第 {attempt} 次）: {e}")
                if attempt == 2:
                    return False
                continue

            left = self._still_present(targets)
            if not left:
                logger.info(
                    f"已删除 {len(targets)} 个种子及其下载文件"
                    + (f"（{', '.join(contents[:5])}{' 等' if len(contents) > 5 else ''}）" if contents else "")
                )
                # 种子没了，对应的 hash 映射也没用了，顺手清掉，别让 seedmeta 无限长大
                try:
                    SeedMeta.delete().where(SeedMeta.hash.in_(all_targets)).execute()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"清理种子 hash 映射失败: {e}")
                return True

            logger.warning(f"删除后仍有 {len(left)} 个种子存在，重试一次: {left[:5]}")
            targets = left

        logger.error(f"种子里仍有 {len(targets)} 个删不掉，请到下载器手动处理: {targets[:5]}")
        return False

    def _content_paths(self, hashes: List[str]) -> List[str]:
        """删除前记下这些种子落盘的位置，方便事后在日志里核对文件有没有一起删掉。"""
        try:
            infos = self.qb.torrents_info(torrent_hashes=list(hashes))
        except Exception as e:
            logger.debug(f"删除前读取种子信息失败（忽略）: {e}")
            return []
        out = []
        for t in (infos or []):
            p = t.get("content_path") if isinstance(t, dict) else None
            if p:
                out.append(p)
        return out

    def _still_present(self, hashes: List[str]) -> List[str]:
        """反查 qB，返回仍然存在的 hash。查询本身失败时按「已删除」处理，不阻塞清理流程。"""
        try:
            infos = self.qb.torrents_info(torrent_hashes=list(hashes))
            present = set()
            for t in (infos or []):
                h = t.get("hash") if isinstance(t, dict) else getattr(t, "hash", None)
                if h:
                    present.add(h.lower())
            return [h for h in hashes if h.lower() in present]
        except Exception as e:
            logger.debug(f"删除后反查种子失败（忽略）: {e}")
            return []

    def reannounce(self, hashes: List[str]) -> bool:
        """
        删除前先重新汇报一次：让站点尽快记下这一轮的做种数据，
        避免刚做够时长/分享率就删、站点那边还没统计到。对齐 MoviePilot 的做法。
        """
        if not hashes:
            return True
        try:
            self.qb.torrents_reannounce(torrent_hashes=list(hashes))
            return True
        except Exception as e:
            logger.warning(f"种子重新汇报失败（不影响删除）: {e}")
            return False
    


        # self.qb.torrents_delete(delete_files=True, torrent_hashes=[torrent_hash])
    
   

    def get_torrent_files(self, hash: str) -> List[dict]:
        """
        检索单个种子的所有文件
        """
        files = self.qb.torrents_files(torrent_hash=hash)
        return files

    def set_no_download_files(self, hash: str, file_ids: List[int]) -> bool:
        """
        设置种子的某个文件不下载
        """
        self.qb.torrents_file_priority(hash, file_ids=file_ids, priority=0)
        return True
