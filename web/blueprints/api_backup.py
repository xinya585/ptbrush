"""备份与恢复。

  GET  /api/backup/info      备份范围（会打包哪些文件）、历史备份列表
  GET  /api/backup/export    打包并下载（config.toml + ptbrush.db + 其它状态 json）
  POST /api/backup/import    上传备份包并恢复（导入前自动备份当前数据，导入后需重启生效）
  POST /api/backup/restart   重启进程（容器依赖 restart 策略，Windows 便携版自启）

备份包结构（zip）：
  meta.json     版本 / 导出时间 / 文件清单
  config.toml   站点、下载器、刷流、Web、CookieCloud 等全部配置
  ptbrush.db    站点账号快照、统计、图标、种子与删种记录（VACUUM 快照，一致导出）
  search.json   搜片站点开关与 TMDB 配置
  ptlist_cache.json  PT 站点名字缓存（可选）
日志、升级包、历史备份不进包。
"""
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file
from loguru import logger

try:
    import tomllib  # py3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from paths import data_dir
from version import get_version
from web.auth import login_required

api_backup_bp = Blueprint("api_backup", __name__)

BACKUP_PREFIX = "ptbrush-backup"
MAX_IMPORT_BYTES = 300 * 1024 * 1024     # 上传包上限
MAX_UNPACK_BYTES = 800 * 1024 * 1024     # 解压后总大小上限
KEEP_BACKUPS = 10                        # data/backups 里最多留几份


def _db_path() -> Path:
    return data_dir() / "ptbrush.db"


def _backup_dir() -> Path:
    path = data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _collect_files() -> list:
    """data 目录里要进备份的文件：日志、升级包、历史备份、sqlite 临时文件都不带。"""
    root = data_dir()
    rows = []
    for p in root.iterdir():
        if not p.is_file():
            continue
        if p.name.startswith("ptbrush.db"):      # 数据库单独走 VACUUM 快照
            continue
        if p.name.startswith("ptbrush.log") or p.suffix == ".log":
            continue
        if p.suffix in (".part", ".tmp", ".lock"):
            continue
        rows.append(p)
    return sorted(rows, key=lambda x: x.name)


def _vacuum_into(src: Path, dest: Path) -> None:
    """SQLite 一致性快照：VACUUM INTO 会等写完并生成一份可独立打开的库。"""
    con = sqlite3.connect(str(src))
    try:
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001
            pass
        quoted = str(dest).replace("'", "''")
        con.execute(f"VACUUM INTO '{quoted}'")
    finally:
        con.close()


def _zip_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def write_backup(dest: Path, reason: str = "manual") -> dict:
    """把当前数据打进 dest（zip），返回 meta。"""
    files = []
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        db = _db_path()
        if db.exists():
            with tempfile.TemporaryDirectory(prefix="ptbrush-snap-") as td:
                snap = Path(td) / "ptbrush.db"
                try:
                    _vacuum_into(db, snap)
                    zf.write(snap, "ptbrush.db")
                    files.append({"name": "ptbrush.db", "size": snap.stat().st_size})
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"数据库快照失败，改用直接拷贝：{e}")
                    zf.write(db, "ptbrush.db")
                    files.append({"name": "ptbrush.db", "size": db.stat().st_size})
        for p in _collect_files():
            zf.write(p, p.name)
            files.append({"name": p.name, "size": _zip_size(p)})

        meta = {
            "app": "ptbrush",
            "version": get_version(),
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "reason": reason,
            "files": files,
        }
        zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
    meta["archive"] = dest.name
    meta["archive_size"] = _zip_size(dest)
    return meta


def _prune_backups() -> None:
    rows = sorted(_backup_dir().glob(f"{BACKUP_PREFIX}-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in rows[KEEP_BACKUPS:]:
        try:
            p.unlink()
        except OSError:
            pass


def _list_backups() -> list:
    rows = []
    for p in sorted(_backup_dir().glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True):
        rows.append({
            "name": p.name,
            "size": _zip_size(p),
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime)),
        })
    return rows[:KEEP_BACKUPS]


@api_backup_bp.route("/api/backup/info", methods=["GET"])
@login_required
def backup_info():
    files = []
    db = _db_path()
    if db.exists():
        files.append({"name": "ptbrush.db", "size": _zip_size(db), "desc": "站点账号快照 / 统计 / 图标 / 种子记录"})
    for p in _collect_files():
        files.append({"name": p.name, "size": _zip_size(p), "desc": ""})
    return jsonify({
        "version": get_version(),
        "data_dir": str(data_dir()),
        "files": files,
        "backups": _list_backups(),
    })


@api_backup_bp.route("/api/backup/export", methods=["GET"])
@login_required
def backup_export():
    name = f"{BACKUP_PREFIX}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    dest = _backup_dir() / name
    try:
        meta = write_backup(dest, reason="export")
    except Exception as e:  # noqa: BLE001
        logger.exception("导出备份失败")
        return jsonify({"error": f"打包失败：{e}"}), 500
    _prune_backups()
    logger.info(f"已导出备份 {name}（{meta['archive_size'] // 1024} KB，{len(meta['files'])} 个文件）")
    return send_file(
        str(dest),
        mimetype="application/zip",
        as_attachment=True,
        download_name=name,
        max_age=0,
    )


def _safe_extract(zf: zipfile.ZipFile, dest: Path, limit: int = MAX_UNPACK_BYTES) -> list:
    """按白名单解压到 dest；拒绝绝对路径和 .. 越界，并按实际写出量卡总量（防伪造 size 的压缩包）。"""
    names = []
    written = 0
    for info in zf.infolist():
        name = info.filename
        if not name or name.endswith("/"):
            continue
        parts = Path(name).parts
        if name.startswith("/") or name.startswith("\\") or ".." in parts:
            raise ValueError(f"备份包里有非法路径：{name}")
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise ValueError(f"解压后超过 {limit // 1048576} MB，拒绝导入")
                out.write(chunk)
        names.append(name)
    return names


@api_backup_bp.route("/api/backup/import", methods=["POST"])
@login_required
def backup_import():
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "请选择备份文件（.zip）"}), 400

    payload = upload.read()
    if not payload:
        return jsonify({"error": "备份文件是空的"}), 400
    if len(payload) > MAX_IMPORT_BYTES:
        return jsonify({"error": f"备份文件超过 {MAX_IMPORT_BYTES // 1048576} MB，拒绝导入"}), 400

    try:
        zf = zipfile.ZipFile(io.BytesIO(payload))
    except Exception:  # noqa: BLE001
        return jsonify({"error": "不是有效的 zip 备份包"}), 400

    infos = zf.infolist()
    unpack_total = sum(max(0, i.file_size) for i in infos)
    if unpack_total > MAX_UNPACK_BYTES:
        return jsonify({"error": "解压后超过 800 MiB，拒绝导入"}), 400

    names = {i.filename for i in infos if not i.filename.endswith("/")}
    if "config.toml" not in names:
        return jsonify({"error": "这个包里没有 config.toml，不像是 PTBrush 的备份"}), 400

    tmp = Path(tempfile.mkdtemp(prefix="ptbrush-import-"))
    try:
        try:
            extracted = _safe_extract(zf, tmp)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        # 1) 校验备份里的配置能解析
        from config.config import PTBrushConfig

        try:
            cfg_text = (tmp / "config.toml").read_bytes()
            cfg_dict = tomllib.loads(cfg_text.decode("utf-8"))
            PTBrushConfig.model_validate(cfg_dict)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"备份里的 config.toml 不合法：{e}"}), 400

        # 2) 导入前先把当前数据整体备份一份
        auto_name = f"{BACKUP_PREFIX}-auto-before-import-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        auto_path = _backup_dir() / auto_name
        try:
            write_backup(auto_path, reason="before-import")
            logger.info(f"导入前已自动备份当前数据：{auto_name}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"导入前自动备份失败（继续导入）：{e}")
            auto_name = ""

        # 3) 关闭数据库连接后替换文件（Windows 下打开着的 db 是换不掉的）
        db_closed = False
        try:
            from db import database

            database.close()
            db_closed = True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"关闭数据库连接失败：{e}")

        replaced = []
        root = data_dir()
        applied = []        # 已替换成功的目标文件名，失败时按这份清单回滚

        def _put(src: Path, dst: Path) -> None:
            shutil.copy2(src, dst)
            applied.append(dst.name)

        try:
            _put(tmp / "config.toml", root / "config.toml")
            replaced.append("config.toml")

            src_db = tmp / "ptbrush.db"
            if src_db.exists():
                for suffix in ("-wal", "-shm", "-journal"):
                    (root / f"ptbrush.db{suffix}").unlink(missing_ok=True)
                _put(src_db, _db_path())
                replaced.append("ptbrush.db")

            for name in extracted:
                if name in ("config.toml", "ptbrush.db", "meta.json"):
                    continue
                if "/" in name:
                    continue
                # 数据库的 wal/shm/journal 混进来会和刚换的 db 打架，直接跳过
                if name.startswith("ptbrush.db"):
                    continue
                _put(tmp / name, root / name)
                replaced.append(name)
        except Exception as e:  # noqa: BLE001
            logger.exception("导入替换阶段出错，尝试回滚")
            rolled = False
            if auto_name and auto_path.exists():
                try:
                    with tempfile.TemporaryDirectory(prefix="ptbrush-rollback-") as td2:
                        with zipfile.ZipFile(auto_path) as bz:
                            _safe_extract(bz, Path(td2))
                        for name in applied:
                            src2 = Path(td2) / name
                            if src2.exists():
                                if name.startswith("ptbrush.db"):
                                    for suffix in ("-wal", "-shm"):
                                        (root / f"ptbrush.db{suffix}").unlink(missing_ok=True)
                                shutil.copy2(src2, root / name)
                    rolled = True
                    logger.warning(f"导入失败，已用 {auto_name} 回滚 {len(applied)} 个文件")
                except Exception as e2:  # noqa: BLE001
                    logger.error(f"回滚失败，数据可能不一致：{e2}")
            return jsonify({
                "error": f"替换文件失败：{e}" + ("，已回滚到导入前数据" if rolled else "，请手动恢复备份"),
                "replaced": replaced,
            }), 500

        # 4) 清缓存；调度器里的站点对象要重启才会重新加载
        try:
            from ptsite.profile import clear_cache
            from ptsite import throttle

            clear_cache()
            throttle.reset()
        except Exception:  # noqa: BLE001
            pass

        if db_closed:
            try:
                from db import database

                database.connection()
            except Exception:  # noqa: BLE001
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    logger.info(f"已导入备份：替换 {len(replaced)} 个文件（{', '.join(replaced)}）")
    return jsonify({
        "ok": True,
        "need_restart": True,
        "replaced": replaced,
        "auto_backup": auto_name,
        "message": "配置已导入，重启 PTBrush 后全部生效",
    })


def _restart_process() -> None:
    time.sleep(0.8)
    try:
        if getattr(sys, "frozen", False) and sys.platform == "win32":
            subprocess.Popen([sys.executable], close_fds=True, cwd=str(Path(sys.executable).parent))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"重启前拉起新进程失败：{e}")
    logger.info("按用户请求重启进程")
    os._exit(0)


@api_backup_bp.route("/api/backup/restart", methods=["POST"])
@login_required
def backup_restart():
    """重启进程。容器里依赖 --restart 策略被 docker 拉起；Windows 便携版会自己再启动一个。"""
    threading.Thread(target=_restart_process, daemon=True).start()
    return jsonify({"ok": True, "message": "正在重启，约 5 秒后刷新页面"})
