"""PT 站点中文名（savept.icu 的「PT监护室」）。

CookieCloud 交叉对比出来的站点，名字默认是域名（hdsky.me、pterclub.net 这种），
用起来不直观，用户往往事后一个个改成中文名。但站点名在刷流工具里是有代价的：
qB 里的种子名带着 `__meta.<站点名>.<id>`，改名之后历史种子和站点就对不上了。

所以更好的做法是**建站时就取到中文名**。savept.icu 的站点卡片里同时有域名和
中文名（`HDHome - 家园`、`PterClub - 猫站`），正好拿来当建站名字。

页面上每个站点是一段：

    <a href="https://hdhome.org/" ... data-status="online" data-search="hdhome家园" ...>
      ...
      <div class="card-title">
          HDHome - 家园
      </div>

拉不到页面时退回本地缓存（data/ptnames_cache.json），缓存默认 24 小时；
缓存也没有就让调用方回退到域名做名字，不影响建站本身。
"""
import json
import re
import time
from html import unescape
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from paths import data_dir
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

NAMES_URL = "https://savept.icu/"
DATA_DIR = data_dir()
CACHE_FILE = DATA_DIR / "ptnames_cache.json"
CACHE_TTL = 24 * 3600

_ANCHOR_RE = re.compile(r'<a\s+href="(https?://[^"]+)"(.*?)</a>', re.S)
_TITLE_RE = re.compile(r'<div class="card-title">\s*(.*?)\s*</div>', re.S)
_STATUS_RE = re.compile(r'data-status="([^"]*)"')
_TAG_RE = re.compile(r"<[^>]+>")

_NAMES: Optional[Dict[str, Any]] = None


def normalize_host(host: str) -> str:
    return (host or "").strip().lstrip(".").lower()


def host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse

        h = urlparse(url if "://" in url else "https://" + url).netloc.lower()
        h = h.split("@")[-1].split(":")[0]
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _clean(text: str) -> str:
    return unescape(_TAG_RE.sub("", text or "")).strip()


def parse_names(html: str) -> Dict[str, Dict[str, str]]:
    """
    把页面 HTML 解析成 {"hdhome.org": {"en": "HDHome", "cn": "家园", "title": "HDHome - 家园", "status": "online"}}。

    card-title 形如「HDHome - 家园」；有的站只有英文名（PTFans），此时 cn 为空。
    """
    result: Dict[str, Dict[str, str]] = {}
    for href, rest in _ANCHOR_RE.findall(html or ""):
        host = host_of(href)
        if not host:
            continue
        title_match = _TITLE_RE.search(rest)
        if not title_match:
            continue
        title = _clean(title_match.group(1))
        if not title:
            continue
        status_match = _STATUS_RE.search(rest)
        status = (status_match.group(1) or "").strip() if status_match else ""

        en, cn = "", ""
        if " - " in title:
            en, _, cn = title.partition(" - ")
            en, cn = en.strip(), cn.strip()
        else:
            en = title

        # 同一个域名可能出现多次（不同分组），保留信息更全的那条
        old = result.get(host)
        if old and old.get("cn") and not cn:
            continue
        result[host] = {"en": en, "cn": cn, "title": title, "status": status}
    return result


def load_names(force: bool = False, timeout: int = 15) -> Dict[str, Any]:
    """拉 savept.icu 的站点名，带 24 小时本地缓存；失败时用缓存兜底。"""
    cache = None
    try:
        if CACHE_FILE.exists():
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        cache = None

    if cache and not force and time.time() - float(cache.get("fetched_at") or 0) < CACHE_TTL:
        return cache

    result: Dict[str, Any] = {"fetched_at": time.time(), "names": {}, "error": ""}
    try:
        resp = requests.get(
            NAMES_URL,
            timeout=timeout,
            verify=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ptbrush)"},
        )
        resp.raise_for_status()
        names = parse_names(resp.text)
        if not names:
            raise ValueError("页面里没解析出站点名（结构可能变了）")
        result["names"] = names
    except Exception as e:
        if cache and cache.get("names"):
            result["names"] = cache["names"]
            result["error"] = f"拉取失败（用缓存）：{e}"
        else:
            result["names"] = {}
            result["error"] = f"拉取失败：{e}"

    if result["names"]:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return result


def get_names(force: bool = False) -> Dict[str, Any]:
    """进程内缓存，避免每次预览都读文件。"""
    global _NAMES
    if _NAMES is None or force:
        _NAMES = load_names(force=force)
    return _NAMES


def name_for(host: str, names: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, str]]:
    """按域名取站点名，返回 {"en", "cn", "title", "status"}；没匹配到返回 None。

    host 可能是 `pt.keepfrds.com` 这种子域，先精确匹配，再退到父域匹配。
    """
    names = names or get_names()
    table = names.get("names") or {}
    h = normalize_host(host)
    if not h or not table:
        return None
    if h in table:
        return table[h]
    # 子域退到父域：pt.keepfrds.com -> keepfrds.com
    parts = h.split(".")
    for i in range(1, len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in table:
            return table[cand]
    return None


def count_names(names: Optional[Dict[str, Any]] = None) -> int:
    names = names or get_names()
    return len(names.get("names") or {})
