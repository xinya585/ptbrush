#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""搜片功能自己的配置存档。

搜片的站点开关是「只参与搜片」的，跟刷流的 target_sites、站点配置里的
enabled 都是两套东西，所以单独存一份 data/search.json：

- 站点开关按域名键存布尔值，站点改名不影响；没记录的站点默认参与
- TMDB 的 API Key / 站点地址 / 语言也放这里，改完立即生效，不用重启

不写进 config.toml 的原因：config.toml 整份保存有 rev 版本号，搜片开关是
每次点立刻落盘的，混进去会和配置页互相覆盖。
"""
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

from loguru import logger
from paths import data_dir

_LOCK = threading.Lock()

DEFAULT_TMDB_URL = "https://api.themoviedb.org/3"


def _path() -> Path:
    return data_dir() / "search.json"


def _defaults() -> Dict[str, Any]:
    return {
        "tmdb_api_key": "",
        "tmdb_api_url": DEFAULT_TMDB_URL,
        "language": "zh-CN",
        # {域名键: True/False}
        "sites": {},
    }


def get_settings() -> Dict[str, Any]:
    base = _defaults()
    path = _path()
    if path.exists():
        try:
            raw = json.loads(path.read_text("utf-8"))
            if isinstance(raw, dict):
                base.update({k: v for k, v in raw.items() if v is not None})
                if not isinstance(base.get("sites"), dict):
                    base["sites"] = {}
        except Exception as e:
            logger.warning(f"读取 search.json 失败，按默认值处理：{e}")
    return base


def _write(data: Dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, path)


def update_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    """局部更新。tmdb_api_key 传空或 *** 表示「不改」。"""
    with _LOCK:
        data = get_settings()
        for key in ("tmdb_api_url", "language"):
            if key in patch and patch[key] is not None:
                data[key] = str(patch[key]).strip()
        data.pop("proxy", None)
        if "tmdb_api_key" in patch:
            value = str(patch["tmdb_api_key"] or "").strip()
            if value and value != "***":
                data["tmdb_api_key"] = value
        if not str(data.get("tmdb_api_url") or "").strip():
            data["tmdb_api_url"] = DEFAULT_TMDB_URL
        _write(data)
        return data


def set_site_flag(domain: str, enabled: bool) -> Dict[str, Any]:
    with _LOCK:
        data = get_settings()
        sites = dict(data.get("sites") or {})
        key = str(domain or "").strip().lower()
        if key:
            sites[key] = bool(enabled)
        data["sites"] = sites
        _write(data)
        return data


def site_flags() -> Dict[str, bool]:
    raw = get_settings().get("sites") or {}
    return {str(k).lower(): bool(v) for k, v in raw.items()}


def is_site_on(domain: str) -> bool:
    """没记录的站点默认参与，和刷流「名单留空=全参与」的直觉一致。"""
    return site_flags().get(str(domain or "").strip().lower(), True)


def public_view(data: Dict[str, Any] = None) -> Dict[str, Any]:
    """给前端的视图：key 只给「有没有 / 掩码」，不回明文。"""
    data = data or get_settings()
    key = str(data.get("tmdb_api_key") or "")
    return {
        "has_key": bool(key),
        "tmdb_api_key": "***" if key else "",
        "tmdb_api_url": data.get("tmdb_api_url") or DEFAULT_TMDB_URL,
        "language": data.get("language") or "zh-CN",
        "sites": {str(k).lower(): bool(v) for k, v in (data.get("sites") or {}).items()},
    }
