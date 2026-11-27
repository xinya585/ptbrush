"""系统设置：CookieCloud 凭据 + Web 访问密码。

配置备份功能已下线（不再生成快照、不提供下载与恢复），data/backups 目录也不再写入。

页面 `/security` 对应的接口：

  GET    /api/security                      初始数据（CookieCloud 凭据脱敏 + Web 登录状态）
  PUT    /api/security/cookiecloud          只改 [cookiecloud] 段，不碰 brush / sites / downloaders
"""
from flask import Blueprint, jsonify, request

from config.config import PTBrushConfig
from web.auth import is_login_required, login_required
from web.config_io import atomic_write, read_raw
from web.config_serializer import MASKED_SENTINEL, mask

api_security_bp = Blueprint("api_security", __name__)


def _serialize_cookiecloud(raw: dict) -> dict:
    cc = raw or {}
    return {
        "server": cc.get("server", "") or "",
        "uuid": cc.get("uuid", "") or "",
        "password": mask(cc.get("password", "") or ""),
    }


@api_security_bp.route("/api/security", methods=["GET"])
@login_required
def get_security():
    try:
        PTBrushConfig()  # 能读通就说明 toml 本身没问题
    except Exception as e:
        return jsonify({"error": f"读取配置失败：{e}"}), 500
    raw = read_raw().get("cookiecloud") or {}
    return jsonify({
        # 以 toml 原文为准，避免 mask 逻辑把 password 变成 *** 之后看不出到底有没有设置
        "cookiecloud": _serialize_cookiecloud(raw),
        "web": {"login_required": is_login_required()},
    })


@api_security_bp.route("/api/security/cookiecloud", methods=["PUT"])
@login_required
def update_cookiecloud():
    body = request.get_json(silent=True) or {}
    server = (body.get("server") or "").strip()
    uuid = (body.get("uuid") or "").strip()
    password = (body.get("password") or "").strip()

    if server and not server.startswith(("http://", "https://")):
        return jsonify({"error": "CookieCloud 地址必须以 http:// 或 https:// 开头"}), 400

    raw = read_raw()
    old = raw.get("cookiecloud") or {}
    if password in ("", MASKED_SENTINEL):
        password = old.get("password", "") or ""
    raw["cookiecloud"] = {"server": server, "uuid": uuid, "password": password}

    try:
        PTBrushConfig.model_validate(raw)
    except Exception as e:
        return jsonify({"error": f"配置校验失败：{e}"}), 400

    try:
        atomic_write(raw)
    except Exception as e:
        return jsonify({"error": f"保存失败：{e}"}), 500
    return jsonify({
        "ok": True,
        "message": "CookieCloud 凭据已保存",
        "cookiecloud": _serialize_cookiecloud(raw["cookiecloud"]),
    })
