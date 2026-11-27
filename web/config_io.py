"""
配置 toml 文件读写：原子写、进程内锁、敏感字段 mask 合并。

配置备份能力已下线：保存时不再往 backups/ 落快照，也不再写 config.toml.bak。
写盘仍走 `.tmp` + `os.replace()` 的原子替换，中途失败不会留下半份配置。
"""
import os
import threading
from pathlib import Path
from typing import Any, Dict

import tomli_w

try:
    import tomllib  # py3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from config.config import CONFIG_FILE_PATH, site_domain_of
from web.config_serializer import is_masked

_LOCK = threading.Lock()
_TMP_PATH = Path(str(CONFIG_FILE_PATH) + ".tmp")


def read_raw() -> Dict[str, Any]:
    """以原始 dict 形态读 toml；不存在时返回空 dict（不抛错）。"""
    if not Path(CONFIG_FILE_PATH).exists():
        return {}
    with open(CONFIG_FILE_PATH, "rb") as f:
        return tomllib.load(f)


def atomic_write(data: Dict[str, Any]) -> None:
    """
    把整份配置写回 toml：
    1. tomli_w.dump 到 .tmp
    2. os.replace(.tmp, config.toml) 原子替换
    全程持锁。不再留任何备份副本（快照与 .bak 都已下线）。
    """
    with _LOCK:
        with open(_TMP_PATH, "wb") as f:
            tomli_w.dump(data, f)
        os.replace(_TMP_PATH, CONFIG_FILE_PATH)


def merge_with_mask(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """
    把前端提交的 new 合并到 old：
    - downloader.password、downloader.api_key、sites[i].cookie、sites[i].headers[j].value
      这些敏感字段
      若提交值是 "" 或 "***"，保留 old 中的值
    - 其他字段直接用 new 的
    - 返回新的 dict，old 不会被改
    """
    # 以老配置为底再叠加新值：这样 config.toml 里本文不认识的段
    # （如 [cookiecloud]、以后再加的段）不会被一次保存动作抹掉
    merged: Dict[str, Any] = dict(old)
    merged.update(new)
    old_downloader = old.get("downloader", {}) or {}
    old_sites = old.get("sites", []) or []

    # downloader.password / downloader.api_key（老的单个 [downloader] 段）
    dl = dict(merged.get("downloader") or {})
    if is_masked(dl.get("password", "")):
        dl["password"] = old_downloader.get("password", "")
    if is_masked(dl.get("api_key", "")):
        dl["api_key"] = old_downloader.get("api_key", "")
    merged["downloader"] = dl

    # 多下载器 [[downloaders]]：与老单条段同时存在时以列表为准，
    # 敏感字段按别名沿用旧值，避免整份保存时把别家的密码清空
    old_list = [d for d in (old.get("downloaders") or []) if isinstance(d, dict)]
    new_list = merged.get("downloaders")
    if isinstance(new_list, list):
        fixed = []
        for d in new_list:
            d = dict(d)
            prev = {}
            for o in old_list:
                if (o.get("alias") or "").strip() == (d.get("alias") or "").strip():
                    prev = o
                    break
            if is_masked(d.get("password", "")):
                d["password"] = prev.get("password", "") or ""
            if is_masked(d.get("api_key", "")):
                d["api_key"] = prev.get("api_key", "") or ""
            fixed.append(d)
        merged["downloaders"] = fixed
        if fixed:
            merged.pop("downloader", None)

    # sites[i].cookie 和 headers[j].value
    new_sites = list(merged.get("sites") or [])
    merged_sites = []
    for i, site in enumerate(new_sites):
        site = dict(site)
        # 按域名键匹配老站点（站点名可以改，域名键更稳）；匹配不到再按位置 / 站点名兜底
        old_site = {}
        domain = site_domain_of(
            site.get("domain") or site.get("base_url") or (site.get("options") or {}).get("rss_url") or ""
        )
        if domain:
            for cand in old_sites:
                cand_domain = site_domain_of(
                    cand.get("domain")
                    or cand.get("base_url")
                    or (cand.get("options") or {}).get("rss_url")
                    or ""
                )
                if cand_domain and cand_domain == domain:
                    old_site = cand
                    break
        if not old_site and i < len(old_sites) and old_sites[i].get("name") == site.get("name"):
            old_site = old_sites[i]
        if not old_site:
            for cand in old_sites:
                if cand.get("name") == site.get("name"):
                    old_site = cand
                    break

        # 以老配置为底再叠加新值：整份保存时前端只带原来那几个字段，
        # domain / limit_seconds / limit_interval / limit_count / proxy / filter / note / public
        # 这些新增字段不能被一次保存抹掉
        merged_site = {k: v for k, v in old_site.items() if v is not None}
        merged_site.update({k: v for k, v in site.items() if v is not None})
        site = merged_site

        if is_masked(site.get("cookie", "")):
            site["cookie"] = old_site.get("cookie", "")
        # 站点级 API Key 同属敏感字段
        if is_masked(site.get("api_key", "") or ""):
            site["api_key"] = old_site.get("api_key", "")

        # 适配器参数：UI 只改 rss_url 这类少数字段，手工写进 toml 的 pages/request_interval
        # 等不能被保存动作抹掉，所以以老配置为底再叠加新值
        old_options = dict(old_site.get("options") or {})
        new_options = dict(site.get("options") or {})
        merged_options = dict(old_options)
        merged_options.update({k: v for k, v in new_options.items() if v not in (None, "")})
        site["options"] = merged_options

        new_headers = []
        old_headers = old_site.get("headers", []) or []
        for h in site.get("headers", []) or []:
            h = dict(h)
            if is_masked(h.get("value", "")):
                # 按 key 找老 header 的 value
                for oh in old_headers:
                    if oh.get("key") == h.get("key"):
                        h["value"] = oh.get("value", "")
                        break
                else:
                    h["value"] = ""
            new_headers.append(h)
        site["headers"] = new_headers
        merged_sites.append(site)
    merged["sites"] = merged_sites

    # web.secret_key 不允许前端覆盖；从 old 继承
    web = dict(merged.get("web") or {})
    if "secret_key" in (old.get("web") or {}):
        web["secret_key"] = (old.get("web") or {}).get("secret_key", "")
    if is_masked(web.get("password", "")):
        web["password"] = (old.get("web") or {}).get("password", "")
    merged["web"] = web

    # cookiecloud.password 同属敏感字段，回传 *** 时保留旧值
    cc = dict(merged.get("cookiecloud") or {})
    if is_masked(cc.get("password", "")):
        cc["password"] = (old.get("cookiecloud") or {}).get("password", "")
    merged["cookiecloud"] = cc

    return merged


def ensure_secret_key() -> str:
    """启动时调用：若 [web].secret_key 为空，生成并写回；返回当前 secret_key。"""
    raw = read_raw()
    web = raw.get("web") or {}
    if web.get("secret_key"):
        return web["secret_key"]
    new_key = os.urandom(32).hex()
    web["secret_key"] = new_key
    raw["web"] = web
    atomic_write(raw)
    return new_key
