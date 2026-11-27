import json
import os
from pathlib import Path

import qbittorrentapi
from datetime import datetime

from flask import Blueprint, jsonify, request
from loguru import logger
from pydantic import ValidationError

from config.config import (
    CONFIG_FILE_PATH,
    PROMOTION_META,
    PTBrushConfig,
    SiteModel,
    normalize_target_sites,
    site_key,
)
from ptsite import TorrentFetch, normalize_type, site_type_options
from paths import data_dir
from web.auth import login_required
from web.config_io import atomic_write, merge_with_mask, read_raw
from web.config_schemas import (
    ConfigPayload,
    DownloadersPayload,
    HeaderInput,
    TestDownloaderPayload,
    TestSitePayload,
)
from web.config_serializer import (
    expand_work_time,
    humanize_size,
    mask,
    to_unit_string,
)
from web.runtime import reschedule_brush, reschedule_check

api_config_bp = Blueprint("api_config", __name__)
TEST_DOWNLOADER_TIMEOUT = 5

def _serialize_brush(brush, sites=None) -> dict:
    return {
        "min_disk_space": humanize_size(brush.min_disk_space),
        "torrent_max_size": humanize_size(brush.torrent_max_size),
        "max_downloading_torrents": brush.max_downloading_torrents,
        "download_speed_min": getattr(brush, "download_speed_min", 0) or 0,
        "slow_delete_minutes": getattr(brush, "slow_delete_minutes", 0) or 0,
        "slow_delete_speed": getattr(brush, "slow_delete_speed", 0) or 0,
        "slow_up_delete_enabled": bool(getattr(brush, "slow_up_delete_enabled", False)),
        "slow_up_delete_minutes": getattr(brush, "slow_up_delete_minutes", 0) or 0,
        "slow_up_delete_speed": getattr(brush, "slow_up_delete_speed", 0) or 0,
        "max_no_activate_time": brush.max_no_activate_time,
        "free_end_delete_minutes": getattr(brush, "free_end_delete_minutes", 30),
        "work_time": brush.work_time or "",
        "work_time_hours": expand_work_time(brush.work_time or ""),
        "upload_cycle": brush.upload_cycle,
        "download_cycle": brush.download_cycle,
        "promotions": list(brush.promotions or []),
        "exclude_hr": bool(brush.exclude_hr),
        "min_free_hours": brush.min_free_hours,
        "brush_interval": brush.brush_interval,
        # 回给前端一律是域名键，和下拉框的 value 对齐（存量按站点名存的也能翻译过来）
        "target_sites": normalize_target_sites(brush.target_sites, sites) if sites is not None else list(brush.target_sites or []),
        # 「一个都不参与」开关：留空名单 + True 表示不刷任何站点（旧配置留空仍等于全部参与）
        "target_none": bool(getattr(brush, "target_none", False)),
        # ---- 对齐 MoviePilot 刷流的新增项 ----
        "site_ratio_control": bool(getattr(brush, "site_ratio_control", False)),
        "site_ratio_target": getattr(brush, "site_ratio_target", 2.0),
        "seed_time": getattr(brush, "seed_time", 0),
        "hr_seed_time": getattr(brush, "hr_seed_time", 0),
        "seed_ratio": getattr(brush, "seed_ratio", 0),
        "seed_size": getattr(brush, "seed_size", 0),
        "download_time": getattr(brush, "download_time", 0),
        "seed_avgspeed": getattr(brush, "seed_avgspeed", 0),
        "delete_size_range": getattr(brush, "delete_size_range", "") or "",
        "include": getattr(brush, "include", "") or "",
        "exclude": getattr(brush, "exclude", "") or "",
        "size": getattr(brush, "size", "") or "",
        "seeder": getattr(brush, "seeder", "") or "",
        "pubtime": getattr(brush, "pubtime", "") or "",
        "fail_backoff_minutes": getattr(brush, "fail_backoff_minutes", 1440),
        "qb_category": getattr(brush, "qb_category", "ptbrush") or "ptbrush",
        "check_interval": getattr(brush, "check_interval", 5),
    }


def _serialize_downloader(dl) -> dict:
    if dl is None:
        return {
            "alias": "",
            "url": "",
            "auth_type": "password",
            "username": "",
            "password": "",
            "api_key": "",
            "save_path": "",
        }
    # 内网自用，密码/密钥直接明文回显，方便核对
    return {
        "alias": getattr(dl, "alias", "") or "",
        "url": dl.url,
        "auth_type": dl.auth_type,
        "username": dl.username,
        "password": dl.password or "",
        "api_key": dl.api_key or "",
        # 站点没填保存路径时用这个默认值
        "save_path": getattr(dl, "save_path", "") or "",
    }


def _serialize_downloaders(cfg: PTBrushConfig) -> list:
    """归一化后的下载器列表（老的单个 [downloader] 会变成一条 alias=默认）。"""
    return [_serialize_downloader(dl) for dl in cfg.downloader_list()]


def _serialize_cookiecloud(cc) -> dict:
    if cc is None:
        return {"server": "", "uuid": "", "password": ""}
    return {
        "server": cc.server or "",
        "uuid": cc.uuid or "",
        "password": mask(cc.password),
    }


def _serialize_sites(sites) -> list:
    result = []
    for site in sites or []:
        options = site.options or {}
        result.append(
            {
                # 「参与刷流的站点」下拉的 value 直接用后端算出的稳定键（域名优先），
                # 前端不要自己再推导一遍，否则馒头这类 base_url 带子域名的站会错位
                "key": site_key(site),
                "name": site.name,
                "type": site.type or "",
                "base_url": site.base_url or "",
                "rss_url": options.get("rss_url") or "",
                "cookie": mask(site.cookie or ""),
                "headers": [
                    {"key": h.key, "value": mask(h.value)} for h in (site.headers or [])
                ],
                "priority": getattr(site, "priority", 5),
                "enabled": bool(getattr(site, "enabled", True)),
                # 空串 = 用第一个（主下载器），别名则按名字绑定
                "downloader": getattr(site, "downloader", "") or "",
                "save_path": getattr(site, "save_path", "") or "",
                "api_key": mask(getattr(site, "api_key", "") or ""),
                "user_agent": options.get("user_agent") or "",
                "timeout": options.get("timeout", 15),
            }
        )
    return result


def _payload_to_toml_dict(payload: ConfigPayload) -> dict:
    """把 input schema 转成 toml 写入用的 dict 形态，保留人类可读单位字符串。"""
    b = payload.brush
    sites_dict = [
        {
            "name": s.name,
            "type": s.type or "",
            "base_url": s.base_url or "",
            "cookie": s.cookie,
            "headers": [{"key": h.key, "value": h.value} for h in s.headers],
            "priority": s.priority,
            "enabled": bool(s.enabled),
            "downloader": s.downloader or "",
            "save_path": (s.save_path or "").strip(),
            "api_key": s.api_key,
            "options": {
                k: v
                for k, v in {
                    "rss_url": s.rss_url,
                    "user_agent": s.user_agent,
                    "timeout": s.timeout,
                }.items()
                if v not in (None, "")
            },
        }
        for s in payload.sites
    ]
    # 刷流配置页整份保存时不会带下载器绑定和保存路径：空值不能覆盖站点页里的设置
    for item in sites_dict:
        for key in ("downloader", "save_path"):
            if not item.get(key):
                item.pop(key, None)

    brush_dict = {
        "min_disk_space": to_unit_string(b.min_disk_space.value, b.min_disk_space.unit),
        "torrent_max_size": to_unit_string(b.torrent_max_size.value, b.torrent_max_size.unit),
        "max_downloading_torrents": b.max_downloading_torrents,
        "download_speed_min": float(getattr(b, "download_speed_min", 0) or 0),
        "slow_delete_minutes": int(getattr(b, "slow_delete_minutes", 0) or 0),
        "slow_delete_speed": float(getattr(b, "slow_delete_speed", 0) or 0),
        "slow_up_delete_enabled": bool(getattr(b, "slow_up_delete_enabled", False)),
        "slow_up_delete_minutes": int(getattr(b, "slow_up_delete_minutes", 0) or 0),
        "slow_up_delete_speed": float(getattr(b, "slow_up_delete_speed", 0) or 0),
        "max_no_activate_time": b.max_no_activate_time,
        "free_end_delete_minutes": int(getattr(b, "free_end_delete_minutes", 30) or 0),
        "work_time": b.work_time,
        "upload_cycle": b.upload_cycle,
        "download_cycle": b.download_cycle,
        "promotions": list(b.promotions or []),
        "exclude_hr": bool(b.exclude_hr),
        "min_free_hours": b.min_free_hours,
        "brush_interval": b.brush_interval,
        # 统一存域名：站点改成中文名之后选择项依然有效（同时剔除已删除的站点）
        "target_sites": normalize_target_sites(b.target_sites, sites_dict),
        "target_none": bool(getattr(b, "target_none", False)),
        # ---- 对齐 MoviePilot 刷流的新增项 ----
        "site_ratio_control": bool(b.site_ratio_control),
        "site_ratio_target": b.site_ratio_target,
        "seed_time": b.seed_time,
        "hr_seed_time": b.hr_seed_time,
        "seed_ratio": b.seed_ratio,
        "seed_size": b.seed_size,
        "download_time": b.download_time,
        "seed_avgspeed": b.seed_avgspeed,
        "delete_size_range": (b.delete_size_range or "").strip(),
        "include": (b.include or "").strip(),
        "exclude": (b.exclude or "").strip(),
        "size": (b.size or "").strip(),
        "seeder": (b.seeder or "").strip(),
        "pubtime": (b.pubtime or "").strip(),
        "fail_backoff_minutes": b.fail_backoff_minutes,
        "qb_category": (b.qb_category or "ptbrush").strip() or "ptbrush",
        "check_interval": b.check_interval,
    }
    result = {
        "brush": brush_dict,
        "sites": sites_dict,
    }
    # CookieCloud 凭据归「安全设置」页管；刷流配置页不提交这段，不能顺手写空值把它抹掉
    if payload.cookiecloud is not None:
        result["cookiecloud"] = {
            "server": payload.cookiecloud.server,
            "uuid": payload.cookiecloud.uuid,
            "password": payload.cookiecloud.password,
        }
    # 下载器现在由独立的「下载器配置」页维护；刷流配置页不传这段时保留原值
    if payload.downloader is not None:
        result["downloader"] = {
            "url": payload.downloader.url,
            "auth_type": payload.downloader.auth_type,
            "username": payload.downloader.username,
            "password": payload.downloader.password,
            "api_key": payload.downloader.api_key,
            "save_path": payload.downloader.save_path,
        }
    return result


@api_config_bp.route("/api/config", methods=["GET"])
@login_required
def get_config():
    try:
        config = PTBrushConfig()
        return jsonify(
            {
                "brush": _serialize_brush(config.brush, config.sites),
                "downloader": _serialize_downloader(config.downloader_named()),
                "downloaders": _serialize_downloaders(config),
                "cookiecloud": _serialize_cookiecloud(config.cookiecloud),
                "sites": _serialize_sites(config.sites),
                "supported_sites": [
                    item["value"] for item in site_type_options()
                ],
                "site_types": site_type_options(),
                "promotion_types": [dict(item) for item in PROMOTION_META],
                # 页面拿这个版本号回传保存；不一致说明页面数据过期，拒绝保存
                "rev": config_rev(),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def config_rev() -> str:
    """配置文件的版本指纹：修改时间 + 大小。任何一次保存都会让它变，据此判断页面是不是拿的旧数据。"""
    try:
        st = os.stat(CONFIG_FILE_PATH)
        return f"{st.st_mtime_ns}-{st.st_size}"
    except Exception:
        return ""


@api_config_bp.route("/api/config", methods=["PUT"])
@login_required
def update_config():
    raw_body = request.get_json(silent=True) or {}
    page_rev = str(raw_body.pop("rev", "") or "")
    if page_rev:
        current = config_rev()
        if current and page_rev != current:
            return jsonify({
                "error": "配置在你打开页面之后被改动过（可能是另一个页面、另一个窗口，或站点/下载器保存过）。"
                         "为避免覆盖，这次没有保存。请刷新页面确认最新内容后重试。",
                "stale": True,
                "rev": current,
            }), 409

    try:
        payload = ConfigPayload.model_validate(raw_body)
    except ValidationError as e:
        return jsonify({"error": "请求格式错误", "details": e.errors()}), 400

    try:
        new_dict = _payload_to_toml_dict(payload)
        old_dict = read_raw()
        merged = merge_with_mask(old_dict, new_dict)

        # 二次校验：跑现有 field_validator（如 parse_size / parse_speed / parse_time_ranges）
        try:
            cfg_obj = PTBrushConfig.model_validate(merged)
        except ValidationError as e:
            return jsonify({"error": "配置校验失败", "details": e.errors()}), 400

        # 用「提交上来的原始站点键」校验（normalize 会把不存在的键直接过滤掉，那就查不出来了）：
        # 出现站点列表里没有的键，说明页面拿的是旧列表，直接拒绝而不是静默丢站点
        raw_targets = []
        if payload.brush is not None:
            raw_targets = [str(t).strip() for t in (payload.brush.target_sites or []) if str(t).strip()]
        pool = payload.sites if payload.sites is not None else (cfg_obj.sites or [])
        known = {site_key(x) for x in (pool or [])}
        unknown = [k for k in raw_targets if k not in known]
        if unknown:
            return jsonify({
                "error": "配置里有当前站点列表中不存在的站点：" + "、".join(unknown)
                         + "。请刷新页面重新选择「参与刷流的站点」后保存。",
                "unknown_sites": unknown,
            }), 400

        # 名单变窄却没人动过开关 → 说明页面拿的是旧数据（这次就发生过：只点「填推荐值」保存，馒头被顺手踢掉）
        if payload.brush is not None and not bool(getattr(payload, "targets_touched", False)):
            pool_sites = cfg_obj.sites or []

            def _key_set(raw):
                # 空列表 = 全部站点参与，要展开成实际站点键再比，否则「恢复成全站」会被误判成缩窄
                norm = [str(k) for k in normalize_target_sites(raw, pool_sites)]
                if norm:
                    return set(norm)
                return {site_key(x) for x in pool_sites}

            old_norm = _key_set((old_dict.get("brush") or {}).get("target_sites") or [])
            new_norm = _key_set((merged.get("brush") or {}).get("target_sites") or [])
            removed = sorted(old_norm - new_norm)
            if removed:
                return jsonify({
                    "error": "这次保存会把参与刷流的站点从「{}」缩成「{}」，但你并没有改动站点开关，"
                             "页面数据可能过期。请刷新页面再做一次；确实不想刷这些站，"
                             "请在「参与刷流的站点」里手动关掉它们。".format(
                                 "、".join(sorted(old_norm)) or "全部站点",
                                 "、".join(sorted(new_norm)) or "全部站点",
                             ),
                    "removed_sites": removed,
                }), 400

        # 保存前后的刷流站点名单写进日志，方便回溯（以前这里丢过站点、事后无从查起）
        old_targets = [str(t) for t in ((old_dict.get("brush") or {}).get("target_sites") or [])]
        new_targets = [str(t) for t in ((merged.get("brush") or {}).get("target_sites") or [])]
        if old_targets != new_targets:
            logger.warning(
                "参与刷流站点变更：{} → {}".format(
                    "、".join(old_targets) or "全部站点",
                    "、".join(new_targets) or "全部站点",
                )
            )

        atomic_write(merged)
        # 间隔能即时生效就即时生效，省得为了改个数字重启容器
        applied = reschedule_brush(merged["brush"].get("brush_interval", 15))
        # 检查周期（删种/状态检查）也一起重排
        check_applied = reschedule_check(merged["brush"].get("check_interval", 10))
        brush_minutes = merged["brush"].get("brush_interval", 15)
        check_minutes = merged["brush"].get("check_interval", 10)
        if applied and check_applied:
            message = (
                f"已保存，刷流间隔每 {brush_minutes} 分钟、检查周期每 {check_minutes} 分钟，均即时生效"
            )
        elif applied or check_applied:
            message = "已保存，部分周期已即时生效，其余重启容器后生效"
        else:
            message = "已保存，配置将在下次任务周期自动生效（重启容器后全部生效）"
        return jsonify({
            "ok": True,
            "message": message,
            "brush_interval_applied": applied,
            "check_interval_applied": check_applied,
            "sites_count": len(merged.get("sites") or []),
            "rev": config_rev(),
        })
    except Exception as e:
        return jsonify({"error": f"保存失败：{e}"}), 500


def _resolve_secret(value: str, fallback: str) -> str:
    """前端传 *** 或空 → 用 fallback（旧值）；否则用前端的。"""
    return fallback if value in ("", "***") else value


def _clean_alias(raw: str, idx: int) -> str:
    alias = (raw or "").strip() or f"下载器{idx}"
    return alias[:20]


@api_config_bp.route("/api/downloaders", methods=["GET"])
@login_required
def get_downloaders():
    """下载器列表（支持多个 qBittorrent，每项带别名）。"""
    try:
        cfg = PTBrushConfig()
        return jsonify({"downloaders": _serialize_downloaders(cfg)})
    except Exception as e:
        return jsonify({"error": f"读取下载器配置失败：{e}"}), 500


@api_config_bp.route("/api/downloaders", methods=["PUT"])
@login_required
def update_downloaders():
    """整体保存下载器列表：新增 / 改别名 / 改地址 / 删除，都只动 downloaders 段。"""
    try:
        payload = DownloadersPayload.model_validate(request.get_json(silent=True) or {})
    except ValidationError as e:
        return jsonify({"error": "请求格式错误", "details": e.errors()}), 400

    items: list = []
    aliases: set = set()
    for idx, dl in enumerate(payload.downloaders, 1):
        url = (dl.url or "").strip().rstrip("/")
        if not url:
            return jsonify({"error": f"第 {idx} 个下载器没填地址"}), 400
        if not url.startswith(("http://", "https://")):
            return jsonify({"error": f"第 {idx} 个下载器地址要带 http:// 或 https://"}), 400
        alias = _clean_alias(dl.alias, idx)
        if alias in aliases:
            return jsonify({"error": f"下载器别名「{alias}」重复了，每个下载器要给不同的名字"}), 400
        aliases.add(alias)
        items.append(
            {
                "alias": alias,
                "url": url,
                "auth_type": dl.auth_type,
                "username": dl.username or "",
                "password": dl.password or "",
                "api_key": dl.api_key or "",
                "save_path": (dl.save_path or "").strip(),
            }
        )

    try:
        old = read_raw()
        old_items = [d for d in (old.get("downloaders") or []) if isinstance(d, dict)]
        legacy = old.get("downloader") if isinstance(old.get("downloader"), dict) else None
        pool = old_items + ([legacy] if legacy else [])

        for dl in items:
            # 按别名优先、其次按地址找历史值：留空则沿用原来的密码 / API Key
            prev = next((o for o in pool if (o.get("alias") or "").strip() == dl["alias"]), None)
            if prev is None:
                prev = next(
                    (o for o in pool if (o.get("url") or "").strip().rstrip("/") == dl["url"]), None
                )
            prev = prev or {}
            dl["password"] = _resolve_secret(dl["password"], prev.get("password", "") or "")
            dl["api_key"] = _resolve_secret(dl["api_key"], prev.get("api_key", "") or "")

        merged = dict(old)
        merged["downloaders"] = items
        # 迁移收口：删掉单下载器老段，避免两份真相
        merged.pop("downloader", None)
        PTBrushConfig.model_validate(merged)
        atomic_write(merged)
        logger.info(
            "下载器列表已保存：" + (", ".join(d["alias"] for d in items) if items else "（空）")
        )
        return jsonify(
            {
                "ok": True,
                "message": "下载器配置已保存" if items else "已清空下载器配置",
                "downloaders": _serialize_downloaders(PTBrushConfig()),
            }
        )
    except Exception as e:
        return jsonify({"error": f"保存下载器配置失败：{e}"}), 500


@api_config_bp.route("/api/config/test-downloader", methods=["POST"])
@login_required
def test_downloader():
    try:
        payload = TestDownloaderPayload.model_validate(request.get_json(silent=True) or {})
    except ValidationError as e:
        return jsonify({"error": "请求格式错误", "details": e.errors()}), 400

    cfg = PTBrushConfig()
    # 测试的是哪个下载器：优先按别名取它的历史凭据，取不到就退回主下载器
    prev = cfg.downloader_named(getattr(payload, "alias", "") or "")
    old_pwd = prev.password if prev else ""
    old_api_key = prev.api_key if prev else ""
    password = _resolve_secret(payload.password, old_pwd)
    api_key = _resolve_secret(payload.api_key, old_api_key)

    qb = None
    try:
        client_kwargs = {
            "host": payload.url,
            "REQUESTS_ARGS": {"timeout": TEST_DOWNLOADER_TIMEOUT},
        }
        if payload.auth_type == "api_key":
            client_kwargs["api_key"] = api_key
        else:
            client_kwargs["username"] = payload.username
            client_kwargs["password"] = password
        qb = qbittorrentapi.Client(**client_kwargs)
        qb.auth_log_in()
        return jsonify({"ok": True, "message": "连接成功"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"连接失败：{e}"}), 200
    finally:
        if qb is not None:
            try:
                qb.auth_log_out()
            except Exception:
                pass


@api_config_bp.route("/api/config/health", methods=["GET"])
@login_required
def config_health():
    cfg = PTBrushConfig()
    missing = []
    dls = cfg.downloader_list()
    if not dls:
        missing.append("downloaders")
    else:
        # 任一连接缺关键项就算不健康：多下载器时逐条检查
        for idx, dl in enumerate(dls, 1):
            tag = f"downloaders[{dl.alias or idx}]"
            if not (dl.url or "").strip():
                missing.append(f"{tag}.url")
            if dl.auth_type == "api_key":
                if not dl.api_key:
                    missing.append(f"{tag}.api_key")
            else:
                # username 留空适配 qBittorrent 对可信地址放行登录的场景，不作为必填项
                if not dl.password:
                    missing.append(f"{tag}.password")
    if not cfg.sites:
        missing.append("sites")
    else:
        # 至少一个站点配置完整（按各自的解析器类型判断凭证）
        valid = False
        for s in cfg.sites:
            resolved = normalize_type(s.type) or (
                "mteam" if s.name in TorrentFetch.SITE_SPIDER_MAP
                else ("nexusphp" if s.base_url else ("rss" if (s.options or {}).get("rss_url") else ""))
            )
            if resolved == "mteam":
                valid = valid or any(h.value for h in (s.headers or []))
            elif resolved == "nexusphp":
                valid = valid or bool(s.base_url and (s.cookie or any(h.value for h in (s.headers or []))))
            elif resolved == "rss":
                valid = valid or bool((s.options or {}).get("rss_url"))
        if not valid:
            missing.append("sites[*].credentials")
    return jsonify({"ok": not missing, "missing": missing})


@api_config_bp.route("/api/config/test-site", methods=["POST"])
@login_required
def test_site():
    try:
        payload = TestSitePayload.model_validate(request.get_json(silent=True) or {})
    except ValidationError as e:
        return jsonify({"error": "请求格式错误", "details": e.errors()}), 400

    # 敏感值（cookie/header.value）若为 *** 或空，则从老配置回退
    cfg = PTBrushConfig()
    old_site = None
    for s in cfg.sites or []:
        if s.name == payload.name:
            old_site = s
            break
    old_cookie = old_site.cookie if old_site else ""
    cookie = _resolve_secret(payload.cookie, old_cookie)

    headers = []
    for h in payload.headers:
        old_value = ""
        if old_site:
            for oh in old_site.headers or []:
                if oh.key == h.key:
                    old_value = oh.value
                    break
        headers.append(HeaderInput(key=h.key, value=_resolve_secret(h.value, old_value)))

    base_url = (payload.base_url or (old_site.base_url if old_site else "") or "").strip()
    rss_url = (payload.rss_url or ((old_site.options or {}).get("rss_url") if old_site else "") or "").strip()
    site_type = (payload.type or (old_site.type if old_site else "") or "").strip()
    options = dict(getattr(old_site, "options", None) or {})
    if base_url:
        options["base_url"] = base_url
    if rss_url:
        options["rss_url"] = rss_url

    try:
        site_model = SiteModel(
            name=payload.name,
            type=site_type,
            base_url=base_url,
            cookie=cookie,
            headers=headers,
            options=options,
        )
    except ValidationError as e:
        return jsonify({"ok": False, "message": f"配置不完整：{e.errors()[0]['msg']}"}), 200

    fetcher = None
    try:
        brush = cfg.brush
        fetcher = TorrentFetch(
            site_model,
            promotions=brush.promotions,
            exclude_hr=brush.exclude_hr,
            min_free_hours=brush.min_free_hours,
        )
        count = 0
        hr_count = 0
        promos = {}
        for torrent in fetcher.free_torrents:
            count += 1
            if torrent.hit_and_run:
                hr_count += 1
            promos[torrent.promotion] = promos.get(torrent.promotion, 0) + 1
            if count >= 5:
                break
        detail = f"其中 H&R {hr_count} 个，类型分布 {promos}" if count else ""
        return jsonify({
            "ok": True,
            "message": (
                f"解析器 {fetcher.site_type} 成功拉取 {count} 个种子（演示，仅尝试前 5 条）；"
                f"{detail}"
            ).strip("；"),
        })
    except Exception as e:
        return jsonify({"ok": False, "message": f"拉取失败：{e}"}), 200
    finally:
        if fetcher is not None:
            fetcher.close()


@api_config_bp.route("/api/brush/run", methods=["POST"])
@login_required
def brush_run():
    """手动跑一轮：选种加种 + 删种检查（与定时任务同一套逻辑），返回本轮诊断。"""
    from tasks.services import BrushService, QBTorrentService

    try:
        added = BrushService().brush()
    except Exception as e:
        logger.exception(f"手动刷流失败: {e}")
        return jsonify({"message": f"刷流失败：{e}"}), 500

    result = {"added": int(added or 0)}
    try:
        checker = QBTorrentService()
        checker.clean_will_expired()
        checker.clean_long_time_no_activate()
        result["check"] = checker.check_all()
    except Exception as e:
        logger.exception(f"手动删种检查失败: {e}")
        result["check_error"] = str(e)

    path = data_dir() / "brush_report.json"
    try:
        if path.exists():
            result["report"] = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return jsonify(result)


@api_config_bp.route("/api/brush/idle", methods=["GET"])
@login_required
def brush_idle():
    """列出「空转种」：免费期剩余不足 hours 小时（含已结束）且一点上传都没有的种子。"""
    from datetime import datetime, timedelta

    from tasks.services import QBTorrentService

    hours = float(request.args.get("hours", 1) or 1)
    deadline = datetime.now() + timedelta(hours=hours)
    svc = QBTorrentService()
    hr_keys = set()
    try:
        hr_keys = svc._hit_and_run_keys()
    except Exception:
        pass

    items = []
    for qb in svc._qbs:
        for t in qb.torrents:
            if t.free_end_time and t.free_end_time > deadline:
                continue
            if int(t.up_total_size or 0) > 0:
                continue
            if (t.site, str(t.torrent_id)) in hr_keys:
                continue
            items.append({
                "hash": t.hash,
                "name": t.name,
                "site": t.site,
                "torrent_id": t.torrent_id,
                "free_end_time": t.free_end_time.strftime("%m-%d %H:%M") if t.free_end_time else "",
                "size": int(t.total_size or t.size or 0),
                "progress": round(float(t.progress or 0), 3),
                "upspeed": int(t.upspeed or 0),
                "qb_alias": getattr(qb, "alias", "") or "",
            })
    items.sort(key=lambda x: x["free_end_time"])
    return jsonify({"hours": hours, "count": len(items), "items": items})


@api_config_bp.route("/api/brush/idle/clean", methods=["POST"])
@login_required
def brush_idle_clean():
    """按 hash 删除指定的空转种（种子 + 文件一起删，删前 reannounce 声明）。"""
    from tasks.services import QBTorrentService

    payload = request.get_json(silent=True) or {}
    hashes = [str(h) for h in (payload.get("hashes") or []) if h]
    if not hashes:
        return jsonify({"message": "没有要删除的种子", "deleted": 0}), 400

    svc = QBTorrentService()
    deleted, failed = 0, []
    for qb in svc._qbs:
        matched = [h for h in hashes if h]
        try:
            before = {t.hash: t for t in qb.torrents}
        except Exception:
            before = {}
        hit = [h for h in matched if h in before]
        if not hit:
            continue
        try:
            qb.reannounce(hit)
            qb.delete_torrents(hit)
            deleted += len(hit)
            logger.info(f"手动清理空转种 {len(hit)} 个: {hit}")
            # 手动删的也进「删种记录」，原因写清楚是手动清的、当时什么状态
            for h in hit:
                t = before.get(h)
                if t is None:
                    continue
                parts = []
                try:
                    if t.free_end_time:
                        left = (t.free_end_time - datetime.now()).total_seconds() / 3600
                        parts.append(f"免费剩余 {left:.1f} 小时")
                except Exception:
                    pass
                parts.append(f"已上传 {getattr(t, 'up_total_size', 0) / 1024 ** 3:.2f} GiB")
                parts.append(f"已下载 {getattr(t, 'dl_total_size', 0) / 1024 ** 3:.2f} GiB")
                svc._record_deletion(
                    t, "手动清理空转种（免费快结束且一直没有上传）", rule="手动",
                    detail="，".join(parts),
                )
        except Exception as e:
            logger.exception(f"手动清理空转种失败: {e}")
            failed.extend(hit)

    return jsonify({"deleted": deleted, "failed": failed, "message": f"已删除 {deleted} 个空转种"})


@api_config_bp.route("/api/brush/report", methods=["GET"])
@login_required
def brush_report():
    """最近一轮刷流的运行诊断：候选数、被拒原因分布、实际加种数。"""
    path = data_dir() / "brush_report.json"
    if not path.exists():
        return jsonify({"available": False, "report": None})
    try:
        return jsonify({"available": True, "report": json.loads(path.read_text(encoding="utf-8"))})
    except Exception as e:
        logger.warning(f"读取刷流诊断报告失败: {e}")
        return jsonify({"available": False, "report": None, "error": str(e)})
