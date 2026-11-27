"""PT 站点域名清单（xinya585/pt-tracker-rules）。

CookieCloud 里拉下来动辄一百多个域名，绝大多数是普通网站。要靠这两份
Surge/Clash 风格的 `DOMAIN-SUFFIX` 清单判断哪些域名属于 PT 站，
才能把"只配了家园"变成"浏览器登录过的 PT 站都配上"。

清单每行形如：

    DOMAIN-SUFFIX,hdhome.org
    DOMAIN-SUFFIX,hdchina.org # [已关站]

末尾 `#` 后面的状态注释（[已关站] / [病危] / [状态未知]）一律丢弃：
那是维护者的主观标注，会过时（站点复活了清单也未必改）。这里只取域名，
当"这个域名是不是 PT 站"的判断依据，能不能用交给实际请求去验证。

拉不到清单时退回本地缓存（data/ptlist_cache.json），缓存默认 24 小时。
"""
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import urllib3

from paths import data_dir

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LIST_URLS = {
    "domestic": "https://raw.githubusercontent.com/xinya585/pt-tracker-rules/main/pt_domestic.list",
    "foreign": "https://raw.githubusercontent.com/xinya585/pt-tracker-rules/main/pt_foreign.list",
}

DATA_DIR = data_dir()
CACHE_FILE = DATA_DIR / "ptlist_cache.json"
CACHE_TTL = 24 * 3600

# 这些前缀下面挂的是 API / tracker / wiki 域名，不是站点入口，自动建站时跳过
NON_SITE_PREFIXES = ("api.", "mtapi.", "tracker.", "u2.", "share.", "abroad.", "cf.", "na.", "www2.", "wiki.", "docs.")

# M-Team 走官方 API（要 API Key），cookie 对它没用
M_TEAM_MARK = "m-team.cc"

_LISTS: Optional[Dict[str, Any]] = None


def normalize_host(host: str) -> str:
    return (host or "").strip().lstrip(".").lower()


def parse_list(text: str) -> List[Dict[str, str]]:
    """把 .list 文本解析成 [{"domain": ...}]。

    只取域名：行尾 `#` 后面的状态注释直接丢掉，不往系统里带任何站点状态。
    """
    entries = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "," in line:
            kind, _, domain = line.partition(",")
            if kind.strip().upper() not in ("DOMAIN-SUFFIX", "DOMAIN", "DOMAIN-KEYWORD"):
                continue
        else:
            domain = line
        domain = normalize_host(domain)
        if domain:
            entries.append({"domain": domain})
    return entries


def load_lists(force: bool = False, timeout: int = 10) -> Dict[str, Any]:
    """拉两份清单，带 24 小时本地缓存；拉失败时用缓存兜底。"""
    cache = None
    try:
        if CACHE_FILE.exists():
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        cache = None

    if cache and not force and time.time() - float(cache.get("fetched_at") or 0) < CACHE_TTL:
        return cache

    result: Dict[str, Any] = {"fetched_at": time.time(), "domestic": [], "foreign": [], "errors": []}
    for group, url in LIST_URLS.items():
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            result[group] = parse_list(resp.text)
        except Exception as e:
            if cache and cache.get(group):
                result[group] = cache[group]
                result["errors"].append(f"{group} 拉取失败（用缓存）：{e}")
            else:
                result["errors"].append(f"{group} 拉取失败：{e}")

    if result["domestic"] or result["foreign"]:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return result


def get_lists(force: bool = False) -> Dict[str, Any]:
    """进程内缓存，避免每次预览都读文件。"""
    global _LISTS
    if _LISTS is None or force:
        _LISTS = load_lists(force=force)
    return _LISTS


def lookup(host: str, lists: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, str]]:
    """判断 host 是否属于 PT 站，返回 {"domain", "group"}，不属于返回 None。"""
    lists = lists or get_lists()
    h = normalize_host(host)
    if not h:
        return None
    best: Optional[Dict[str, str]] = None
    for group in ("domestic", "foreign"):
        for entry in lists.get(group) or []:
            d = entry["domain"]
            if h == d or h.endswith("." + d):
                if best is None or len(d) > len(best["domain"]):
                    best = {"domain": d, "group": group}
    return best


def skip_reason(host: str, hit: Dict[str, str]) -> str:
    """这个 PT 站域名能不能自动建成站点；能就返回空串，不能返回原因。

    只看域名本身的结构（接口域名 / 需要 API Key 的站），不看清单里的状态注释。
    """
    h = normalize_host(host)
    if M_TEAM_MARK in h:
        return "M-Team 走官方 API（需要 API Key），请在站点卡片里手工添加"
    for prefix in NON_SITE_PREFIXES:
        if h.startswith(prefix):
            return f"{prefix}* 是接口/资源域名，不是站点入口"
    return ""


def count_pt(lists: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    lists = lists or get_lists()
    return {
        "domestic": len(lists.get("domestic") or []),
        "foreign": len(lists.get("foreign") or []),
        "total": len(lists.get("domestic") or []) + len(lists.get("foreign") or []),
    }
