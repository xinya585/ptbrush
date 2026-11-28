#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
    @File    :   main.py
    @Time    :   2024/11/05 14:38:04
    @Author  :   huihuidehui 
    @Version :   1.0
'''

import argparse
import os
import sys
import threading
import time
import webbrowser

# 数据目录必须在导入业务模块之前定下来：db / config 在 import 时就按它算好了路径。
_cli = argparse.ArgumentParser(prog="PTBrush", description="PT 站点刷流工具")
_cli.add_argument("--port", type=int, default=None, help="Web 端口（默认 44445，容器里沿用 WEB_PORT）")
_cli.add_argument("--data", default=None, help="数据目录（默认程序同级的 data 目录）")
_cli.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
_cli.add_argument("--no-tray", action="store_true", help="不显示托盘图标（Windows 桌面版）")
_cli.add_argument("--autostart", choices=["on", "off"], default=None, help="开启/关闭开机自启后退出")
_cli.add_argument("--console", action="store_true", help="显示控制台窗口（默认隐藏，仅 Windows）")
_cli.add_argument("--version", action="store_true", help="打印版本号后退出")
_cli.add_argument("--check-update", action="store_true", help="打开检查更新窗口（Windows 桌面版），查完即退出")
ARGS, _ = _cli.parse_known_args()

from paths import data_dir, hide_portable_internal, is_frozen, set_data_dir

if ARGS.data:
    set_data_dir(ARGS.data)

if os.name == "nt":
    # 窗口化打包默认没有控制台：--console / --version 时再接管一个
    if ARGS.console or ARGS.version:
        try:
            from win_desktop import attach_console

            attach_console()
        except Exception:
            pass
    if sys.stdout is None or sys.stderr is None:
        # 没有标准流时重定向到文件，避免 print / logging 往 None 上写而报错
        try:
            _console_log = open(
                data_dir() / "console.log", "w", encoding="utf-8", errors="replace", buffering=1
            )
            sys.stdout = sys.stdout or _console_log
            sys.stderr = sys.stderr or _console_log
        except OSError:
            pass
    try:
        # 控制台默认是 GBK，日志里的中文会变乱码；切到 UTF-8
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from loguru import logger
import tasks as tasks
from config.config import BrushConfig, PTBrushConfig
from version import get_version
from web.server import start_web_server_thread
from web.runtime import set_scheduler, BRUSH_JOB_ID, CHECK_JOB_ID
from db import migrate_database

if ARGS.version:
    print(get_version())
    sys.exit(0)

if ARGS.check_update:
    if os.name != "nt":
        print("--check-update 只在 Windows 桌面版可用")
        sys.exit(1)
    from win_desktop import run_update_check

    run_update_check()
    sys.exit(0)

DEFAULT_PORT = 44445

# 设置不打印 debug 级别的日志，最小级别为 INFO
logger.remove()  # 移除默认的 handler
logger.add(data_dir() / "ptbrush.log", rotation="10 MB", retention="10 days", level="INFO")
if is_frozen():
    # 便携版没有 docker logs，控制台留一份镜像，方便看启动情况
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level: <8} | {message}")

def check_work_time(brush_config:BrushConfig):
    """检查当前是否在工作时间内"""
    
    if not brush_config.is_work_time():
        logger.info("当前不在工作时间范围内，跳过任务执行")
        return False
    return True

def run_if_work_time(func):
    """只在工作时间内运行的装饰器"""

    def wrapper():
        config = PTBrushConfig()
        if check_work_time(config.brush):
            func()
    return wrapper

def main():
    # 便携版：把 _internal 藏起来，用户翻目录时不至于误删一堆运行库
    if hide_portable_internal():
        logger.info("已把 _internal 设为隐藏")

    # 确保数据库结构是最新的
    migrate_database()
    
    # Start web server
    if ARGS.port:
        web_port = ARGS.port
    elif os.environ.get("WEB_PORT"):
        web_port = int(os.environ["WEB_PORT"])
    else:
        # 容器外（便携版）给一个好记的默认端口
        web_port = DEFAULT_PORT if is_frozen() else 8000

    is_windows_desktop = os.name == "nt"
    if is_windows_desktop:
        from win_desktop import (
            acquire_single_instance,
            read_port,
            remember_port,
            set_autostart,
            start_tray,
        )

        # --autostart on/off：只改注册表，不启动服务
        if ARGS.autostart:
            sys.exit(0 if set_autostart(ARGS.autostart == "on") else 1)

        # 已经有一个在跑（比如开机自启那个）：直接把界面打开，不重复占端口
        if not acquire_single_instance():
            existing = read_port() or web_port
            logger.warning(f"已有 PTBrush 在运行，打开现有界面: http://127.0.0.1:{existing}")
            webbrowser.open(f"http://127.0.0.1:{existing}")
            sys.exit(0)

    web_thread = start_web_server_thread(port=web_port)
    host = f"http://127.0.0.1:{web_port}"
    logger.info(f"Web界面已启动: {host}")
    logger.info(f"数据目录: {data_dir()}")

    if is_windows_desktop:
        remember_port(web_port)
        if not ARGS.no_tray:
            start_tray(web_port)

    if ARGS.open:
        threading.Timer(1.5, lambda: webbrowser.open(host)).start()
        logger.info("已尝试打开浏览器，若没有弹出请手动访问上面的地址")

    if is_frozen():
        # 便携版 / Linux 原生安装才有自动检查更新；容器里升级走换镜像，不需要程序自己换文件
        from update import auto_check_loop, deploy_mode

        if deploy_mode() != "docker":
            stop_update = threading.Event()
            threading.Thread(
                target=auto_check_loop, args=(stop_update,), name="ptbrush-update", daemon=True
            ).start()
    
    # PTBRUSH_WEB_ONLY=1：只跑 Web（预览/调界面用），刷流、删种、统计全部不挂调度。
    # 预览容器要是连了同一台下载器，会和生产容器抢着删种，必须能单独关掉后台任务。
    web_only = str(os.environ.get("PTBRUSH_WEB_ONLY") or "").strip().lower() in ("1", "true", "yes", "on")
    if web_only:
        logger.info("PTBRUSH_WEB_ONLY=1：只提供 Web 界面，刷流/删种/统计等后台任务全部跳过")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            logger.info("收到退出信号")
        return

    executors = {"default": ThreadPoolExecutor(max_workers=6)}
    job_defaults = {"coalesce": True, "max_instances": 1}
    scheduler = BlockingScheduler(
        executors=executors, job_defaults=job_defaults)
        
    # 每10分钟执行一次种子瘦身
    scheduler.add_job(
        tasks.torrent_thinned, 
        "cron", 
        minute="*/10"
    )
    
    # 刷流任务：间隔可配置（默认 10 分钟，最小 5 分钟），受刷流任务工作时间设置
    brush_interval = 10
    try:
        brush_interval = max(5, int(PTBrushConfig().brush.brush_interval))
    except Exception as e:
        logger.warning(f"读取刷流间隔配置失败，按默认 10 分钟执行: {e}")
    scheduler.add_job(
        run_if_work_time(tasks.brush),
        "interval",
        minutes=brush_interval,
        id=BRUSH_JOB_ID,
        replace_existing=True
    )
    # 注册到 runtime，Web 端保存配置后可以即时重设间隔，不必重启容器
    set_scheduler(scheduler)
    logger.info(f"刷流任务间隔: {brush_interval} 分钟")
    
    # 删种与状态检查：独立周期（check_interval，默认 5 分钟）。
    # 一次跑完「即将过期清理 + 无活跃清理 + 条件删种 + 动态删种」，
    # 省得每个清理各占一个调度槽，周期也能在 Web 端改完即时生效。
    check_interval = 5
    try:
        check_interval = max(1, int(PTBrushConfig().brush.check_interval))
    except Exception as e:
        logger.warning(f"读取检查周期配置失败，按默认 5 分钟执行: {e}")
    scheduler.add_job(
        tasks.check_brush_torrents,
        "interval",
        minutes=check_interval,
        id=CHECK_JOB_ID,
        replace_existing=True
    )
    logger.info(f"删种与状态检查周期: {check_interval} 分钟")
    
    # 每15秒记录一次QB状态
    scheduler.add_job(
        tasks.fetch_qb_status, 
        "cron", 
        second="*/15"
    )
    
    # 每分钟记录一次QB中的种子状态
    scheduler.add_job(
        tasks.fetch_qb_torrents, 
        "cron", 
        minute="*"
    )
    
    # 每 10 分钟抓一次 PT 站的种子（候选库）。
    # 页面上的「刷流间隔」只管「多久从候选库里挑种加进下载器」，
    # 抓种太慢会让 5 分钟的挑种一直在啃 30 分钟前的旧数据。
    scheduler.add_job(
        tasks.fetch_pt_torrents,
        "interval",
        minutes=10,
        id="fetch_pt_torrents",
        replace_existing=True
    )
    
    # 每 7 天对数据库执行一次 VACUUM 瘦身（从 clean_long_time_no_activate_torrents 拆出，
    # 原先每分钟一次会导致整库重写约 ~120MB，对 SSD 寿命冲击极大）
    scheduler.add_job(
        tasks.vacuum_database,
        "interval",
        days=7
    )

    # 每 6 小时刷新一次站点账号数据（用户名/上传下载量/分享率/做种数 + 图标缓存）。
    # 启动 2 分钟后就先跑一次，新部署的容器不用干等 6 小时才有数据。
    scheduler.add_job(
        tasks.refresh_site_data,
        "interval",
        hours=6,
        next_run_time=datetime.now() + timedelta(seconds=120),
        id="site_data_refresh",
        replace_existing=True
    )

    # 每 10 分钟只刷「正在执行的刷流站点」的账号数据：
    # 仪表盘那张卡每 10 分钟读一次 siteuserdata，全量 14 站仍是 6 小时一次，这里只补参与站点。
    scheduler.add_job(
        tasks.refresh_brush_sites,
        "interval",
        minutes=10,
        next_run_time=datetime.now() + timedelta(seconds=60),
        id="brush_site_refresh",
        replace_existing=True
    )

    # 每周一 04:10 更新一次 pre-dessert-sites 配置库：
    # 只取有用的两样 —— 128×128 站点图标（变了才写库）、各站适配规则的变动（只记日志，不自动改解析）。
    # 上游是周日更新，所以放周一早上；库里 195 份适配规则我们用不上，每周一次足够，也省掉每天 26MB 下载。
    scheduler.add_job(
        tasks.predessert_update,
        "cron",
        day_of_week="mon",
        hour=4,
        minute=10,
        id="predessert_update",
        replace_existing=True
    )
    logger.info("站点配置库（pre-dessert-sites）计划更新: 每周一 04:10")

    logger.info(f"开始运行，稍后你可以在日志文件中查看日志，观察运行情况...")
    logger.info(f"Web界面访问地址: {host}")
    scheduler.start()

if __name__ == "__main__":
    main()