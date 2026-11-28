"""快速开始的三套刷流参数套餐。

这里只定义 [brush] 配置值，不包含规则、任务或站点清单。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

PRESETS: List[Dict[str, Any]] = [
    {
        "id": "brush-general",
        "name": "通用刷流",
        "badge": "",
        "desc": "不绑定站点：你有几个站就套几个。小水管与盒子都能用，上限保守。",
        "highlights": ["并发 5", "体积 10-100 GiB", "做种 12 小时", "盒子低速上传：关"],
        "brush": {
            "brush_interval": 10,
            "check_interval": 5,
            "max_downloading_torrents": 5,
            "min_disk_space": "120GiB",
            "download_speed_min": 2.0,
            "slow_delete_speed": 1.0,
            "slow_delete_minutes": 15,
            "slow_up_delete_enabled": False,
            "slow_up_delete_minutes": 0,
            "slow_up_delete_speed": 0.1,
            "max_no_activate_time": 0,
            "free_end_delete_minutes": 20,
            "promotions": ["free", "free_2x"],
            "exclude_hr": True,
            "min_free_hours": 2,
            "size": "10-100",
            "seeder": "20",
            "pubtime": "60",
            "seed_time": 12,
            "hr_seed_time": 72,
            "seed_ratio": 1.0,
        },
    },
    {
        "id": "seed-first",
        "name": "保种优先",
        "badge": "",
        "desc": "删种更慢、分享率门槛更高，适合号上保种多、怕误删的场景。",
        "highlights": ["并发 3", "体积 30-150 GiB", "做种 24 小时", "盒子低速上传：关"],
        "brush": {
            "brush_interval": 15,
            "check_interval": 10,
            "max_downloading_torrents": 3,
            "min_disk_space": "180GiB",
            "download_speed_min": 1.0,
            "slow_delete_speed": 0.8,
            # 必须短于 download_cycle（默认 20 分钟），否则会先让出下载位、种子还没删
            "slow_delete_minutes": 15,
            "slow_up_delete_enabled": False,
            "slow_up_delete_minutes": 0,
            "slow_up_delete_speed": 0.1,
            "max_no_activate_time": 0,
            "free_end_delete_minutes": 10,
            "promotions": ["free", "free_2x"],
            "exclude_hr": True,
            "min_free_hours": 2,
            "size": "30-150",
            "seeder": "10-40",
            "pubtime": "120",
            "seed_time": 24,
            "hr_seed_time": 72,
            "seed_ratio": 2.0,
        },
    },
    {
        "id": "big-box",
        "name": "大硬盘盒子",
        "badge": "",
        "desc": "优先大体积、放宽做种人数下限，配合高上传带宽跑量。",
        "highlights": ["并发 10", "体积 60-300 GiB", "做种 6 小时", "盒子低速上传：开"],
        "brush": {
            "brush_interval": 5,
            "check_interval": 5,
            "max_downloading_torrents": 10,
            # 体积上限 300 GiB，磁盘下限必须高于它，避免只剩 50 GiB 还挑 300 GiB 的包
            "min_disk_space": "350GiB",
            "download_speed_min": 3.0,
            "slow_delete_speed": 1.5,
            "slow_delete_minutes": 10,
            "slow_up_delete_enabled": True,
            "slow_up_delete_minutes": 15,
            "slow_up_delete_speed": 0.1,
            "max_no_activate_time": 0,
            "free_end_delete_minutes": 30,
            "promotions": ["free", "free_2x"],
            "exclude_hr": True,
            "min_free_hours": 2,
            "size": "60-300",
            "seeder": "1-60",
            "pubtime": "30",
            "seed_time": 6,
            "hr_seed_time": 72,
            "seed_ratio": 0.8,
        },
    },
]


def preset_list() -> List[Dict[str, Any]]:
    result = []
    for pack in PRESETS:
        item = {k: v for k, v in pack.items() if k not in ("brush", "rules", "task")}
        # 低速上传开关 / 阈值 / 分钟在界面上合并成 1 项，所以可调项比配置键少 2
        item["params"] = len(pack.get("brush") or {}) - 2
        result.append(item)
    return result


def get_preset(preset_id: str) -> Optional[Dict[str, Any]]:
    for pack in PRESETS:
        if pack["id"] == preset_id:
            return pack
    return None
