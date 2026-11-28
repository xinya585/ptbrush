"""自动更新接口。

  GET  /api/update/status    当前状态（是否已有新版本、是否已下载）
  POST /api/update/check     立刻并发拉三个节点的版本清单，自动挑最快的
  POST /api/update/download  按用户选的下载节点下载并校验（失败自动换节点）
  POST /api/update/apply     启动替换脚本并退出程序（仅 Windows 便携版）
  GET  /api/update/nodes     两个下载节点 + 可选的连通性探测
  POST /api/update/node      保存「下载节点」选择（写 config.toml 的 [update]）
"""

import re
import sys
import time

import requests
from flask import Blueprint, jsonify, request
from loguru import logger

import update
from config.config import PTBrushConfig
from web.auth import login_required
from web.config_io import atomic_write, read_raw

api_update_bp = Blueprint("api_update", __name__)

_IP_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_URL_RE = re.compile(r"https?://[^\s'\"\)\],]+")


def _brief_error(e: Exception) -> str:
    """把节点探测异常的原文压成一句人话，顺带抹掉地址，界面上不出现具体服务器。"""
    if isinstance(e, requests.exceptions.Timeout):
        return "连接超时"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "连接失败"
    msg = (str(e) or type(e).__name__).splitlines()[0]
    msg = _URL_RE.sub("节点", msg)
    msg = _IP_RE.sub("", msg).strip(" :,'\"-")
    return msg[:40] or "不可用"


@api_update_bp.route("/api/update/status", methods=["GET"])
def update_status():
    snap = update.state()
    snap["manifest"] = update.manifest_url()
    snap.setdefault("node", update.selected_node())
    snap["deploy"] = update.deploy_mode()
    snap["upgradeable"] = update.upgradeable()
    return jsonify(snap)


@api_update_bp.route("/api/update/check", methods=["POST"])
@login_required
def update_check():
    return jsonify(update.check())


@api_update_bp.route("/api/update/download", methods=["POST"])
@login_required
def update_download():
    mode = update.deploy_mode()
    if mode == "docker":
        # 容器里代码在镜像内，下载了也替换不了自己，白占 30MB，直接说清楚怎么升
        return jsonify({
            "error": "容器版不支持下载安装包升级，请更新镜像：docker pull xinya585/ptbrush:latest 后重建容器",
        }), 400
    if mode == "source":
        return jsonify({"error": "源码运行方式不支持自动下载升级包"}), 400
    try:
        return jsonify(update.download())
    except Exception as e:
        logger.warning(f"下载更新失败: {e}")
        return jsonify({"error": str(e)}), 400


@api_update_bp.route("/api/update/apply", methods=["POST"])
@login_required
def update_apply():
    try:
        update.apply()
    except Exception as e:
        logger.warning(f"应用更新失败: {e}")
        return jsonify({"error": str(e)}), 400
    message = "升级脚本已启动，程序即将退出并自动重启"
    if update.deploy_mode() == "linux":
        message = "程序文件已替换，正在重启服务"
    return jsonify({"ok": True, "message": message})


@api_update_bp.route("/api/update/oneclick", methods=["POST"])
@login_required
def update_oneclick():
    """Linux 原生安装版：下载 → 校验 → 替换 → 重启，一次点完。"""
    mode = update.deploy_mode()
    if mode != "linux":
        return jsonify({"error": "一键升级只在 Linux 原生安装版可用"}), 400
    try:
        update.download()
        update.apply()
    except Exception as e:
        logger.warning(f"Linux 一键升级失败: {e}")
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "message": "安装包已下载并校验通过，正在替换程序文件"})


@api_update_bp.route("/api/update/nodes", methods=["GET"])
@login_required
def update_nodes():
    """两个下载节点；probe=1 时顺带测一次每个节点的清单延迟。"""
    probe = request.args.get("probe") in ("1", "true", "yes")
    current = update.selected_node()
    rows = []
    for key in update.NODE_ORDER:
        node = update.NODES[key]
        row = {
            "key": key,
            "label": node["label"],
            "current": key == current,
        }
        if probe:
            started = time.time()
            try:
                resp = requests.head(node["manifest"], timeout=5, allow_redirects=True)
                row["latency"] = round((time.time() - started) * 1000)
                row["ok"] = resp.status_code < 400
                if not row["ok"]:
                    row["error"] = f"HTTP {resp.status_code}"
            except Exception as e:  # noqa: BLE001
                row["ok"] = False
                row["latency"] = None
                row["error"] = _brief_error(e)
        rows.append(row)
    return jsonify({"current": current, "nodes": rows})


@api_update_bp.route("/api/update/node", methods=["POST"])
@login_required
def update_node():
    body = request.get_json(silent=True) or {}
    key = str(body.get("node") or "").strip().lower()
    if key not in update.NODES:
        return jsonify({"error": "节点只能是 cf / ov"}), 400

    raw = read_raw()
    section = dict(raw.get("update") or {})
    section["download_node"] = key
    raw["update"] = section
    try:
        PTBrushConfig.model_validate(raw)
    except Exception as e:
        return jsonify({"error": f"配置校验失败：{e}"}), 400
    try:
        atomic_write(raw)
    except Exception as e:
        return jsonify({"error": f"保存失败：{e}"}), 500

    label = update.NODES[key]["label"]
    logger.info(f"下载节点已切换为 {label}（{key}）")
    return jsonify({"ok": True, "node": key, "message": f"下载节点已切换为「{label}」"})

