#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""PTBrush 规则引擎（1.3.0 预览版）。

设计约束：
- 不执行任意脚本，条件表只认「字段 + 操作符 + 值」，值支持带单位（10 GiB / 200 KiB/s / 6 h）。
- 预设只提供规则模板，不绑定站点；站点数量由用户自己决定。
- 规则默认停用，删种另有每轮/每小时安全阀。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------- 字段字典
# type: size / speed / duration / number / string / enum / bool
SELECT_FIELDS = [
    {"key": "site", "label": "站点", "type": "string"},
    {"key": "promo", "label": "促销", "type": "enum",
     "options": ["free", "free_2x", "2x", "half_down"]},
    {"key": "title", "label": "标题", "type": "string"},
    {"key": "size", "label": "体积", "type": "size"},
    {"key": "seeders", "label": "做种人数", "type": "number"},
    {"key": "leechers", "label": "下载人数", "type": "number"},
    {"key": "snatches", "label": "完成人数", "type": "number"},
    {"key": "publish_time", "label": "发布距今", "type": "duration"},
    {"key": "hr", "label": "H&R", "type": "bool"},
    {"key": "category", "label": "分类", "type": "string"},
    {"key": "tags", "label": "标签", "type": "string"},
]

DELETE_FIELDS = [
    {"key": "name", "label": "种子名", "type": "string"},
    {"key": "progress", "label": "进度 %", "type": "number"},
    {"key": "ratio", "label": "分享率", "type": "number"},
    {"key": "uploaded", "label": "已上传", "type": "size"},
    {"key": "downloaded", "label": "已下载", "type": "size"},
    {"key": "upload_speed", "label": "上传速度", "type": "speed"},
    {"key": "download_speed", "label": "下载速度", "type": "speed"},
    {"key": "state", "label": "状态", "type": "string"},
    {"key": "seed_time", "label": "做种时长", "type": "duration"},
    {"key": "added_time", "label": "添加距今", "type": "duration"},
    {"key": "completed_time", "label": "完成距今", "type": "duration"},
    {"key": "free_space", "label": "剩余空间", "type": "size"},
    {"key": "total_size", "label": "种子体积", "type": "size"},
    {"key": "tracker", "label": "站点", "type": "string"},
    {"key": "seeding_count", "label": "做种数", "type": "number"},
    {"key": "leeching_count", "label": "下载数", "type": "number"},
    {"key": "global_up", "label": "总上传", "type": "size"},
    {"key": "global_down", "label": "总下载", "type": "size"},
    {"key": "hour_of_day", "label": "当前小时", "type": "number"},
    {"key": "category", "label": "分类", "type": "string"},
    {"key": "tags", "label": "标签", "type": "string"},
]

OPERATORS = [
    {"key": "eq", "label": "等于"},
    {"key": "neq", "label": "不等于"},
    {"key": "gt", "label": "大于"},
    {"key": "gte", "label": "大于等于"},
    {"key": "lt", "label": "小于"},
    {"key": "lte", "label": "小于等于"},
    {"key": "between", "label": "介于"},
    {"key": "in", "label": "属于"},
    {"key": "not_in", "label": "不属于"},
    {"key": "contains", "label": "包含"},
    {"key": "not_contains", "label": "不包含"},
    {"key": "regex", "label": "正则匹配"},
    {"key": "not_regex", "label": "正则不匹配"},
]

_FIELD_MAP = {f["key"]: f for f in (SELECT_FIELDS + DELETE_FIELDS)}
_DURATION_UNITS = {"s": 1, "秒": 1, "m": 60, "分钟": 60, "h": 3600, "小时": 3600, "d": 86400, "天": 86400}
_BYTE_UNITS = {
    "b": 1, "byte": 1, "bytes": 1,
    "kb": 1000, "kib": 1024, "mb": 1000 ** 2, "mib": 1024 ** 2,
    "gb": 1000 ** 3, "gib": 1024 ** 3, "tb": 1000 ** 4, "tib": 1024 ** 4,
}


def fields_for(kind: str) -> List[Dict[str, Any]]:
    return list(SELECT_FIELDS if kind == "select" else DELETE_FIELDS)


def _split_unit(raw: Any) -> Tuple[Optional[float], str]:
    if isinstance(raw, (int, float)):
        return float(raw), ""
    text = str(raw or "").strip()
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*([A-Za-z\u4e00-\u9fa5/]*)$", text)
    if not m:
        return None, ""
    return float(m.group(1)), m.group(2).lower()


def parse_value(field: str, raw: Any) -> Any:
    """把 UI 里填的值转成可比较的数值/字符串。识别不了就原样返回，比较时按字符串处理。"""
    meta = _FIELD_MAP.get(field) or {}
    ftype = meta.get("type")
    if raw is None:
        return None
    if ftype == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "是", "on")
    if ftype in ("size", "speed", "duration"):
        num, unit = _split_unit(raw)
        if num is None:
            return raw
        if ftype == "duration":
            return num * _DURATION_UNITS.get(unit or "h", 3600)
        factor = _BYTE_UNITS.get(unit or ("b" if ftype == "size" else "b/s"))
        if factor is None:
            # 速度单位可能写成 MiB/s，去掉 /s 再查
            factor = _BYTE_UNITS.get(unit.replace("/s", ""), 1)
        return num * factor
    if ftype == "number":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw
    return str(raw)


def _cmp(a: Any, b: Any) -> Optional[int]:
    try:
        if a is None or b is None:
            return None
        if isinstance(a, str) or isinstance(b, str):
            a, b = str(a), str(b)
            return (a > b) - (a < b)
        a, b = float(a), float(b)
        return (a > b) - (a < b)
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value]
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def check_condition(cond: Dict[str, Any], item: Dict[str, Any]) -> bool:
    field = str(cond.get("field") or "")
    op = str(cond.get("op") or "eq")
    raw = cond.get("value")
    raw2 = cond.get("value2")
    actual = item.get(field)
    meta = _FIELD_MAP.get(field) or {}
    ftype = meta.get("type")

    if op in ("in", "not_in"):
        values = [parse_value(field, v) for v in _as_list(raw)]
        hit = actual in values or str(actual) in [str(v) for v in values]
        return hit if op == "in" else not hit

    if op == "between":
        low = parse_value(field, raw)
        high = parse_value(field, raw2)
        a = _cmp(actual, low)
        b = _cmp(actual, high)
        return bool(a is not None and b is not None and a >= 0 and b <= 0)

    if op in ("contains", "not_contains"):
        hit = str(raw or "") in str(actual or "")
        return hit if op == "contains" else not hit

    if op in ("regex", "not_regex"):
        try:
            hit = re.search(str(raw or ""), str(actual or "")) is not None
        except re.error:
            hit = False
        return hit if op == "regex" else not hit

    expected = parse_value(field, raw)
    if ftype == "bool":
        return (bool(actual) == bool(expected)) if op in ("eq", "gte", "lte") else (bool(actual) != bool(expected))

    c = _cmp(actual, expected)
    if c is None:
        # 数值比较失败时退回字符串相等，避免规则整条崩掉
        return str(actual) == str(raw) if op == "eq" else False
    if op == "eq":
        return c == 0
    if op == "neq":
        return c != 0
    if op == "gt":
        return c > 0
    if op == "gte":
        return c >= 0
    if op == "lt":
        return c < 0
    if op == "lte":
        return c <= 0
    return False


def evaluate(rule: Dict[str, Any], item: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]]]:
    """返回 (是否命中, 未满足的条件列表)。空条件视为不命中，防止一条空规则删光。"""
    conditions = rule.get("conditions") or []
    if isinstance(conditions, str):
        try:
            conditions = json.loads(conditions)
        except Exception:
            conditions = []
    if not conditions:
        return False, [{"field": "*", "reason": "空条件"}]
    match = str(rule.get("match") or "all").lower()
    failed = []
    for cond in conditions:
        try:
            ok = check_condition(cond, item)
        except Exception:
            ok = False
        if not ok:
            failed.append({"field": cond.get("field"), "op": cond.get("op"), "value": cond.get("value")})
            if match != "any":
                return False, failed
        elif match == "any":
            return True, []
    return (match == "all"), failed


def summarize(rule: Dict[str, Any]) -> str:
    conditions = rule.get("conditions") or []
    if isinstance(conditions, str):
        try:
            conditions = json.loads(conditions)
        except Exception:
            conditions = []
    labels = {o["key"]: o["label"] for o in OPERATORS}
    parts = []
    for c in conditions:
        field = _FIELD_MAP.get(c.get("field")) or {}
        parts.append(f"{field.get('label') or c.get('field')} {labels.get(c.get('op'), c.get('op'))} {c.get('value')}"
                     + (f" ~ {c.get('value2')}" if c.get("op") == "between" else ""))
    joiner = " 且 " if str(rule.get("match") or "all") != "any" else " 或 "
    return joiner.join(parts) if parts else "（未设置条件）"


# ---------------------------------------------------------------- 预设模板
# 只放规则模板与任务参数：不含任何站点清单，导入时按用户自己的站点生成任务。
PRESETS: List[Dict[str, Any]] = [
    {
        "id": "brush-general",
        "name": "通用刷流",
        "badge": "推荐",
        "desc": "不绑定站点：你有几个站就套几个。小水管与盒子都能用，上限保守。",
        "task": {"name": "通用刷流", "per_hour": 20, "interval": 10},
        "rules": [
            {"kind": "select", "name": "免费种 · 10G 以上", "priority": 10, "match": "all",
             "conditions": [
                 {"field": "promo", "op": "in", "value": ["free", "free_2x"]},
                 {"field": "size", "op": "gte", "value": "10 GiB"},
                 {"field": "hr", "op": "eq", "value": False},
             ], "description": "只挑免费/免费 2x，体积 10G 起，跳过 H&R"},
            {"kind": "select", "name": "免费大种 · 30G 以上", "priority": 20, "match": "all",
             "conditions": [
                 {"field": "promo", "op": "in", "value": ["free", "free_2x"]},
                 {"field": "size", "op": "gte", "value": "30 GiB"},
                 {"field": "seeders", "op": "lte", "value": 60},
             ], "description": "大种优先，做种人数过多不抢"},
            {"kind": "delete", "name": "低分享率 · 做种 6h 后", "priority": 10, "match": "all",
             "conditions": [
                 {"field": "seed_time", "op": "gte", "value": "6 h"},
                 {"field": "ratio", "op": "lt", "value": 0.5},
             ], "limits": {"per_round": 3, "per_hour": 30}, "description": "做满 6 小时仍不到 0.5 才删"},
            {"kind": "delete", "name": "出种后低速", "priority": 20, "match": "all",
             "conditions": [
                 {"field": "progress", "op": "gte", "value": 100},
                 {"field": "upload_speed", "op": "lt", "value": "200 KiB/s"},
                 {"field": "seed_time", "op": "gte", "value": "2 h"},
             ], "limits": {"per_round": 3, "per_hour": 30}, "description": "下完 2 小时还没上传速度"},
            {"kind": "delete", "name": "空间告急", "priority": 5, "match": "all",
             "conditions": [{"field": "free_space", "op": "lt", "value": "50 GiB"}],
             "limits": {"per_round": 3, "per_hour": 30}, "description": "磁盘剩不到 50G 时腾位置"},
        ],
    },
    {
        "id": "seed-first",
        "name": "保种优先",
        "badge": "",
        "desc": "删种更慢、分享率门槛更高，适合号上保种多、怕误删的场景。",
        "task": {"name": "保种优先", "per_hour": 10, "interval": 15},
        "rules": [
            {"kind": "select", "name": "免费大种 · 30G 起", "priority": 10, "match": "all",
             "conditions": [
                 {"field": "promo", "op": "in", "value": ["free", "free_2x"]},
                 {"field": "size", "op": "gte", "value": "30 GiB"},
             ], "description": "只下大种，减少种子数量"},
            {"kind": "delete", "name": "严格低分享率", "priority": 10, "match": "all",
             "conditions": [
                 {"field": "progress", "op": "gt", "value": 50},
                 {"field": "seed_time", "op": "gte", "value": "24 h"},
                 {"field": "ratio", "op": "lt", "value": 0.2},
             ], "limits": {"per_round": 1, "per_hour": 5}, "description": "只删做满一天仍极低的"},
            {"kind": "delete", "name": "空间告急 · 保守", "priority": 20, "match": "all",
             "conditions": [{"field": "free_space", "op": "lt", "value": "30 GiB"}],
             "limits": {"per_round": 1, "per_hour": 5}, "description": "空间真不够才删"},
        ],
    },
    {
        "id": "big-box",
        "name": "大硬盘盒子",
        "badge": "",
        "desc": "优先大体积、放宽做种人数下限，配合高上传带宽跑量。",
        "task": {"name": "大硬盘盒子", "per_hour": 50, "interval": 5},
        "rules": [
            {"kind": "select", "name": "大种优先 · 60G 起", "priority": 10, "match": "all",
             "conditions": [
                 {"field": "size", "op": "gte", "value": "60 GiB"},
                 {"field": "promo", "op": "in", "value": ["free", "free_2x"]},
             ], "description": "大盘跑量，只挑大种"},
            {"kind": "delete", "name": "慢车清退", "priority": 10, "match": "all",
             "conditions": [
                 {"field": "seed_time", "op": "gte", "value": "3 h"},
                 {"field": "ratio", "op": "lt", "value": 0.3},
             ], "limits": {"per_round": 10, "per_hour": 50}, "description": "3 小时跑不动就换"},
            {"kind": "delete", "name": "空间不足优先清", "priority": 5, "match": "all",
             "conditions": [{"field": "free_space", "op": "lt", "value": "100 GiB"}],
             "limits": {"per_round": 10, "per_hour": 50}, "description": "大包吃盘，空间不足先清"},
        ],
    },
]


def preset_list() -> List[Dict[str, Any]]:
    result = []
    for pack in PRESETS:
        counts = {"select": 0, "delete": 0}
        for r in pack["rules"]:
            counts[r["kind"]] += 1
        result.append({**pack, "counts": counts})
    return result


def get_preset(preset_id: str) -> Optional[Dict[str, Any]]:
    for pack in PRESETS:
        if pack["id"] == preset_id:
            return pack
    return None


def rule_dict(rule) -> Dict[str, Any]:
    def _load(text, default):
        try:
            return json.loads(text) if text else default
        except Exception:
            return default

    return {
        "id": rule.id,
        "kind": rule.kind,
        "name": rule.name,
        "enabled": bool(rule.enabled),
        "priority": rule.priority,
        "task_ids": _load(rule.task_ids, []),
        "match": rule.match,
        "conditions": _load(rule.conditions, []),
        "sort": _load(rule.sort, {}),
        "limits": _load(rule.limits, {}),
        "protect": _load(rule.protect, {}),
        "preset_id": rule.preset_id,
        "description": rule.description,
        "summary": summarize({"conditions": _load(rule.conditions, []), "match": rule.match}),
        "updated_time": rule.updated_time.strftime("%Y-%m-%d %H:%M") if getattr(rule, "updated_time", None) else "",
    }


def delete_context(qb_service) -> Dict[str, Any]:
    """删种规则要用到的全局量：剩余空间、全站上下行、做种/下载数量、当前小时。"""
    from db import QBStatus

    ctx: Dict[str, Any] = {"free_space": 0, "global_up": 0, "global_down": 0,
                           "seeding_count": 0, "leeching_count": 0,
                           "hour_of_day": datetime.now().hour}
    try:
        latest = QBStatus.select().order_by(QBStatus.id.desc()).first()
        if latest:
            ctx.update({
                "free_space": int(latest.free_space_size or 0),
                "global_up": int(latest.up_total_size or 0),
                "global_down": int(latest.dl_total_size or 0),
            })
    except Exception:
        pass
    torrents = []
    for qb in getattr(qb_service, "_qbs", []) or []:
        try:
            torrents.extend(qb.torrents)
        except Exception:
            continue
    ctx["seeding_count"] = sum(1 for t in torrents if getattr(t, "completed", False))
    ctx["leeching_count"] = sum(1 for t in torrents if not getattr(t, "completed", False))
    return ctx


def item_from_qb(t: Any, ctx: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    size = int(getattr(t, "total_size", 0) or getattr(t, "size", 0) or 0)
    uploaded = int(getattr(t, "up_total_size", 0) or getattr(t, "uploaded", 0) or 0)
    downloaded = int(getattr(t, "dl_total_size", 0) or getattr(t, "downloaded", 0) or 0)
    ratio = getattr(t, "ratio", None)
    if ratio is None:
        ratio = (uploaded / downloaded) if downloaded else 0
    added = getattr(t, "added_on", 0) or 0
    dltime = int(getattr(t, "dltime", 0) or 0)
    # qB 对象没有 added_on / completed_on，服务层已经换算成「加入后秒数」和「做种秒数」
    added_time = int(now - added) if added else dltime
    completed_time = int(getattr(t, "seeding_time", 0) or 0)
    return {
        "name": str(getattr(t, "name", "") or "").split("__meta.")[0],
        "progress": float(getattr(t, "progress", 0) or 0) * 100,
        "ratio": float(ratio or 0),
        "uploaded": uploaded,
        "downloaded": downloaded,
        "upload_speed": int(getattr(t, "upspeed", 0) or getattr(t, "up_speed", 0) or 0),
        "download_speed": int(getattr(t, "dlspeed", 0) or getattr(t, "dl_speed", 0) or 0),
        "state": str(getattr(t, "state", "") or ""),
        "seed_time": int(getattr(t, "seeding_time", 0) or 0),
        "added_time": added_time,
        "completed_time": completed_time,
        "total_size": size,
        "tracker": str(getattr(t, "site", "") or ""),
        "category": str(getattr(t, "category", "") or ""),
        "tags": ",".join(str(x) for x in (getattr(t, "tags", None) or [])),
        **{k: ctx.get(k, 0) for k in ("free_space", "seeding_count", "leeching_count",
                                      "global_up", "global_down", "hour_of_day")},
    }


def item_from_db(row) -> Dict[str, Any]:
    """候选池行（Torrent 表）→ 选种规则可用的字段。"""
    free_end = getattr(row, "free_end_time", None)
    now = datetime.now()
    publish_age = 0
    if free_end:
        try:
            # 免费截止时间一般比发布时间晚，仅作近似：没有发布时间字段时用免费截止估算
            publish_age = max(0, int((free_end - now).total_seconds()))
        except Exception:
            publish_age = 0
    return {
        "site": getattr(row, "site", "") or "",
        "promo": getattr(row, "promotion", "") or "",
        "title": getattr(row, "name", "") or "",
        "size": int(getattr(row, "size", 0) or 0),
        "seeders": int(getattr(row, "seeders", 0) or 0),
        "leechers": int(getattr(row, "leechers", 0) or 0),
        "snatches": int(getattr(row, "snatches", 0) or 0),
        "publish_time": publish_age,
        "hr": bool(getattr(row, "hit_and_run", False)),
        "category": "",
        "tags": "",
    }
