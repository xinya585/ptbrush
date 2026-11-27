"""站点管理接口（对齐 MoviePilot 的 site 模块）。

三层数据分工：
- 配置：config.toml 的 [[sites]]，由 web.site_store 做单站增删改（不再整份覆盖）
- 运行数据：SQLite 里的 siteuserdata / sitestatistic / siteicon 三张表，按 domain 关联
- 流控：ptsite.throttle 的内存状态，按 domain 隔离

路由一览：
  GET    /api/sites                     站点列表（配置 + 最新快照 + 统计 + 图标）
  GET    /api/sites/support             支持的类型与字段说明
  GET    /api/sites/overview            仪表盘用的批量账号概览（兼容老接口）
  GET    /api/sites/icon?domain=        站点图标（base64 解码后直接返回二进制）
  POST   /api/sites                     新增
  PUT    /api/sites                     更新（body 里 old_domain 定位原站点）
  DELETE /api/sites/<key>               删除（key 可以是域名或站点名）
  POST   /api/sites/<key>/refresh       只刷新这一个站点的账号数据与图标
  POST   /api/sites/test                测试未保存的站点配置
"""
import base64
import time

from flask import Blueprint, Response, jsonify, request
from loguru import logger

from config.config import PTBrushConfig, site_domain_of
from ptsite import site_type_options, throttle
from ptsite import store as site_store_db
from ptsite.profile import clear_cache, fetch_site_icon, fetch_site_profile, fetch_sites_overview, human_bytes
from web.auth import login_required
from web.config_io import atomic_write, read_raw

from web.site_store import (
    SiteStoreError,
    build_site,
    delete_site,
    list_sites_raw,
    upsert_site,
)

api_sites_bp = Blueprint("api_sites", __name__)

# 站点编辑框里可改的 options 项：其余开关（pages / request_interval 等）留在 toml 里不暴露
_EDITABLE_OPTIONS = ("rss_url", "user_agent", "timeout", "request_interval", "pages")


def _key_of(site) -> str:
    explicit = str(getattr(site, "domain", "") or "")
    if explicit:
        return explicit
    return site_domain_of(getattr(site, "base_url", "") or "") or str(getattr(site, "name", "") or "")


def _find_site(key: str):
    """按域名或站点名找配置里的站点。"""
    sites = PTBrushConfig().sites or []
    for site in sites:
        if _key_of(site) == key or str(site.name or "") == key:
            return site
    return None


def _status_of(site, snapshot) -> str:
    if not getattr(site, "enabled", True):
        return "disabled"
    if not snapshot:
        return "unknown"
    return "ok" if snapshot.get("ok") else "error"


def _site_payload(site, snapshot=None, statistic=None) -> dict:
    """单站返回给前端的完整视图（配置 + 运行数据）。"""
    domain = _key_of(site)
    options = dict(site.options or {})
    snap = snapshot or {}
    secret_headers = []
    for h in site.headers or []:
        key = getattr(h, "key", "") or (h.get("key") if isinstance(h, dict) else "")
        if not key:
            continue
        secret_headers.append({
            "key": key,
            "value": (getattr(h, "value", "") or (h.get("value") if isinstance(h, dict) else "") or ""),
        })


    return {
        # ---- 配置 ----
        "domain": domain,
        # 稳定键给前端当选择项用（参与刷流的站点、下载器别名联动），
        # 和 /api/config 里的 key 保持同一个值，避免前端从 base_url 推导出 zp.m-team.io
        "key": domain,
        "name": site.name,
        "type": site.type or "",
        "base_url": site.base_url or "",
        "url": site.base_url or options.get("rss_url") or "",
        "rss_url": options.get("rss_url") or "",
        "priority": int(site.priority or 0),
        "enabled": bool(site.enabled),
        "public": bool(getattr(site, "public", False)),
        "proxy": bool(getattr(site, "proxy", False)),
        "filter": getattr(site, "filter", "") or "",
        "note": getattr(site, "note", "") or "",
        "downloader": site.downloader or "",
        "save_path": site.save_path or "",
        "user_agent": options.get("user_agent") or "",
        "timeout": int(options.get("timeout") or 0),
        "request_interval": float(options.get("request_interval") or 0),
        "pages": int(options.get("pages") or 0),
        "limit_seconds": int(getattr(site, "limit_seconds", 0) or 0),
        "limit_interval": int(getattr(site, "limit_interval", 0) or 0),
        "limit_count": int(getattr(site, "limit_count", 0) or 0),
        # 内网自用，凭据直接明文回显，方便核对和复制
        "cookie": site.cookie or "",
        "api_key": site.api_key or "",
        "token": getattr(site, "token", "") or "",
        "headers": secret_headers,
        "has_cookie": bool(site.cookie),
        "has_api_key": bool(site.api_key),
        # ---- 运行数据（来自 SQLite，按 domain 关联）----
        "username": snap.get("username", ""),
        "user_level": snap.get("user_level", ""),
        "uploaded": human_bytes(snap.get("upload")) if snap.get("upload") else "",
        "downloaded": human_bytes(snap.get("download")) if snap.get("download") else "",
        "ratio": ("%.3f" % snap["ratio"]) if snap.get("ratio") else "",
        # 站点接口不给做种数时（馒头 member/profile 就没有）退回 qB 实际做种数
        "seeding": snap.get("seeding") or _qb_seed_stats().get(site.name, {}).get("seeding", 0),
        "leeching": snap.get("leeching", 0),
        "uploaded_bytes": snap.get("upload", 0),
        "downloaded_bytes": snap.get("download", 0),
        "status": _status_of(site, snap),
        "message": snap.get("message", ""),
        "updated_at": snap.get("updated_at", ""),
        "icon": _icon_url(domain),
        "statistic": statistic or {},
        "cooldown": int(throttle.cooldown_left(domain)),
    }


_QB_SEED_CACHE: dict = {"ts": 0.0, "data": {}}


def _qb_seed_stats() -> dict:
    """qB 里按站点聚合的做种数与做种量；30 秒缓存，站点页一次请求只打一遍 qB。"""
    now = time.time()
    if now - float(_QB_SEED_CACHE.get("ts") or 0) < 30:
        return _QB_SEED_CACHE.get("data") or {}
    data: dict = {}
    try:
        from tasks.services import QBTorrentService

        svc = QBTorrentService()
        for qb in svc._qbs:
            for t in qb.torrents:
                name = str(t.site or "")
                if not name:
                    continue
                agg = data.setdefault(name, {"seeding": 0, "seeding_size": 0})
                if float(t.progress or 0) >= 1.0:
                    agg["seeding"] += 1
                    agg["seeding_size"] += int(t.total_size or 0)
    except Exception:
        data = {}
    _QB_SEED_CACHE["ts"] = now
    _QB_SEED_CACHE["data"] = data
    return data


def _icon_url(domain: str) -> str:
    """图标地址带版本号，换图标后不会命中浏览器旧缓存。"""
    ver = site_store_db.icon_version(domain)
    return f"/api/sites/icon?domain={domain}&v={ver}" if ver else f"/api/sites/icon?domain={domain}"


def _all_site_payloads() -> list:
    cfg = PTBrushConfig()
    sites = cfg.sites or []
    snapshots = site_store_db.latest_snapshots()
    payloads = []
    for site in sites:
        domain = _key_of(site)
        statistic = site_store_db.statistic_of(domain)
        payloads.append(_site_payload(site, snapshots.get(domain), statistic))
    # 卡片默认按中文站名升序（优先级设置已下线，排序只在页面上做）
    payloads.sort(key=lambda x: str(x["name"] or ""))
    return payloads


# ----------------------------------------------------------------- 读接口
@api_sites_bp.route("/api/sites", methods=["GET"])
@login_required
def sites_list():
    """站点列表：配置 + 每个站最近一次快照 + 健康统计。"""
    try:
        payloads = _all_site_payloads()
    except Exception as e:
        logger.exception("读取站点列表失败")
        return jsonify({"error": f"读取站点失败：{e}"}), 500
    return jsonify({"sites": payloads, "total": len(payloads)})


@api_sites_bp.route("/api/sites/support", methods=["GET"])
@login_required
def sites_support():
    """支持的解析器类型 + 编辑框字段说明（前端拿来渲染下拉与提示）。"""
    return jsonify(
        {
            "types": site_type_options(),
            "editable_options": list(_EDITABLE_OPTIONS),
            "limit_help": {
                "limit_seconds": "相邻两次请求的最小间隔（秒），0 = 不限",
                "limit_interval": "统计窗口长度（秒），和下面的次数一起生效",
                "limit_count": "窗口内允许的最大请求数，0 = 不限",
            },
        }
    )


@api_sites_bp.route("/api/sites/icon", methods=["GET"])
@login_required
def sites_icon():
    """站点图标：直接返回图片二进制（浏览器按 content-type 渲染）。"""
    domain = (request.args.get("domain") or "").strip()
    record = site_store_db.icon_record(domain)
    if not record:
        return Response(status=404)
    try:
        content = base64.b64decode(record["base64"])
    except Exception:
        return Response(status=404)
    resp = Response(content, mimetype=record.get("mime") or "image/x-icon")
    # 带版本号后不需要长缓存：1 小时够用，站点换图标时 URL 也会变
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@api_sites_bp.route("/api/sites/predessert", methods=["GET"])
@login_required
def predessert_status():
    """GET /api/sites/predessert —— 上一次「站点配置库」更新结果（含适配规则变化）。"""
    try:
        from tasks.predessert import read_report

        report = read_report()
    except Exception as e:  # noqa: BLE001
        return jsonify({"available": False, "error": str(e)}), 500
    return jsonify({"available": bool(report), "report": report})


@api_sites_bp.route("/api/sites/predessert/sync", methods=["POST"])
@login_required
def predessert_sync():
    """POST /api/sites/predessert/sync —— 立即更新一次（平时由计划任务每周一 04:10 跑）。"""
    try:
        from tasks.predessert import update_library

        report = update_library(reason="手动触发")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"手动更新站点配置库失败：{e}")
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": bool(report.get("ok")), "report": report})


@api_sites_bp.route("/api/sites/overview", methods=["GET"])
@login_required
def sites_overview():
    """GET /api/sites/overview?refresh=1
    仪表盘用的批量账号概览；refresh=1 强制忽略缓存重新抓取。
    """
    force = request.args.get("refresh") in ("1", "true", "yes")
    if force:
        clear_cache()
    try:
        cfg = PTBrushConfig()
    except Exception as e:
        return jsonify({"error": f"读取配置失败：{e}"}), 500

    sites = cfg.sites or []
    if not sites:
        return jsonify({"sites": [], "message": "尚未配置任何站点"})

    rows = fetch_sites_overview(sites, force=force)
    snapshots = site_store_db.latest_snapshots()
    for row in rows:
        row["icon"] = _icon_url(row.get("domain", ""))
        row["cooldown"] = int(throttle.cooldown_left(row.get("domain", "")))
        snap = snapshots.get(row.get("domain", "")) or {}
        row["uploaded"] = human_bytes(snap.get("upload")) if snap.get("upload") else row.get("uploaded", "")
        row["downloaded"] = human_bytes(snap.get("download")) if snap.get("download") else row.get("downloaded", "")
    return jsonify({"sites": rows})


# ----------------------------------------------------------------- 写接口
def _bad_request(message: str, status: int = 400):
    return jsonify({"error": message}), status


@api_sites_bp.route("/api/sites", methods=["POST"])
@login_required
def sites_create():
    """新增站点：校验域名 / 站点名唯一后写进 [[sites]]。"""
    payload = request.get_json(silent=True) or {}
    try:
        site = upsert_site(payload)
    except SiteStoreError as e:
        return _bad_request(str(e))
    except Exception as e:
        logger.exception("新增站点失败")
        return _bad_request(f"新增站点失败：{e}", 500)
    logger.info(f"新增站点：{site.get('name')}（{site.get('domain') or site.get('base_url')}）")
    # 新增后顺手抓一次账号数据和图标，卡片不用空着等定时任务
    refreshed = _refresh_one(site.get("domain") or site.get("name"))
    return jsonify({"ok": True, "site": refreshed or site})


@api_sites_bp.route("/api/sites", methods=["PUT"])
@login_required
def sites_update():
    """更新站点：body 里的 old_domain 用来定位原站点（支持改名和换域名）。"""
    payload = request.get_json(silent=True) or {}
    old_key = str(payload.pop("old_domain", "") or payload.get("domain", "") or "")
    if not old_key:
        return _bad_request("缺少 old_domain，无法定位要更新的站点")
    try:
        site = upsert_site(payload, old_domain=old_key)
    except SiteStoreError as e:
        return _bad_request(str(e))
    except Exception as e:
        logger.exception("更新站点失败")
        return _bad_request(f"更新站点失败：{e}", 500)
    logger.info(f"更新站点：{site.get('name')}（{site.get('domain') or site.get('base_url')}）")
    refreshed = _refresh_one(site.get("domain") or site.get("name"))
    return jsonify({"ok": True, "site": refreshed or site})


def _sync_references(domain: str, name: str) -> None:
    """删站点后清掉引用：刷流选择项里如果有它，一并剔除（对齐 MoviePilot 的引用清理）。"""
    raw = read_raw()
    brush = dict(raw.get("brush") or {})
    targets = list(brush.get("target_sites") or [])
    cleaned = [t for t in targets if str(t) not in (domain, name)]
    if cleaned != targets:
        brush["target_sites"] = cleaned
        raw["brush"] = brush
        atomic_write(raw)
        logger.info(f"已从「参与刷流的站点」里移除 {domain or name}（剩 {len(cleaned)} 个）")


@api_sites_bp.route("/api/sites/<path:key>", methods=["DELETE"])
@login_required
def sites_delete(key: str):
    """删除站点（key 可以是域名，也可以是站点名）。"""
    try:
        removed = delete_site(key)
    except SiteStoreError as e:
        return _bad_request(str(e))
    except Exception as e:
        logger.exception("删除站点失败")
        return _bad_request(f"删除站点失败：{e}", 500)
    domain = site_domain_of(removed.get("base_url") or "") or str(removed.get("domain") or "")
    _sync_references(domain, str(removed.get("name") or ""))
    logger.info(f"已删除站点：{removed.get('name')}（{domain or key}）")
    return jsonify({"ok": True, "removed": {"name": removed.get("name"), "domain": domain}})


def _refresh_one(key: str) -> dict:
    """抓单个站点：账号数据 + 图标，返回它在列表里的完整视图。"""
    if not key:
        return {}
    site = _find_site(key)
    if site is None:
        return {}
    domain = _key_of(site)
    clear_cache()
    try:
        fetch_site_profile(site, persist=True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"刷新站点 {site.name} 账号数据失败：{e}")
    try:
        fetch_site_icon(site)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"刷新站点 {site.name} 图标失败：{e}")

    snapshot = site_store_db.latest_snapshots().get(domain)
    statistic = site_store_db.statistic_of(domain)
    return _site_payload(site, snapshot, statistic)


@api_sites_bp.route("/api/sites/<path:key>/refresh", methods=["POST"])
@login_required
def sites_refresh(key: str):
    """只刷新这一个站点，不惊动其它站。"""
    site = _find_site(key)
    if site is None:
        return _bad_request(f"没有找到站点：{key}", 404)
    data = _refresh_one(key)
    if not data:
        return _bad_request("刷新失败", 500)
    logger.info(f"已刷新站点：{data['name']}（{data['status']}｜{data.get('message') or '正常'}）")
    return jsonify({"ok": True, "site": data})


@api_sites_bp.route("/api/sites/test", methods=["POST"])
@login_required
def sites_test():
    """测试未保存的站点配置：只抓一次、不落库，用来验证 Cookie / 地址填对没有。"""
    payload = request.get_json(silent=True) or {}
    old_key = str(payload.pop("old_domain", "") or "")
    base = None
    if old_key:
        for item in list_sites_raw():
            domain = site_domain_of(item.get("base_url") or (item.get("options") or {}).get("rss_url") or "")
            if domain == old_key or str(item.get("name") or "") == old_key:
                base = item
                break
    try:
        site = build_site(payload, base=base)
    except SiteStoreError as e:
        return _bad_request(str(e))
    result = fetch_site_profile(site, persist=False)
    return jsonify({"ok": bool(result.get("ok")), "result": result})


def _raw_domain(item: dict) -> str:
    """从 toml 原始字典里取域名键（和 web.site_store._domain_of 同规则）。"""
    explicit = str(item.get("domain") or "")
    if explicit:
        return site_domain_of(explicit) or explicit
    url = item.get("base_url") or (item.get("options") or {}).get("rss_url") or ""
    return site_domain_of(url) or ""


@api_sites_bp.route("/api/sites/bulk-delete", methods=["POST"])
@login_required
def sites_bulk_delete():
    """批量删除站点。

    mode=all      —— 删掉配置里的全部站点
    mode=invalid  —— 只删「最近一次刷新失败」的站点（status=error）；
                     从没抓取过（unknown）和手动停用的（disabled）不算无效，保留
    站点配置、Cookie / API Key、刷流引用一起清；已下载的种子与历史统计保留。
    """
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in ("all", "invalid"):
        return _bad_request("mode 只能是 all 或 invalid")

    sites = list_sites_raw()
    if not sites:
        return jsonify({"ok": True, "count": 0, "removed": [], "message": "没有站点可删"})

    snapshots = site_store_db.latest_snapshots()

    def status_of(item: dict) -> str:
        if not bool(item.get("enabled", True)):
            return "disabled"
        snap = snapshots.get(_raw_domain(item))
        if not snap:
            return "unknown"
        return "ok" if snap.get("ok") else "error"

    if mode == "all":
        targets = list(sites)
        remaining = []
    else:
        targets = [item for item in sites if status_of(item) == "error"]
        remaining = [item for item in sites if status_of(item) != "error"]

    skipped_unknown = 0
    if mode == "invalid":
        skipped_unknown = sum(1 for item in sites if status_of(item) == "unknown")

    if not targets:
        message = "没有最近一次刷新失败的站点"
        if skipped_unknown:
            message = f"没有刷新失败的站点；另有 {skipped_unknown} 个站点还没抓取过，不计入无效"
        return jsonify({"ok": True, "count": 0, "removed": [], "message": message})

    keys = {_raw_domain(t) for t in targets if _raw_domain(t)}
    names = {str(t.get("name") or "") for t in targets if t.get("name")}

    raw = read_raw()
    raw["sites"] = remaining
    brush = dict(raw.get("brush") or {})
    old_targets = list(brush.get("target_sites") or [])
    new_targets = [t for t in old_targets if str(t) not in keys and str(t) not in names]
    if new_targets != old_targets:
        brush["target_sites"] = new_targets
        raw["brush"] = brush
        logger.info(f"批量删除时顺带从「参与刷流的站点」移除 {len(old_targets) - len(new_targets)} 个")
    atomic_write(raw)

    from ptsite.profile import clear_cache
    from ptsite import throttle

    clear_cache()
    throttle.reset()

    removed = [
        {
            "name": t.get("name"),
            "domain": _raw_domain(t),
            "status": status_of(t),
        }
        for t in targets
    ]
    logger.info(f"批量删除站点（{mode}）：{len(removed)} 个 → {', '.join(str(r['name']) for r in removed)}")
    return jsonify(
        {
            "ok": True,
            "count": len(removed),
            "removed": removed,
            "remaining": len(remaining),
            "skipped_unknown": skipped_unknown,
            "message": f"已删除 {len(removed)} 个站点",
        }
    )

