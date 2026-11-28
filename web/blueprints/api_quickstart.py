"""快速开始与刷流参数套餐接口。

策略页和规则引擎已经移除，这里只保留：
  GET  /api/presets               套餐列表
  POST /api/presets/<id>/preview  参数差异预览
  POST /api/presets/<id>/import   应用参数
  GET  /api/quickstart/summary    快速开始状态
"""
from flask import Blueprint, jsonify

from config.config import PTBrushConfig
from web.auth import login_required
from web.preset_config import GROUPS, apply_preset, matching_preset_id, preview_preset
from web.presets import get_preset, preset_list

api_quickstart_bp = Blueprint("api_quickstart", __name__)


@api_quickstart_bp.route("/api/presets", methods=["GET"])
@login_required
def api_presets():
    cfg = PTBrushConfig()
    site_count = len(cfg.sites or [])
    return jsonify({"presets": preset_list(), "sites": site_count})


@api_quickstart_bp.route("/api/presets/<preset_id>/preview", methods=["POST"])
@login_required
def api_preset_preview(preset_id):
    pack = get_preset(preset_id)
    if not pack:
        return jsonify({"error": "套餐不存在"}), 404
    return jsonify(preview_preset(pack, PTBrushConfig()))


@api_quickstart_bp.route("/api/presets/<preset_id>/import", methods=["POST"])
@login_required
def api_preset_import(preset_id):
    pack = get_preset(preset_id)
    if not pack:
        return jsonify({"error": "套餐不存在"}), 404
    return jsonify(apply_preset(pack))


@api_quickstart_bp.route("/api/quickstart/summary", methods=["GET"])
@login_required
def api_quickstart_summary():
    cfg = PTBrushConfig()
    sites = list(cfg.sites or [])
    total = len(sites)
    cookie_ok = sum(1 for s in sites if (s.cookie or s.api_key or s.token or s.public))
    downloaders = cfg.downloader_list()
    preset_id = matching_preset_id(cfg)
    preset = get_preset(preset_id) if preset_id else None
    save_path_ready = any(bool(getattr(d, "save_path", "")) for d in downloaders)
    return jsonify({
        "sites": {"total": total, "cookie_ok": cookie_ok},
        "downloaders": {"total": len(downloaders),
                        "alias": downloaders[0].alias if downloaders else ""},
        "brush": {"preset_id": preset_id, "preset_name": preset["name"] if preset else "",
                  "matched": bool(preset_id), "params": len(GROUPS)},
        "steps": [
            {"key": "downloader", "label": "下载器地址与账号", "done": len(downloaders) > 0,
             "detail": downloaders[0].alias if downloaders else "未配置", "to": "/downloader"},
            {"key": "sites", "label": "添加你自己的站点", "done": total > 0,
             "detail": f"{total} 个", "to": "/sites"},
            {"key": "cookies", "label": "站点 Cookie", "done": total > 0 and cookie_ok >= total,
             "detail": f"{cookie_ok} / {total} 已填" if total else "加站时填", "to": "/sites"},
            {"key": "brush", "label": "套用一套刷流参数", "done": bool(preset_id),
             "detail": preset["name"] if preset else "未套用", "to": "/start"},
            {"key": "save_path", "label": "选择保存目录", "done": save_path_ready,
             "detail": "已设置" if save_path_ready else "用下载器默认目录", "to": "/brush"},
        ],
    })
