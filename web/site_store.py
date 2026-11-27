#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
站点配置的局部读写。

老版本只能整份保存配置（PUT /api/config 全量），改一个站点也要把 14 个站全传一遍，
前端漏字段就会静默丢配置。这里提供站点段（config.toml 的 [[sites]]）的增删改，
复用 config_io 的 read_raw / atomic_write（原子写 + 进程内锁）。

约定：
- 站点唯一键是 domain（域名键），name 也必须唯一（数据库里 Torrent.site 存的是站点名）
- 提交 cookie / api_key / token 为 "***" 或空时，沿用原值（前端本来就没拿到明文）
"""
from typing import Any, Dict, List, Optional

from config.config import SiteModel, site_domain_of
from web.config_io import atomic_write, read_raw
from web.config_serializer import is_masked


class SiteStoreError(Exception):
    """站点写入失败（校验不通过），消息直接给前端展示。"""


def list_sites_raw() -> List[Dict[str, Any]]:
    raw = read_raw()
    sites = raw.get("sites") or []
    return [dict(item) for item in sites if isinstance(item, dict)]


def _domain_of(site: Dict[str, Any]) -> str:
    explicit = str(site.get("domain") or "")
    if explicit:
        return site_domain_of(explicit) or explicit
    url = site.get("base_url") or (site.get("options") or {}).get("rss_url") or ""
    return site_domain_of(url) or ""


def build_site(payload: Dict[str, Any], base: Optional[Dict[str, Any]] = None) -> SiteModel:
    """
    把前端提交的字段合成 SiteModel 并跑一遍校验。

    base 是该站点在 toml 里的原值：提交里没给的字段（比如 options 里的
    pages / request_interval）以及被掩码的敏感字段都从 base 继承。
    """
    merged: Dict[str, Any] = {}
    if base:
        merged.update({k: v for k, v in base.items() if v is not None})

    incoming = dict(payload or {})
    # options 是嵌套字典，站点编辑框只暴露少数几项，其余必须保留老值
    new_options = incoming.pop("options", None)
    if new_options is not None:
        old_options = dict((base or {}).get("options") or {})
        for key, value in dict(new_options).items():
            if value is not None:
                old_options[key] = value
        merged["options"] = old_options

    merged.update({k: v for k, v in incoming.items() if k != "options"})

    # 掩码 / 空值的敏感字段沿用原值
    for field in ("cookie", "api_key", "token"):
        value = merged.get(field, "")
        if is_masked(value) or (value in (None, "") and base and base.get(field)):
            merged[field] = (base or {}).get(field, "")

    headers = merged.get("headers")
    if isinstance(headers, list):
        merged["headers"] = [
            {"key": (h or {}).get("key", ""), "value": (h or {}).get("value", "")}
            for h in headers
            if isinstance(h, dict) and (h.get("key") or "")
        ]

    # domain 由地址推导，别让前端传脏值
    if not merged.get("domain"):
        merged["domain"] = _domain_of(merged)

    try:
        return SiteModel(**merged)
    except Exception as e:
        raise SiteStoreError(f"站点配置校验失败：{getattr(e, 'errors', lambda: e)() if hasattr(e, 'errors') else e}")


def _dump(site: SiteModel) -> Dict[str, Any]:
    data = site.model_dump(exclude_none=True)
    data["headers"] = [{"key": h.get("key", ""), "value": h.get("value", "")} for h in data.get("headers") or []]
    options = {k: v for k, v in (data.get("options") or {}).items() if v is not None}
    data["options"] = options
    # 空串的敏感字段不写进 toml，保持文件干净
    for field in ("cookie", "api_key", "token"):
        if data.get(field) == "":
            data.pop(field, None)
    if not data.get("domain"):
        data.pop("domain", None)
    # 站点没单独填保存路径就不写这个键
    if data.get("save_path") == "":
        data.pop("save_path", None)
    return data


def _check_unique(sites: List[Dict[str, Any]], candidate: SiteModel, skip_index: int = -1) -> None:
    for i, site in enumerate(sites):
        if i == skip_index:
            continue
        existing_domain = _domain_of(site)
        existing_name = str(site.get("name") or "")
        if candidate.domain and existing_domain and candidate.domain == existing_domain:
            raise SiteStoreError(f"域名 {candidate.domain} 已经被站点「{existing_name}」占用，不能重复添加")
        if candidate.name and candidate.name == existing_name:
            raise SiteStoreError(f"站点名「{candidate.name}」已存在，请换一个名字（站点名在数据库里是唯一键）")


def upsert_site(payload: Dict[str, Any], old_domain: str = "") -> Dict[str, Any]:
    """
    新增或更新一个站点。

    old_domain 为空 → 追加；不为空 → 定位原站点后原地替换（支持站点改名和换域名）。
    返回写入后的站点 dict。
    """
    sites = list_sites_raw()
    index = -1
    if old_domain:
        for i, site in enumerate(sites):
            if _domain_of(site) == old_domain or str(site.get("name") or "") == old_domain:
                index = i
                break
        if index < 0:
            raise SiteStoreError(f"没有找到要更新的站点：{old_domain}")

    base = sites[index] if index >= 0 else None
    site = build_site(payload, base=base)
    _check_unique(sites, site, skip_index=index)

    dumped = _dump(site)
    if index >= 0:
        sites[index] = dumped
    else:
        sites.append(dumped)

    raw = read_raw()
    raw["sites"] = sites
    atomic_write(raw)
    from ptsite.profile import clear_cache
    from ptsite import throttle

    clear_cache()
    throttle.reset()
    return dumped


def delete_site(domain: str) -> Dict[str, Any]:
    """删掉一个站点，返回被删的原始字典（调用方拿它的名字去清理引用）。"""
    sites = list_sites_raw()
    target = None
    remaining: List[Dict[str, Any]] = []
    for site in sites:
        if target is None and (
            _domain_of(site) == domain or str(site.get("name") or "") == domain
        ):
            target = site
            continue
        remaining.append(site)
    if target is None:
        raise SiteStoreError(f"没有找到要删除的站点：{domain}")

    raw = read_raw()
    raw["sites"] = remaining
    atomic_write(raw)
    from ptsite.profile import clear_cache
    from ptsite import throttle

    clear_cache()
    throttle.reset(_domain_of(target))
    return target
