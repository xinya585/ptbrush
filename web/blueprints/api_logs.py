"""运行日志接口。

日志页读的是容器里的 loguru 文件（默认 /app/data/ptbrush.log，挂在宿主 data 目录），
不用 docker logs：进程自己的滚动文件更完整，也方便按级别/关键字过滤。

路由：
  GET /api/logs        尾部日志（?lines=300&level=ERROR&q=关键字&file=）
  GET /api/logs/files  可读的日志文件列表（含滚动出来的历史文件）
"""
import re
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request

from web.auth import login_required

api_logs_bp = Blueprint("api_logs", __name__)

from paths import data_dir

# web/blueprints/api_logs.py -> 数据目录（容器 /app/data，便携版 exe 同级 data）
DATA_DIR = data_dir()

# loguru 默认格式：2026-11-15 00:14:40.123 | INFO     | tasks.services:brush:123 - xxx
_LEVEL_RE = re.compile(r"\|\s*(TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL)\s*\|")
# 一次最多从文件尾部读多少字节：足够覆盖上千行，又不会把日志全塞进内存
_TAIL_BYTES = 1024 * 1024
_MAX_LINES = 5000


def _log_files():
    """按修改时间倒序返回日志文件（含 loguru rotation 出来的历史文件）。"""
    try:
        items = [p for p in DATA_DIR.glob("ptbrush*.log*") if p.is_file()]
        items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    return items


def _pick_file(name: str = ""):
    files = _log_files()
    if not files:
        return None
    name = (name or "").strip()
    if name:
        for p in files:
            if p.name == name or str(p) == name:
                return p
        # 只给了主文件名也允许
        for p in files:
            if p.name.startswith(name):
                return p
    return files[0]


def _tail_text(path: Path) -> str:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > _TAIL_BYTES:
            fh.seek(size - _TAIL_BYTES)
            fh.readline()  # 丢掉可能被截断的第一行
        return fh.read().decode("utf-8", "replace")


def _parse_level(text: str) -> str:
    found = _LEVEL_RE.search(text)
    return found.group(1) if found else ""


def _int_arg(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


@api_logs_bp.route("/api/logs", methods=["GET"])
@login_required
def get_logs():
    path = _pick_file(request.args.get("file", ""))
    if path is None:
        return jsonify({
            "rows": [],
            "count": 0,
            "file": "",
            "files": [],
            "message": "还没有日志文件，等第一次任务跑起来就有了",
        })

    want = _int_arg("lines", 300, 20, _MAX_LINES)
    level = (request.args.get("level") or "").strip().upper()
    keyword = (request.args.get("q") or "").strip().lower()

    text = _tail_text(path)
    all_rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        row_level = _parse_level(line)
        if level and level != "ALL" and level != row_level:
            continue
        if keyword and keyword not in line.lower():
            continue
        all_rows.append({"text": line, "level": row_level})

    matched = len(all_rows)
    rows = all_rows[-want:]

    try:
        stat = path.stat()
        size = stat.st_size
        updated_at = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        size = 0
        updated_at = ""

    files = []
    for p in _log_files():
        try:
            files.append({
                "name": p.name,
                "size": p.stat().st_size,
                "updated_at": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
        except OSError:
            continue

    return jsonify({
        "rows": rows,
        "count": len(rows),
        "matched": matched,
        "file": path.name,
        "size": size,
        "updated_at": updated_at,
        "files": files,
        "level": level if level != "ALL" else "",
        "keyword": keyword,
    })


@api_logs_bp.route("/api/logs/files", methods=["GET"])
@login_required
def get_log_files():
    files = []
    for p in _log_files():
        try:
            files.append({
                "name": p.name,
                "size": p.stat().st_size,
                "updated_at": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
        except OSError:
            continue
    return jsonify({"files": files})
