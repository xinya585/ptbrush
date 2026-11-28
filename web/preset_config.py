"""快速开始套餐 -> 刷流配置参数。

套餐只覆盖刷流配置里的简单数值/开关，不创建额外数据，也不动站点、
下载器、保存路径、工作时间、qBittorrent 分类名等用户自己的设置。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from config.config import PROMOTION_META, PTBrushConfig, parse_size
from web.config_io import atomic_write, read_raw
from web.presets import PRESETS
from web.runtime import reschedule_brush, reschedule_check

_PROMO_LABELS = {item["value"]: item["label"] for item in PROMOTION_META}

# (配置键, 界面名)；slow_up_delete 是把开关 + 阈值 + 分钟合成一项展示
GROUPS: List[Tuple[str, str]] = [
    ("promotions", "促销范围"),
    ("exclude_hr", "排除 H&R"),
    ("min_free_hours", "免费剩余不足"),
    ("size", "种子体积范围"),
    ("seeder", "做种人数范围"),
    ("pubtime", "发布时间范围"),
    ("max_downloading_torrents", "最大同时下载"),
    ("min_disk_space", "最小剩余磁盘"),
    ("download_speed_min", "低速不占位阈值"),
    ("slow_delete_speed", "低速下载删种阈值"),
    ("slow_delete_minutes", "低速下载持续"),
    ("slow_up_delete", "低速上传删种（盒子专用）"),
    ("max_no_activate_time", "无上传超时"),
    ("free_end_delete_minutes", "免费结束前删种"),
    ("seed_time", "做种时长"),
    ("hr_seed_time", "H&R 做种时长"),
    ("seed_ratio", "分享率"),
    ("brush_interval", "刷流间隔"),
    ("check_interval", "检查周期"),
]

_SIZE_KEYS = {"min_disk_space"}
_NUMBER_KEYS = {
    "max_downloading_torrents", "min_free_hours", "slow_delete_minutes",
    "max_no_activate_time", "free_end_delete_minutes", "seed_time",
    "hr_seed_time", "seed_ratio", "brush_interval", "check_interval",
}
_SPEED_KEYS = {"download_speed_min", "slow_delete_speed", "slow_up_delete_speed"}
_BOOL_KEYS = {"exclude_hr", "slow_up_delete_enabled"}


def _num(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value or "0")
    if number == int(number):
        return str(int(number))
    return f"{number:g}"


def _bytes(value: Any) -> int:
    try:
        return int(parse_size(value))
    except (TypeError, ValueError):
        return 0


def _fmt_bytes(value: Any) -> str:
    size = _bytes(value)
    if size <= 0:
        return "0"
    for unit, factor in (("TiB", 1024 ** 4), ("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if size >= factor:
            return f"{size / factor:.2f}".rstrip("0").rstrip(".") + f" {unit}"
    return f"{size} B"


def _fmt_speed(value: Any) -> str:
    return f"{_num(value)} MiB/s"


def _fmt_bool(value: Any) -> str:
    return "是" if bool(value) else "否"


def _fmt_promotions(value: Any) -> str:
    labels = [_PROMO_LABELS.get(str(v), str(v)) for v in (value or [])]
    return "、".join(labels) or "不限制"


def _fmt_value(key: str, value: Any) -> str:
    if key == "promotions":
        return _fmt_promotions(value)
    if key == "exclude_hr":
        return _fmt_bool(value)
    if key == "min_free_hours":
        return "不限制" if int(value or 0) <= 0 else f"{_num(value)} 小时"
    if key == "size":
        return f"{value} GiB" if str(value or "").strip() else "不限制"
    if key == "seeder":
        return f"{value} 人" if str(value or "").strip() else "不限制"
    if key == "pubtime":
        return f"{value} 分钟" if str(value or "").strip() else "不限制"
    if key == "max_downloading_torrents":
        return f"{_num(value)} 个"
    if key == "min_disk_space":
        return "不限制" if _bytes(value) <= 0 else _fmt_bytes(value)
    if key == "download_speed_min":
        return "关闭" if float(value or 0) <= 0 else _fmt_speed(value)
    if key == "slow_delete_speed":
        return "关闭" if float(value or 0) <= 0 else _fmt_speed(value)
    if key == "slow_delete_minutes":
        return "关闭" if int(value or 0) <= 0 else f"{_num(value)} 分钟"
    if key == "max_no_activate_time":
        return "关闭" if int(value or 0) <= 0 else f"{_num(value)} 分钟"
    if key == "free_end_delete_minutes":
        return "关闭" if int(value or 0) <= 0 else f"提前 {_num(value)} 分钟"
    if key == "seed_time":
        return "关闭" if float(value or 0) <= 0 else f"{_num(value)} 小时"
    if key == "hr_seed_time":
        return "不单独限制" if float(value or 0) <= 0 else f"{_num(value)} 小时"
    if key == "seed_ratio":
        return "关闭" if float(value or 0) <= 0 else _num(value)
    if key == "brush_interval":
        return f"{_num(value)} 分钟"
    if key == "check_interval":
        return f"{_num(value)} 分钟"
    return str(value)


def _slow_up_summary(data: Dict[str, Any]) -> str:
    enabled = bool(data.get("slow_up_delete_enabled"))
    minutes = int(data.get("slow_up_delete_minutes") or 0)
    speed = float(data.get("slow_up_delete_speed") or 0)
    if not enabled or minutes <= 0 or speed <= 0:
        return "关闭"
    return f"开启（{_fmt_speed(speed)} / {minutes} 分钟）"


def _current_raw(config: PTBrushConfig) -> Dict[str, Any]:
    b = config.brush
    return {
        "promotions": list(b.promotions or []),
        "exclude_hr": bool(b.exclude_hr),
        "min_free_hours": int(b.min_free_hours or 0),
        "size": b.size or "",
        "seeder": b.seeder or "",
        "pubtime": b.pubtime or "",
        "max_downloading_torrents": int(b.max_downloading_torrents or 0),
        "min_disk_space": _bytes(b.min_disk_space),
        "download_speed_min": float(b.download_speed_min or 0),
        "slow_delete_speed": float(b.slow_delete_speed or 0),
        "slow_delete_minutes": int(b.slow_delete_minutes or 0),
        "slow_up_delete_enabled": bool(b.slow_up_delete_enabled),
        "slow_up_delete_minutes": int(b.slow_up_delete_minutes or 0),
        "slow_up_delete_speed": float(b.slow_up_delete_speed or 0),
        "max_no_activate_time": int(b.max_no_activate_time or 0),
        "free_end_delete_minutes": int(b.free_end_delete_minutes or 0),
        "seed_time": float(b.seed_time or 0),
        "hr_seed_time": float(b.hr_seed_time or 0),
        "seed_ratio": float(b.seed_ratio or 0),
        "brush_interval": int(b.brush_interval or 0),
        "check_interval": int(b.check_interval or 0),
    }


def _normalize(key: str, value: Any) -> Any:
    if key == "slow_up_delete":
        return (
            bool(value.get("slow_up_delete_enabled")),
            int(value.get("slow_up_delete_minutes") or 0),
            float(value.get("slow_up_delete_speed") or 0),
        )
    if key in _SIZE_KEYS:
        return _bytes(value)
    if key in _SPEED_KEYS:
        return float(value or 0)
    if key in _BOOL_KEYS:
        return bool(value)
    if key in ("promotions",):
        return [str(v) for v in (value or [])]
    if key in _NUMBER_KEYS:
        return float(value or 0)
    return str(value or "")


def _group_pair(key: str, current: Dict[str, Any], target: Dict[str, Any]) -> Tuple[str, str]:
    if key == "slow_up_delete":
        return _slow_up_summary(current), _slow_up_summary(target)
    return _fmt_value(key, current.get(key)), _fmt_value(key, target.get(key))


def preview_preset(pack: Dict[str, Any], config: PTBrushConfig) -> Dict[str, Any]:
    current = _current_raw(config)
    target = dict(pack.get("brush") or {})
    items = []
    for key, label in GROUPS:
        old_text, new_text = _group_pair(key, current, target)
        if key == "slow_up_delete":
            changed = _normalize(key, current) != _normalize(key, target)
        else:
            changed = _normalize(key, current.get(key)) != _normalize(key, target.get(key))
        if changed:
            items.append({"key": key, "label": label, "current": old_text, "target": new_text})
    return {
        "preset": {"id": pack["id"], "name": pack["name"]},
        "items": items,
        "changed": len(items),
        "total": len(GROUPS),
    }


def matching_preset_id(config: PTBrushConfig) -> str:
    matches = [p["id"] for p in PRESETS if preview_preset(p, config)["changed"] == 0]
    return matches[0] if len(matches) == 1 else ""


def apply_preset(pack: Dict[str, Any]) -> Dict[str, Any]:
    current = PTBrushConfig()
    preview = preview_preset(pack, current)

    raw = read_raw()
    brush = dict(raw.get("brush") or {})
    brush.update(pack.get("brush") or {})
    raw["brush"] = brush

    # 写入前跑一遍完整校验，避免套餐值把 config.toml 写坏
    PTBrushConfig.model_validate(raw)
    atomic_write(raw)

    applied = reschedule_brush(int(brush.get("brush_interval") or 10))
    check_applied = reschedule_check(int(brush.get("check_interval") or 5))
    return {
        "ok": True,
        "changed": preview["changed"],
        "total": preview["total"],
        "preset": {"id": pack["id"], "name": pack["name"]},
        "brush_interval_applied": applied,
        "check_interval_applied": check_applied,
        "brush": _current_raw(PTBrushConfig()),
    }
