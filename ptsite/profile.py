"""站点账号概览：用户名、上传量、下载量、分享率、做种数，以及站点图标。

只给界面展示用，不参与刷流决策。每个站点的请求有缓存（默认 5 分钟），
避免界面每次刷新都去打站点。

抓到的结果会落库（按 domain 键）：每抓一次存一条快照、累加一次健康统计，
favicon 转 base64 存起来，站点临时打不开时卡片照样有图标。
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import requests
import urllib3
from loguru import logger

from config.config import SiteModel, site_domain_of, site_key
from ptsite import store as site_store
from ptsite.throttle import (
    MAX_WAIT,
    SiteRateLimited,
    acquire as acquire_slot,
    defer as defer_site,
    parse_cooldown,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ---- 账号信息解析：同一份数据在各站的模板里长得完全不一样，逐项多模式匹配 ----
# 实测到的几类结构：
#   ① 标准 NexusPHP（家园/我堡/open.cd）：
#      <font class="color_ratio">分享率：</font> 9.610
#      <font class='color_uploaded'>上传量：</font> 99.312 TB
#      <img class="arrowup" alt="Torrents seeding" ... />277
#   ② 纯 label 文本（TTG / 朋友）：
#      分享率 : 8.207  上传量 : 36.150 TB  做种活动 : 251
#   ③ img alt 标签（憨憨）：<img alt="上传">&nbsp;53.453 TB、[分享率]:&nbsp;10.625
#   ④ 紧凑指标 + JSON（观众）：
#      site-userbar__compact-metric--uploaded ... <span>170.522 TB</span>
#      data-uploader-stats="[{&quot;label&quot;:&quot;上传量：&quot;,...
#   ⑤ 站点把用户名塞在 class='XXX_Name' 的链接里，有的站（海豚）只写 "名字 Member"
_SIZE = r"[0-9][0-9.,]*\s*[KMGTPE]?i?B"
_NUM = r"[0-9][0-9,]*"

_HTML_PATTERNS = {
    "uploaded": [
        r"class=[\"']color_uploaded[\"'][^>]*>[^<]*<\s*/\s*font\s*>[^0-9]{0,20}(" + _SIZE + r")",
        r"alt=[\"']上传[\"'][^>]*>(?:&nbsp;|\s)*(" + _SIZE + r")",
        r"compact-metric--uploaded[^>]*>[^0-9]{0,60}(" + _SIZE + r")",
    ],
    "downloaded": [
        r"class=[\"']color_downloaded[\"'][^>]*>[^<]*<\s*/\s*font\s*>[^0-9]{0,20}(" + _SIZE + r")",
        r"alt=[\"']下载[\"'][^>]*>(?:&nbsp;|\s)*(" + _SIZE + r")",
        r"compact-metric--downloaded[^>]*>[^0-9]{0,60}(" + _SIZE + r")",
    ],
    "ratio": [
        # 憨憨这类方括号指标：先锁死 "[分享率]:" 这种明确写法，避免命中公告里的 "分享率大于1.05"
        r"\[分享率\][:：](?:&nbsp;|\s)*([0-9][0-9.,]*)",
        r"class=[\"']color_ratio[\"'][^>]*>[^<]*<\s*/\s*font\s*>[^0-9]{0,20}([0-9][0-9.,]*)",
        r"compact-metric--ratio[^>]*>[^0-9]{0,60}([0-9][0-9.,]*)",
    ],
    "seeding": [
        r"alt=[\"']做种数[\"'][^>]*>(?:&nbsp;|\s)*(" + _NUM + r")",
        r"Torrents seeding[^>]*>(?:&nbsp;|\s)*(" + _NUM + r")",
        r"alt=[\"']做种中[\"'][^>]*>[^0-9]{0,40}(" + _NUM + r")",
    ],
    "leeching": [
        r"alt=[\"']下载数[\"'][^>]*>(?:&nbsp;|\s)*(" + _NUM + r")",
        r"Torrents leeching[^>]*>(?:&nbsp;|\s)*(" + _NUM + r")",
        r"alt=[\"']下载中[\"'][^>]*>[^0-9]{0,40}(" + _NUM + r")",
    ],
}

_TEXT_PATTERNS = {
    "uploaded": [
        r"(?:上传量|上傳量)[^0-9]{0,2}[:：][^0-9]{0,8}(" + _SIZE + r")",
        r"(?:Uploaded)[^0-9]{0,2}[:：][^0-9]{0,8}(" + _SIZE + r")",
    ],
    "downloaded": [
        r"(?:下载量|下載量)[^0-9]{0,2}[:：][^0-9]{0,8}(" + _SIZE + r")",
        r"(?:Downloaded)[^0-9]{0,2}[:：][^0-9]{0,8}(" + _SIZE + r")",
    ],
    "ratio": [
        r"(?:分享率|分享比率)[^0-9]{0,4}[:：][^0-9]{0,4}([0-9][0-9.,]*)",
    ],
    # TTG：做种活动 :&nbsp; &nbsp; 251，中间的空白实体比较长
    "seeding": [
        r"(?:做种活动|做種活動)[^0-9]{0,30}(" + _NUM + r")",
        # 朋友：分享率/上传量 ... | 506 0 可连接
        r"(?:分享率|上传量)[^|]{0,240}\|\s*(" + _NUM + r")\s+" + _NUM,
        # M-Team：當前活動: 1 0（前一个做种数、后一个下载数）
        r"(?:當前活動|当前活动)[^0-9]{0,4}[:：][^0-9]{0,6}(" + _NUM + r")\s+" + _NUM,
    ],
    "leeching": [
        r"(?:下载中|下載中)[^0-9]{0,30}(" + _NUM + r")",
        r"(?:分享率|上传量)[^|]{0,240}\|\s*" + _NUM + r"\s+(" + _NUM + r")",
        r"(?:當前活動|当前活动)[^0-9]{0,4}[:：][^0-9]{0,6}" + _NUM + r"\s+(" + _NUM + r")",
    ],
}

# 观众这类模板把数据放在 JSON 属性里：label / value 成对出现
_PAIR_RE = re.compile(r'"label"\s*:\s*"([^"]{1,20})"\s*,\s*"value"\s*:\s*"([^"]{1,40})"')
# 用户名：绝大多数 NexusPHP 站是 userdetails.php 链接
_RE_USER_LINK = re.compile(
    r'userdetails\.php\?id=[0-9]+["\'&][^>]*>(?:\s|<[^>]+>)*([^<\n]{1,40})', re.I
)
# 也有的站是 class='UltimateUser_Name' / 'Rainbow_Name' 这种，不挂 userdetails
_RE_USER_CLASS = re.compile(
    r'class=["\'][A-Za-z0-9]+_Name["\'][^>]*>(?:\s|<[^>]+>)*([^<\n]{1,40})', re.I
)
# 海豚这类只写 "xinya585 Member"（无链接）
_RE_USER_MEMBER = re.compile(r"\s([A-Za-z0-9_.\-@]{2,24})\s+Member\b")
_RE_LEVEL = re.compile(r"(?:等级|Level|用户等级)[^0-9A-Za-z]{0,6}([0-9A-Za-z.]+)", re.I)


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def _decode_response(resp: requests.Response) -> str:
    """
    按字节判定页面编码。

    TTG 这类站点响应头不带 charset，requests 会退到 ISO-8859-1 解码，
    整页中文变乱码，所有中文关键词都匹配不到 —— 这是之前抓不到账号数据的根因之一。
    """
    raw = resp.content or b""
    ctype = (resp.headers.get("content-type") or "").lower()
    declared = re.search(r"charset=([\w\-]+)", ctype)
    if declared and declared.group(1).lower() not in ("iso-8859-1", "latin-1"):
        try:
            return raw.decode(declared.group(1))
        except (UnicodeDecodeError, LookupError):
            pass
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _first(patterns, text: str) -> str:
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return _clean(m.group(1))
    return ""


def _parse_profile_html(html: str) -> Dict:
    """把页面里的账号信息解析出来；各模板都试一遍，取第一个命中的。"""
    txt = _strip_tags(html)

    # JSON 属性里的 label/value（观众）
    pairs = {}
    for label, value in _PAIR_RE.findall(html.replace("&quot;", '"')):
        pairs.setdefault(label.strip().rstrip("：:").strip(), value.strip())

    out: Dict[str, object] = {}
    for key in ("uploaded", "downloaded", "ratio", "seeding", "leeching"):
        out[key] = _first(_HTML_PATTERNS[key], html) or _first(_TEXT_PATTERNS[key], txt)

    if not out["uploaded"]:
        out["uploaded"] = pairs.get("上传量", "") or pairs.get("上傳量", "")
    if not out["downloaded"]:
        out["downloaded"] = pairs.get("下载量", "") or pairs.get("下載量", "")
    if not out["seeding"]:
        active = pairs.get("活跃", "") or pairs.get("活躍", "")
        m = re.search(r"↑\s*(" + _NUM + r")", active)
        out["seeding"] = m.group(1) if m else ""
    if not out["leeching"]:
        active = pairs.get("活跃", "") or pairs.get("活躍", "")
        m = re.search(r"↓\s*(" + _NUM + r")", active)
        out["leeching"] = m.group(1) if m else ""

    # 分享率兜底：解析值缺失、或明显被站内公告里的 "分享率大于1.05" 之类污染时，用上传/下载算
    up_bytes = site_store.parse_bytes(str(out["uploaded"]))
    down_bytes = site_store.parse_bytes(str(out["downloaded"]))
    if up_bytes and down_bytes:
        calc = up_bytes / max(down_bytes, 1)
        parsed = out["ratio"].replace(",", "")
        try:
            parsed_v = float(parsed) if parsed else 0.0
        except ValueError:
            parsed_v = 0.0
        if not parsed_v or abs(parsed_v - calc) / max(calc, 0.01) > 0.25:
            out["ratio"] = f"{calc:.3f}"

    user = ""
    for pattern in (_RE_USER_LINK, _RE_USER_CLASS):
        m = pattern.search(html)
        if m:
            user = _clean(m.group(1))
            if user:
                break
    if not user:
        m = _RE_USER_MEMBER.search(txt)
        if m:
            user = m.group(1)
    out["username"] = user

    level = _RE_LEVEL.search(txt)
    out["user_level"] = _clean(level.group(1)) if level else ""

    for key in ("seeding", "leeching"):
        digits = re.sub(r"[^\d]", "", str(out[key]))
        out[key] = int(digits) if digits else 0
    return out

_CACHE: Dict[str, Tuple[float, Dict]] = {}
_DEFAULT_TTL = 300

_UNITS = (
    ("PB", 1024 ** 5),
    ("TB", 1024 ** 4),
    ("GB", 1024 ** 3),
    ("MB", 1024 ** 2),
    ("KB", 1024),
)


def human_bytes(value) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    for unit, size in _UNITS:
        if n >= size:
            return f"{n / size:.3f} {unit}"
    return f"{int(n)} B"


def _clean(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _site_base(site: SiteModel) -> str:
    return (site.base_url or "").rstrip("/")


def _domain(site: SiteModel) -> str:
    return site_domain_of(site.base_url or "") or site_key(site)


def _header_value(site: SiteModel, key: str) -> str:
    for h in site.headers or []:
        if (h.key or "").lower() == key.lower():
            return h.value or ""
    return ""


def _new_session(site: SiteModel) -> requests.Session:
    """站点级会话：开了代理才读容器的 HTTP_PROXY，否则强制直连。"""
    sess = requests.Session()
    sess.trust_env = bool(getattr(site, "proxy", False))
    return sess


def _acquire(site: SiteModel) -> None:
    """按站点三元组申请一次请求许可；冷却太长就放弃这一轮。"""
    key = _domain(site)
    while True:
        delay = acquire_slot(
            key,
            getattr(site, "limit_seconds", 0) or 0,
            getattr(site, "limit_interval", 0) or 0,
            getattr(site, "limit_count", 0) or 0,
        )
        if delay <= 0:
            return
        if delay > MAX_WAIT:
            raise SiteRateLimited(f"站点处于限流冷却中，剩余 {int(delay)} 秒")
        time.sleep(delay + 0.03)


def _get(
    site: SiteModel,
    url: str,
    headers: Dict[str, str],
    timeout: int = 15,
    method: str = "GET",
    payload: Optional[Dict] = None,
) -> requests.Response:
    """带流控的请求；429 会把整站推开一段时间。"""
    _acquire(site)
    sess = _new_session(site)
    resp = sess.request(
        method, url, headers=headers, timeout=timeout, allow_redirects=True, verify=False, json=payload
    )
    if resp.status_code == 429:
        seconds = parse_cooldown(_decode_response(resp))
        defer_site(_domain(site), seconds, f"HTTP 429 @ {url}")
        logger.warning(f"[{site.name}] 触发站点限流（429），整站冷却 {int(seconds)} 秒")
    return resp


def _fetch_nexusphp(site: SiteModel, timeout: int = 20) -> Dict:
    base = _site_base(site)
    if not base:
        return {"ok": False, "message": "缺 base_url"}
    resp = _get(
        site,
        f"{base}/index.php",
        headers={
            "Cookie": site.cookie or "",
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        timeout=timeout,
    )
    if resp.status_code == 429:
        return {"ok": False, "message": "站点限流中（429），稍后自动重试"}
    if resp.status_code >= 400:
        return {"ok": False, "message": f"HTTP {resp.status_code}"}
    html = _decode_response(resp)
    final_url = resp.url or ""
    if "login.php" in final_url:
        return {"ok": False, "message": "Cookie 已失效，请重新登录站点后更新 Cookie"}

    info = _parse_profile_html(html)
    if not (info["uploaded"] or info["downloaded"] or info["username"]):
        return {"ok": False, "message": "页面里没找到账号信息（可能 Cookie 失效或站点改版）"}

    return {"ok": True, "message": "", **info}


def _mteam_uid(site: SiteModel) -> str:
    """馒头账号 uid：从 RSS 地址里的 uid= 取，其次读 options.uid。"""
    opts = getattr(site, "options", None) or {}
    uid = str(opts.get("uid") or "").strip()
    if uid.isdigit():
        return uid
    rss = str(opts.get("rss_url") or "")
    m = re.search(r"[?&]uid=(\d+)", rss)
    return m.group(1) if m else ""


def _mteam_profile(site: SiteModel, api_base: str, headers: Dict[str, str], timeout: int) -> Dict:
    """POST /api/member/profile；返回 data 字典（空字典表示失败或没数据）。

    实测（2026-11-15）：带 x-api-key + 表单 uid 请求，返回的 data.memberCount 里
    就有 uploaded / downloaded / shareRate —— 馒头流量是拿得到的，之前只是没读这层。
    不带 uid 也能返回自己账号的数据，但带上更稳妥。表单体与 JSON 体都能过，
    这里统一用表单，跟 genDlToken 那条一致（JSON 头 + 表单体在 M-Team 上会报参数错误）。
    """
    full = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": UA,
    }
    full.update(headers)
    uid = _mteam_uid(site)
    _acquire(site)
    sess = _new_session(site)
    resp = sess.request(
        "POST",
        f"{api_base}/api/member/profile",
        headers=full,
        data={"uid": uid} if uid else {},
        timeout=timeout,
        allow_redirects=True,
        verify=False,
    )
    if resp.status_code == 429:
        seconds = parse_cooldown(_decode_response(resp))
        defer_site(_domain(site), seconds, "HTTP 429 @ /api/member/profile")
        logger.warning(f"[{site.name}] 触发站点限流（429），整站冷却 {int(seconds)} 秒")
        return {}
    if resp.status_code >= 400:
        return {}
    try:
        payload = resp.json()
    except ValueError:
        return {}
    if str(payload.get("code") or "0") not in ("0", ""):
        # 「請求過於頻繁」这类业务错误：不写快照，交给下一轮
        logger.debug(f"[{site.name}] member/profile 返回 code={payload.get('code')}: {payload.get('message')}")
        return {}
    return payload.get("data") or {}


def _mteam_result(data: Dict, note: str = "") -> Dict:
    """把 member/profile 的返回值摊平成账号概览。

    流量在 data.memberCount 里（uploaded/downloaded/shareRate），
    早期版本读的是 data 顶层，所以永远拿不到数 —— 这是馒头一直显示 0 的原因。
    """
    mc = data.get("memberCount") or {}
    up_raw = mc.get("uploaded") if mc.get("uploaded") is not None else data.get("uploaded")
    down_raw = mc.get("downloaded") if mc.get("downloaded") is not None else data.get("downloaded")
    up = human_bytes(up_raw)
    down = human_bytes(down_raw)
    ratio = mc.get("shareRate") if mc.get("shareRate") is not None else data.get("ratio")
    if ratio in (None, ""):
        try:
            ratio = round(float(up_raw or 0) / max(float(down_raw or 1), 1), 3)
        except (TypeError, ValueError):
            ratio = ""
    role = data.get("role")
    return {
        "ok": True,
        "message": note,
        "username": str(data.get("username") or ""),
        "user_level": str(data.get("title") or role or ""),
        "uploaded": up,
        "downloaded": down,
        "ratio": f"{ratio}" if ratio not in (None, "") else "",
        "seeding": int(data.get("seeding") or 0),
        "leeching": int(data.get("leeching") or 0),
    }


def _fetch_mteam(site: SiteModel, timeout: int = 20) -> Dict:
    base = _site_base(site)
    if not base:
        return {"ok": False, "message": "缺 base_url"}
    domain = _domain(site)
    # M-Team 的 API 挂在 api.m-team.io；zp.m-team.io 是网页版镜像，打 API 会 403
    api_base = "https://api.m-team.io" if "m-team" in domain.lower() else base
    cookie = (site.cookie or "").strip()
    key = (
        _header_value(site, "x-api-key")
        or (getattr(site, "api_key", "") or "")
        or (getattr(site, "token", "") or "")
        or ""
    )

    # 1) 有 Cookie 先按网页抓：网页的账号条里带上传/下载量
    if cookie:
        page = _fetch_nexusphp(site, timeout=timeout)
        if page.get("ok") and (page.get("uploaded") or page.get("downloaded")):
            page["message"] = ""
            return page
    # 2) 用 Cookie 调 M-Team 网页版同款接口（网页端就是靠登录态取数的）
    if cookie:
        data = _mteam_profile(site, api_base, {"Cookie": cookie}, timeout)
        if data.get("uploaded") or data.get("downloaded"):
            return _mteam_result(data)
    # 3) 用 API Key 调 member/profile：memberCount 里就是真实流量
    if key:
        data = _mteam_profile(site, api_base, {"x-api-key": key}, timeout)
        if data:
            mc = data.get("memberCount") or {}
            note = ""
            if not (mc.get("uploaded") or mc.get("downloaded") or data.get("uploaded")):
                note = "已读到账号信息，但这个账号的流量字段为空"
            return _mteam_result(data, note)
    if cookie:
        return {"ok": False, "message": "Cookie 已失效或页面里没找到账号信息，请重新登录 M-Team 后更新 Cookie"}
    return {
        "ok": False,
        "message": "M-Team 需要 Cookie 或 API Key：填 Cookie 能读上传/下载量，填 API Key 只能读账号名",
    }


def fetch_site_profile(site: SiteModel, persist: bool = True) -> Dict:
    """抓单个站点的账号概览；失败不抛异常，返回 ok=False 与原因。"""
    started = time.time()
    domain = _domain(site)
    result: Dict = {
        "name": site.name,
        "domain": domain,
        "type": site.type or "",
        "username": "",
        "user_level": "",
        "uploaded": "",
        "downloaded": "",
        "ratio": "",
        "seeding": 0,
        "leeching": 0,
        "ok": False,
        "message": "",
        "seconds": 0,
    }
    stype = (site.type or "").lower()
    try:
        if stype == "mteam":
            data = _fetch_mteam(site)
        elif stype == "rss":
            data = {"ok": False, "message": "RSS 订阅没有账号信息"}
        else:
            data = _fetch_nexusphp(site)
            # 只配了 API Key、没有 Cookie 的站（馒头就是这样配的）走页面抓不到东西，
            # 退回 API 分支再试一次
            if not data.get("ok") and not (site.cookie or "") and (
                getattr(site, "api_key", "") or getattr(site, "token", "")
            ):
                api_data = _fetch_mteam(site)
                if api_data.get("ok"):
                    data = api_data
        result.update(data)
    except SiteRateLimited as e:
        result["ok"] = False
        result["message"] = str(e)[:120]
    except Exception as e:  # 网络/解析异常都只影响这一个站
        logger.warning(f"[{site.name}] 读取站点概览失败：{e}")
        result["ok"] = False
        result["message"] = f"{type(e).__name__}: {e}"[:120]

    result["seconds"] = round(time.time() - started, 2)
    if persist and domain:
        site_store.save_snapshot(site, result, domain=domain)
        site_store.record_statistic(domain, bool(result["ok"]), result["seconds"])
    return result


def fetch_sites_overview(sites: List[SiteModel], ttl: int = _DEFAULT_TTL, force: bool = False) -> List[Dict]:
    """批量抓站点概览，带进程内缓存；多站并发，慢站不拖累其它站。"""
    sites = sites or []
    if not sites:
        return []

    now = time.time()
    results: List[Dict] = []
    todo: List[SiteModel] = []
    for site in sites:
        key = _domain(site)
        cached = _CACHE.get(key)
        if not force and cached and now - cached[0] < ttl:
            results.append(cached[1])
        else:
            todo.append(site)

    if todo:
        fresh: List[Dict] = []
        # 并发别开太大：有的站（皇后）响应慢，一起挤会互相拖成超时
        with ThreadPoolExecutor(max_workers=min(4, len(todo))) as pool:
            for site, data in zip(todo, pool.map(fetch_site_profile, todo)):
                _CACHE[_domain(site)] = (time.time(), data)
                fresh.append(data)
        # 保持配置里的顺序：把缓存命中的与新抓到的按 domain 排回去
        by_domain = {d["domain"]: d for d in results + fresh}
        results = [by_domain[_domain(s)] for s in sites if _domain(s) in by_domain]

    return results


_MAX_ICON_BYTES = 256 * 1024


def _guess_mime(content: bytes, ctype: str = "") -> str:
    """按文件头判断图片类型：不同站点 favicon 有 ico / png / svg 好几种。"""
    head = content[:8]
    if head[:4] == b"\x89PNG":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if head[:3] == b"GIF":
        return "image/gif"
    if head.lstrip()[:1] == b"<":
        return "image/svg+xml"
    if ctype.startswith("image/"):
        return ctype.split(";")[0].strip()
    return "image/x-icon"


def fetch_site_icon(site: SiteModel, timeout: int = 10) -> bool:
    """
    抓站点 favicon 并转 base64 入库。

    只认真正的图片（有些站点的 favicon 返回 404 或 HTML），
    单站失败只记日志，不影响别的站点。
    """
    base = _site_base(site)
    if not base:
        return False
    domain = _domain(site)
    # 已经有图标就不再抓：站点的 favicon.ico 只有 16×16，
    # 会把 pre-dessert-sites 同步进来的 128×128 图标覆盖掉（22px 下明显更糊）
    try:
        from ptsite import store as _icon_store

        if _icon_store.has_icon(domain):
            return True
    except Exception:
        pass
    for path in ("/favicon.ico", "/favicon.png"):
        try:
            resp = _get(site, f"{base}{path}", headers={"User-Agent": UA}, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{site.name}] 抓图标失败 {path}：{e}")
            continue
        if resp.status_code >= 400:
            continue
        content = resp.content or b""
        if not content or len(content) > _MAX_ICON_BYTES:
            continue
        ctype = (resp.headers.get("content-type") or "").lower()
        mime = _guess_mime(content, ctype)
        if site_store.save_icon(domain, site.name, f"{base}{path}", content, mime):
            return True
    logger.debug(f"[{site.name}] 没有抓到可用图标")
    return False




def clear_cache() -> None:
    _CACHE.clear()
