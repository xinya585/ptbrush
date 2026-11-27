"""CookieCloud 同步接口。

浏览器插件把 cookie 传上 CookieCloud 之后，这里按域名匹配到已配置的站点，
把 cookie 直接写进 config.toml，省掉手工复制粘贴。

另外接入了 PT 站点域名清单（见 ptsite/ptlist.py）：预览时能看出这 100 多个
域名里哪些是 PT 站，还能一键把"清单里有、浏览器登录过、但还没配"的 PT 站
全部建成站点。
"""
from flask import Blueprint, jsonify, request

from config.config import PTBrushConfig
from ptsite.cookiecloud import (
    DEFAULT_SERVER,
    cookie_domains,
    cookie_domain_related,
    cookie_string_for_site,
    fetch_cookie_cloud,
    host_of,
    match_domain,
    normalize_domain,
)
from ptsite import ptnames
from ptsite.ptlist import count_pt, get_lists, lookup, skip_reason
from web.auth import login_required
from web.config_io import atomic_write, read_raw
from web.config_serializer import is_masked

api_cookiecloud_bp = Blueprint("api_cookiecloud", __name__)


def _credentials():
    """请求里带了就用请求的，否则回落到配置文件里保存的那份。"""
    body = request.get_json(silent=True) or {}
    saved = PTBrushConfig().cookiecloud
    server = (body.get("server") or "").strip() or (saved.server if saved else "") or DEFAULT_SERVER
    uuid = (body.get("uuid") or "").strip() or (saved.uuid if saved else "")
    password = body.get("password") or ""
    if is_masked(password):
        password = ""
    if not password and saved and saved.password:
        password = saved.password
    return server, uuid, password


def _site_host(site: dict) -> str:
    for key in ("base_url", "rss_url"):
        host = host_of(site.get(key) or "")
        if host:
            return host
    options = site.get("options") or {}
    return host_of(options.get("rss_url") or "")


def _site_matches(site: dict, cookie_data: dict) -> str:
    """返回与该站点匹配的 cookie 域名，没匹配到返回空串。"""
    host = _site_host(site)
    if not host:
        return ""
    best = ""
    for domain in cookie_data.keys():
        # 只算站点自己或它的父域：wiki 这类子站域的 cookie 不能算到主站头上
        if cookie_domain_related(host, domain):
            # 优先挑最具体的域名（.hdhome.org 优于 .org）
            if len(domain) > len(best):
                best = domain
    return best


def _suggest_name(host: str, saved_name: str = "") -> str:
    """站点的显示名：savept.icu 上的中文名优先，其次英文名，最后退回域名。"""
    host = host[4:] if host.startswith("www.") else host
    if saved_name:
        return saved_name
    return host


def _pt_info(host: str, lists: dict, names: dict) -> dict:
    """
    判断一个域名是不是 PT 站，并给出名字与能否自动建站。

    两个来源互补：
    - xinya585/pt-tracker-rules 的 .list：覆盖 250 条，但没有名字
    - savept.icu：覆盖 172 条，有英文名 / 中文名 / 在线状态
    任一命中就算 PT 站；两边都命中时用 list 的分组、savept 的名字。
    """
    hit = lookup(host, lists)
    meta = ptnames.name_for(host, names)
    if not hit and not meta:
        return {"pt": False}

    reason = ""
    if hit:
        reason = skip_reason(host, hit)
    if meta and (meta.get("status") or "") == "offline" and not reason:
        reason = "savept 标记该站已关闭"

    cn = (meta or {}).get("cn") or ""
    en = (meta or {}).get("en") or ""
    return {
        "pt": True,
        "name": cn or en,
        "cn": cn,
        "en": en,
        "source": "both" if (hit and meta) else ("list" if hit else "savept"),
        "group": (hit or {}).get("group", ""),
        "skip": reason,
    }


def _unique_name(base: str, host: str, sites: list) -> str:
    """站点名不能重名：重名时把域名缀在后面。"""
    taken = set()
    for s in sites or []:
        name = s.get("name") if isinstance(s, dict) else getattr(s, "name", None)
        if name:
            taken.add(name)
    if base and base not in taken:
        return base
    host = host[4:] if host.startswith("www.") else host
    return f"{base}({host})" if base else host


@api_cookiecloud_bp.route("/api/cookiecloud/preview", methods=["POST"])
@login_required
def preview():
    server, uuid, password = _credentials()
    try:
        data = fetch_cookie_cloud(server, uuid, password)
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 200

    lists = get_lists()
    names = ptnames.get_names()
    cookie_data = cookie_domains(data)
    sites = read_raw().get("sites") or []
    domains = []
    pt_total = 0
    for domain, cookies in sorted(cookie_data.items()):
        matched = [s.get("name") for s in sites if _site_matches(s, {domain: cookies})]
        info = _pt_info(normalize_domain(domain), lists, names)
        entry = {
            "domain": domain,
            "cookies": len(cookies),
            "matched_sites": matched,
            "pt": bool(info.get("pt")),
        }
        if info.get("pt"):
            pt_total += 1
            entry["pt_name"] = info.get("name", "")
            entry["pt_cn"] = info.get("cn", "")
            entry["pt_en"] = info.get("en", "")
            entry["pt_source"] = info.get("source", "")
            entry["pt_group"] = info.get("group", "")
            # 不带任何站点状态（关站/病危这些注释已在清单解析时丢弃）
            entry["pt_skip"] = info.get("skip", "")
        domains.append(entry)

    return jsonify(
        {
            "ok": True,
            "server": server,
            "domains": domains,
            "total_domains": len(domains),
            "total_cookies": sum(d["cookies"] for d in domains),
            "pt_domains": pt_total,
            "pt_lists": count_pt(lists),
            "pt_names": ptnames.count_names(names),
            "pt_list_errors": (lists.get("errors") or []) + ([names.get("error")] if names.get("error") else []),
        }
    )


@api_cookiecloud_bp.route("/api/cookiecloud/sync", methods=["POST"])
@login_required
def sync():
    server, uuid, password = _credentials()
    try:
        data = fetch_cookie_cloud(server, uuid, password)
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 200

    cookie_data = cookie_domains(data)
    if not cookie_data:
        return jsonify({"ok": False, "message": "CookieCloud 里没有 cookie_data，插件可能还没上传过数据"}), 200

    raw = read_raw()
    sites = raw.get("sites") or []
    if not sites:
        return jsonify({"ok": False, "message": "还没有配置任何站点，先把站点加进来再同步"}), 200

    synced = []
    skipped = []
    for site in sites:
        name = site.get("name") or "(未命名)"
        host = _site_host(site)
        if not host:
            skipped.append({"site": name, "reason": "站点没填 base_url（或 rss_url），没法匹配 cookie"})
            continue
        # 合并这个站点的所有相关 cookie 域：登录态和 CF 通行证经常分家
        value, domains, cookie_count = cookie_string_for_site(host, cookie_data)
        if not value:
            if (site.get("api_key") or "").strip():
                skipped.append({"site": name, "reason": "该站用 API Key，不需要 CookieCloud 的 cookie"})
            else:
                skipped.append({"site": name, "reason": f"CookieCloud 里没有 {host} 可用的 cookie（可能都已过期）"})
            continue
        site["cookie"] = value
        synced.append({"site": name, "host": host, "domains": domains, "cookies": cookie_count})

    if synced:
        raw["sites"] = sites
        atomic_write(raw)

    return jsonify(
        {
            "ok": bool(synced),
            "message": f"已同步 {len(synced)} 个站点" if synced else "没有站点被更新",
            "synced": synced,
            "skipped": skipped,
        }
    )


@api_cookiecloud_bp.route("/api/cookiecloud/add-pt-sites", methods=["POST"])
@login_required
def add_pt_sites():
    """把「域名清单里命中 + CookieCloud 有 cookie + 还没配站点」的 PT 站建成站点。

    默认按 NexusPHP 通用站加（多数国内 PT 站都是），M-Team、tracker/API 域名、
    清单里标了已关站的会跳过并在 skipped 里说明原因。
    """
    body = request.get_json(silent=True) or {}
    server, uuid, password = _credentials()
    try:
        data = fetch_cookie_cloud(server, uuid, password)
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 200

    cookie_data = cookie_domains(data)
    if not cookie_data:
        return jsonify({"ok": False, "message": "CookieCloud 里没有 cookie_data，插件可能还没上传过数据"}), 200

    lists = get_lists(force=bool(body.get("refresh_list")))
    names = ptnames.get_names(force=bool(body.get("refresh_list")))
    raw = read_raw()
    sites = raw.get("sites") or []
    existing_hosts = [h for h in (_site_host(s) for s in sites) if h]

    added = []
    skipped = []
    # 父域先处理（短的先来），子域后加时会被判成"已有站点覆盖"
    for domain in sorted(cookie_data.keys(), key=lambda d: len(normalize_domain(d))):
        host = normalize_domain(domain)
        info = _pt_info(host, lists, names)
        if not info.get("pt"):
            continue
        reason = info.get("skip") or ""
        if reason:
            skipped.append({"domain": host, "reason": reason})
            continue
        if any(match_domain(eh, host) or match_domain(host, eh) for eh in existing_hosts):
            skipped.append({"domain": host, "reason": "已有站点覆盖这个域名，用「同步到站点」更新 cookie 即可"})
            continue
        # 新建站点也合并所有相关 cookie 域，别只拿浏览器分组里最具体的那一个
        value, _domains, _count = cookie_string_for_site(host, cookie_data)
        if not value:
            skipped.append({"domain": host, "reason": "这个域名下没有可用的 cookie（可能全过期了）"})
            continue

        name = _unique_name(_suggest_name(host, info.get("name", "")), host, sites)
        sites.append(
            {
                "name": name,
                "type": "nexusphp",
                "base_url": f"https://{host}",
                "cookie": value,
                "headers": [],
                "options": {},
            }
        )
        existing_hosts.append(host)
        added.append(
            {
                "site": name,
                "domain": domain,
                "cookies": len(cookie_data[domain]),
                "group": info.get("group", "") or info.get("source", ""),
                "name_source": "savept" if info.get("name") else "domain",
            }
        )

    if added:
        raw["sites"] = sites
        atomic_write(raw)

    return jsonify(
        {
            "ok": bool(added),
            "message": f"新增 {len(added)} 个 PT 站点" if added else "没有新的 PT 站可以添加",
            "added": added,
            "skipped": skipped,
            "total_sites": len(sites),
        }
    )
