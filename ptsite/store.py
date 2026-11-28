#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
站点运行数据的仓储层（对齐 MoviePilot 的 siteuserdata / sitestatistic / siteicon 三表）。

三张表全部按 domain（域名键）关联：
- 站点改名 → 数据都还在（键没变）
- 站点删了再建（同一个域名）→ 历史快照和图标直接复用

只做读写，不掺业务：
- SiteUserData   每次抓账号概览存一条快照，按 domain + 日期 + 时间唯一
- SiteStatistic  每站一行，累计成功/失败次数、本轮耗时、最后访问状态
- SiteIcon       favicon 的 base64 缓存，站点挂了也能正常显示图标
"""
import base64
import re
from datetime import datetime
from typing import Any, Dict, Optional

from loguru import logger

from db import SiteIcon, SiteStatistic, SiteUserData

_SIZE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE])?i?B", re.I)
_UNIT = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5, "E": 1024 ** 6}


def parse_bytes(value: Any) -> int:
    """把 "99.051 TB" / "1024" / "" 这类值转成字节数，解析不了返回 0。"""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip()
    if not text:
        return 0
    match = _SIZE_RE.search(text)
    if match:
        try:
            return int(float(match.group(1)) * _UNIT.get((match.group(2) or "").upper(), 1))
        except (TypeError, ValueError):
            return 0
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else 0


def _domain_of(site: Any, fallback: str = "") -> str:
    if isinstance(site, dict):
        return str(site.get("domain") or "") or fallback
    from config.config import site_domain_of

    return (
        str(getattr(site, "domain", "") or "")
        or site_domain_of(
            getattr(site, "base_url", "") or ((getattr(site, "options", None) or {}).get("rss_url") or "")
        )
        or fallback
        or str(getattr(site, "name", "") or "")
    )


def save_snapshot(site: Any, result: Dict, domain: str = "") -> Optional[SiteUserData]:
    """把一次账号概览抓取结果落库（成功失败都记，失败的原因留证）。"""
    key = domain or _domain_of(site)
    if not key:
        return None
    now = datetime.now()
    payload = dict(
        domain=key,
        name=str(result.get("name") or getattr(site, "name", "") or ""),
        username=str(result.get("username") or ""),
        user_level=str(result.get("user_level") or ""),
        upload=parse_bytes(result.get("uploaded")),
        download=parse_bytes(result.get("downloaded")),
        ratio=_to_float(result.get("ratio")),
        seeding=int(result.get("seeding") or 0),
        leeching=int(result.get("leeching") or 0),
        seeding_size=parse_bytes(result.get("seeding_size")),
        leeching_size=parse_bytes(result.get("leeching_size")),
        ok=bool(result.get("ok")),
        err_msg=str(result.get("message") or "")[:250],
        updated_day=now.strftime("%Y-%m-%d"),
        updated_time=now.strftime("%H:%M:%S"),
    )
    try:
        row = SiteUserData.create(**payload)
        return row
    except Exception as e:  # 唯一键冲突（同一秒重复抓）等情况不该拖垮抓取
        logger.warning(f"站点 {key} 快照写入失败：{e}")
        return None


def _to_float(value: Any) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def record_statistic(domain: str, ok: bool, seconds: float = 0) -> None:
    """更新站点健康统计：成功/失败累加，记录最后一次结果与耗时。"""
    if not domain:
        return
    now = datetime.now()
    try:
        row, created = SiteStatistic.get_or_create(domain=domain)
        if ok:
            row.success = (row.success or 0) + 1
        else:
            row.fail = (row.fail or 0) + 1
        row.seconds = int(seconds or 0)
        row.lst_state = 0 if ok else 1
        row.lst_mod_date = now.strftime("%Y-%m-%d %H:%M:%S")
        row.updated_time = now
        row.save()
    except Exception as e:
        logger.warning(f"站点 {domain} 统计写入失败：{e}")


def latest_snapshots() -> Dict[str, Dict]:
    """每个站点最近一次快照：domain -> 摘要。"""
    result: Dict[str, Dict] = {}
    try:
        rows = (
            SiteUserData.select()
            .order_by(SiteUserData.domain.asc(), SiteUserData.id.desc())
        )
        for row in rows:
            if row.domain in result:
                continue
            result[row.domain] = {
                "domain": row.domain,
                "name": row.name,
                "username": row.username,
                "user_level": row.user_level,
                "upload": row.upload,
                "download": row.download,
                "ratio": row.ratio,
                "seeding": row.seeding,
                "leeching": row.leeching,
                "seeding_size": row.seeding_size,
                "leeching_size": row.leeching_size,
                "ok": bool(row.ok),
                "message": row.err_msg,
                "updated_at": f"{row.updated_day} {row.updated_time}",
            }
    except Exception as e:
        logger.warning(f"读取站点快照失败：{e}")
    return result


def statistic_of(domain: str) -> Dict:
    try:
        row = SiteStatistic.get_or_none(SiteStatistic.domain == domain)
    except Exception:
        row = None
    if not row:
        return {"success": 0, "fail": 0, "seconds": 0, "lst_state": 0, "lst_mod_date": ""}
    return {
        "success": row.success or 0,
        "fail": row.fail or 0,
        "seconds": row.seconds or 0,
        "lst_state": row.lst_state or 0,
        "lst_mod_date": row.lst_mod_date or "",
    }


def save_icon(domain: str, name: str, url: str, content: bytes, mime: str = "image/x-icon") -> bool:
    """把 favicon 的二进制存成 base64；同样的内容已存在就不重复写。"""
    if not content or not domain:
        return False
    try:
        encoded = base64.b64encode(content).decode("ascii")
    except Exception:
        return False
    now = datetime.now()
    try:
        row = SiteIcon.get_or_none(SiteIcon.domain == domain)
        if row:
            if row.base64 == encoded and row.url == url:
                return True
            row.base64 = encoded
            row.url = url
            row.name = name or row.name
            row.mime = mime or "image/x-icon"
            row.updated_time = now
            row.save()
        else:
            SiteIcon.create(
                domain=domain,
                name=name or "",
                url=url or "",
                mime=mime or "image/x-icon",
                base64=encoded,
                updated_time=now,
            )
        return True
    except Exception as e:
        logger.warning(f"站点 {domain} 图标写入失败：{e}")
        return False


def icon_record(domain: str) -> Dict[str, str]:
    """图标原始记录：mime / base64 / 更新时间，接口按它拼响应。"""
    try:
        row = SiteIcon.get_or_none(SiteIcon.domain == domain)
    except Exception:
        row = None
    if not row or not row.base64:
        return {}
    return {
        "mime": row.mime or "image/x-icon",
        "base64": row.base64,
        "updated_at": row.updated_time.strftime("%Y-%m-%d %H:%M:%S") if row.updated_time else "",
    }


def icon_version(domain: str) -> str:
    """图标版本号（按更新时间）：拼进 /api/sites/icon?domain=&v=，换图标后前端立即拿到新的。"""
    try:
        row = SiteIcon.get_or_none(SiteIcon.domain == domain)
    except Exception:
        row = None
    if not row or not row.base64 or not row.updated_time:
        return ""
    return row.updated_time.strftime("%y%m%d%H%M")


def has_icon(domain: str) -> bool:
    try:
        row = SiteIcon.get_or_none(SiteIcon.domain == domain)
    except Exception:
        row = None
    return bool(row and row.base64)


def cleanup(days: int = 90) -> int:
    """清掉过老的快照，避免表无限膨胀；每站最近一条永远保留。"""
    if days <= 0:
        return 0
    from datetime import timedelta

    keep_after = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        return SiteUserData.delete().where(SiteUserData.updated_day < keep_after).execute()
    except Exception as e:
        logger.warning(f"清理站点历史快照失败：{e}")
        return 0
