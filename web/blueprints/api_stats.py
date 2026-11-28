import re
import time
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from loguru import logger as log

from db import BrushDelRecord, BrushTorrent, QBStatus, SiteUserData, Torrent, TorrentDayDl
from model import Torrent as TorrentModel
from web.auth import login_required

api_stats_bp = Blueprint("api_stats", __name__)

# 状态采样每 10 分钟一次；超过 15 分钟没新样本就当作已过期，不再把旧数字当成实时状态
STATUS_STALE_AFTER = timedelta(minutes=15)


def _empty_status():
    return {
        "upspeed": 0,
        "dlspeed": 0,
        "up_total": 0,
        "dl_total": 0,
        "free_space": 0,
        "timestamp": "",
    }


@api_stats_bp.route("/api/stats")
@login_required
def get_stats():
    try:
        now = datetime.now()

        try:
            from config.config import PTBrushConfig

            configured = bool(PTBrushConfig().downloader_list())
        except Exception:
            # 配置读不到时不要直接把页面打空，按已配置处理
            configured = True

        # 没有下载器就没有“实时状态”可言。44446 这类沿用了旧数据卷的环境，
        # QBStatus 里可能还留着几天前的最后一条采样，不能拿来当当前状态。
        if not configured:
            return jsonify({
                "configured": False,
                "stale": False,
                "status": _empty_status(),
                "torrents": [],
                "total_active": 0,
            })

        latest_status = QBStatus.select().order_by(QBStatus.created_time.desc()).first()
        stale = bool(
            latest_status
            and latest_status.created_time
            and latest_status.created_time < now - STATUS_STALE_AFTER
        )

        # 分数和展示名从库里补：qB 里的名字带 __meta 后缀，速度/流量只有 qB 是实时准确的
        score_of = {}
        for t in Torrent.select().where(Torrent.brushed == True):
            score_of[(t.site, str(t.torrent_id))] = int(t.score or 0)

        torrents_data = []
        try:
            from tasks.services import QBTorrentService

            svc = QBTorrentService()
            total_active = 0
            for qb in svc._qbs:
                for t in qb.torrents:
                    if int(t.upspeed or 0) > 0 or int(t.dlspeed or 0) > 0:
                        total_active += 1
                    torrents_data.append(
                        {
                            "name": re.sub(r"__meta\..*$", "", t.name or ""),
                            "site": t.site,
                            "upspeed": int(t.upspeed or 0),
                            "dlspeed": int(t.dlspeed or 0),
                            "up_total": int(t.up_total_size or 0),
                            "dl_total": int(t.dl_total_size or 0),
                            "free_end_time": t.free_end_time.strftime("%Y-%m-%d %H:%M:%S") if t.free_end_time else "",
                            "score": score_of.get((t.site, str(t.torrent_id)), 0),
                        }
                    )
            torrents_data.sort(
                key=lambda x: (
                    datetime.strptime(x["free_end_time"], "%Y-%m-%d %H:%M:%S") <= now if x["free_end_time"] else True,
                    x["free_end_time"] or "9999",
                )
            )
            torrents_data = torrents_data[:20]
        except Exception as e:
            # qB 暂时连不上时退回库里的采样，页面不至于空
            log = __import__("loguru").logger
            log.warning(f"实时种子列表获取失败，回退数据库采样: {e}")
            brushed = list(Torrent.select().where(Torrent.brushed == True))
            brushed.sort(key=lambda t: (t.free_end_time <= now, t.free_end_time))
            brushed = brushed[:20]
            need = {t.id for t in brushed}
            latest = {}
            for bt in BrushTorrent.select().order_by(BrushTorrent.created_time.desc()):
                if bt.torrent_id in need and bt.torrent_id not in latest:
                    latest[bt.torrent_id] = bt
                if len(latest) >= len(need):
                    break
            total_active = (
                BrushTorrent.select(BrushTorrent.torrent)
                .where((BrushTorrent.upspeed > 0) | (BrushTorrent.dlspeed) > 0)
                .where(BrushTorrent.created_time >= datetime.now() - timedelta(minutes=5))
                .distinct()
                .count()
            )
            for row in brushed:
                bt = latest.get(row.id)
                torrent_model = TorrentModel.model_validate(row, from_attributes=True)
                torrents_data.append(
                    {
                        "name": torrent_model.name,
                        "site": torrent_model.site,
                        "upspeed": bt.upspeed if bt else 0,
                        "dlspeed": bt.dlspeed if bt else 0,
                        "up_total": bt.up_total_size if bt else 0,
                        "dl_total": bt.dl_total_size if bt else 0,
                        "free_end_time": torrent_model.free_end_time.strftime("%Y-%m-%d %H:%M:%S"),
                        "score": torrent_model.score,
                    }
                )

        return jsonify(
            {
                "configured": True,
                "stale": stale,
                "status": {
                    "upspeed": 0 if stale or not latest_status else latest_status.upspeed,
                    "dlspeed": 0 if stale or not latest_status else latest_status.dlspeed,
                    "up_total": 0 if stale or not latest_status else latest_status.up_total_size,
                    "dl_total": 0 if stale or not latest_status else latest_status.dl_total_size,
                    "free_space": 0 if stale or not latest_status or not hasattr(latest_status, "free_space_size") else latest_status.free_space_size,
                    "timestamp": latest_status.created_time.strftime("%Y-%m-%d %H:%M:%S") if latest_status else "",
                },
                "torrents": torrents_data,
                "total_active": total_active,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_stats_bp.route("/api/brush/sites")
@login_required
def get_brush_sites():
    """正在参与刷流的站点：账号级分享率/上传/下载/做种数/做种量 + 今日增量。

    数据取 siteuserdata 快照（每小时内刷新一次那份），今日增量用「今天最早一条」当基线。
    馒头这类接口不返回流量的站点会返回 ok=False，前端显示「未获取」。
    """
    try:
        from config.config import PTBrushConfig, normalize_target_sites, site_key

        cfg = PTBrushConfig()
        sites = list(cfg.sites or [])
        brush_cfg = cfg.brush or None
        raw_targets = list(getattr(brush_cfg, "target_sites", None) or [])
        targets = [str(k) for k in normalize_target_sites(raw_targets, sites)]
        if bool(getattr(brush_cfg, "target_none", False)):
            picked = []
        elif targets:
            picked = [s for s in sites if site_key(s) in set(targets)]
        else:
            picked = [s for s in sites if getattr(s, "enabled", True)]

        # 「正在执行」还包括 qB 里还挂着刷流种的站点：用户可能刚把某站踢出参与名单，
        # 但它的种子还在做种赚上传，这里也要能看到。
        # 顺带统计每站在 qB 里的做种数/做种量，站点接口不给做种量时用它兜底。
        qb_map = {}
        try:
            from tasks.services import QBTorrentService

            svc = QBTorrentService()
            live_sites = set()
            for qb in svc._qbs:
                for t in qb.torrents:
                    name = str(t.site or "")
                    if not name:
                        continue
                    live_sites.add(name)
                    agg = qb_map.setdefault(name, {"seeding": 0, "seeding_size": 0})
                    if float(t.progress or 0) >= 1.0 and int(t.upspeed or 0) >= 0:
                        agg["seeding"] += 1
                        agg["seeding_size"] += int(t.total_size or 0)
            known = {getattr(x, "name", "") for x in picked}
            for s in sites:
                if getattr(s, "name", "") in live_sites and getattr(s, "name", "") not in known:
                    picked.append(s)
                    known.add(getattr(s, "name", ""))
        except Exception:
            pass

        today = datetime.now().strftime("%Y-%m-%d")
        rows = []
        for site in picked:
            domain = site_key(site)
            snap = (
                SiteUserData.select()
                .where(SiteUserData.domain == domain)
                .order_by(SiteUserData.created_time.desc())
                .first()
            )
            # 最新一条是「刷新失败」或没拿到流量时的空快照（刚重启容器很常见），
            # 直接显示会变成一排「—」。这时退回最近一次成功且有流量的快照，
            # 卡片照常显示旧数，并提示这是哪一次的数据。
            stale_note = ""
            latest = snap
            if latest is not None and (
                not latest.ok
                or not (int(latest.upload or 0) or int(latest.download or 0) or float(latest.ratio or 0))
            ):
                good = (
                    SiteUserData.select()
                    .where(
                        (SiteUserData.domain == domain)
                        & (SiteUserData.ok == True)  # noqa: E712
                        & (SiteUserData.upload > 0)
                    )
                    .order_by(SiteUserData.created_time.desc())
                    .first()
                )
                if good is not None:
                    snap = good
                    if good.updated_day == today:
                        stamp = good.updated_time
                    else:
                        stamp = good.updated_day[5:] + " " + good.updated_time
                    stale_note = "刷新失败，显示 {} 的数据".format(stamp)
            base = (
                SiteUserData.select()
                .where((SiteUserData.domain == domain) & (SiteUserData.updated_day == today))
                .order_by(SiteUserData.created_time.asc())
                .first()
            )
            # 今天只有一条快照时（或今天还没抓），基线用「今天之前最后一条」，避免今日增量恒为 0
            if snap is not None and (base is None or base.id == snap.id):
                base = (
                    SiteUserData.select()
                    .where((SiteUserData.domain == domain) & (SiteUserData.updated_day < today))
                    .order_by(SiteUserData.created_time.desc())
                    .first()
                ) or base
            # 昨天的快照可能是「没拿到流量」的那条（upload=0，比如刚接上 API 的馒头），
            # 这种基线算出来永远是 0；退一步用今天最早一条有流量的快照当基线
            if snap is not None and (base is None or not int(base.upload or 0)):
                alt = (
                    SiteUserData.select()
                    .where(
                        (SiteUserData.domain == domain)
                        & (SiteUserData.updated_day == today)
                        & (SiteUserData.upload > 0)
                    )
                    .order_by(SiteUserData.created_time.asc())
                    .first()
                )
                if alt is not None and alt.id != snap.id:
                    base = alt
            up = int(snap.upload or 0) if snap else 0
            dl = int(snap.download or 0) if snap else 0
            today_up = today_dl = 0
            if snap and base and base.id != snap.id and up and int(base.upload or 0):
                today_up = max(0, up - int(base.upload or 0))
                today_dl = max(0, dl - int(base.download or 0))
            # 站点下载量是「毛量」，免费时段的流量也算了进去（家园实测如此）。
            # 本地每分钟按促销状态分流，把免费那段减掉，剩下的才是真正计费的下载。
            free_dl = 0
            try:
                for rec in TorrentDayDl.select().where(
                    (TorrentDayDl.day == today) & (TorrentDayDl.site == str(site.name))
                ):
                    free_dl += int(rec.free_bytes or 0)
            except Exception:
                free_dl = 0
            today_dl_site = today_dl
            today_dl = max(0, today_dl - free_dl)

            qb_agg = qb_map.get(str(site.name), {})
            seeding = int(snap.seeding or 0) if snap else 0
            seeding_size = int(snap.seeding_size or 0) if snap else 0
            # 站点接口常常不返回做种量（家园就是这样），退回 qB 里这批种子的实际体积
            if not seeding_size:
                seeding_size = int(qb_agg.get("seeding_size") or 0)
            if not seeding:
                seeding = int(qb_agg.get("seeding") or 0)
            # 账号数据三项全零 = 这个站接口不给我们流量（馒头），按「未获取」处理，不要显示成 0
            has_account = bool(snap) and bool(up or dl or float(snap.ratio or 0))
            rows.append(
                {
                    "domain": domain,
                    "name": site.name,
                    "icon": "/api/sites/icon?domain={}".format(domain),
                    "ok": bool(snap and snap.ok and has_account),
                    "ratio": float(snap.ratio or 0) if snap else 0.0,
                    "upload": up,
                    "download": dl,
                    "seeding": seeding,
                    "seeding_size": seeding_size,
                    "today_up": today_up,
                    "today_dl": today_dl,
                    "today_dl_site": today_dl_site,
                    "today_dl_free": free_dl,
                    "updated_at": (snap.updated_day + " " + snap.updated_time) if snap else "",
                    "message": (snap.err_msg or "") if snap else "还没有抓到账号数据",
                    "note": stale_note,
                }
            )
        rows.sort(key=lambda x: (-x["today_up"], -x["upload"]))
        return jsonify(
            {
                "sites": rows,
                "count": len(rows),
                "target_count": len(targets) if targets else len(picked),
                "updated_at": max([r["updated_at"] for r in rows], default=""),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_stats_bp.route("/api/brush/deletions")
@login_required
def get_brush_deletions():
    """最近的删种记录：删了什么、为什么删。仪表盘「删种记录」模块读这个。"""
    try:
        limit = int(request.args.get("limit", 60) or 60)
    except (TypeError, ValueError):
        limit = 60
    limit = max(1, min(limit, 200))
    rows = []
    try:
        for rec in (
            BrushDelRecord.select()
            .order_by(BrushDelRecord.created_time.desc())
            .limit(limit)
        ):
            rows.append({
                "time": rec.created_time.strftime("%m-%d %H:%M:%S") if rec.created_time else "",
                "name": (rec.name or "").split("__meta.")[0],
                "site": rec.site or "",
                "reason": rec.reason or "",
                "rule": rec.rule or "",
                "detail": rec.detail or "",
                "size": int(rec.size or 0),
                "uploaded": int(rec.uploaded or 0),
                "ratio": float(rec.ratio or 0),
                "seeding_hours": round(float(rec.seeding_time or 0) / 3600, 1),
            })
    except Exception as e:  # noqa: BLE001
        log.warning(f"读取删种记录失败: {e}")
    return jsonify({"deletions": rows, "count": len(rows)})
