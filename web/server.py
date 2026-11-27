#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   server.py
@Time    :   2025/03/07 17:05:28
@Version :   1.0
'''

# here put the import lib

from web import create_app
import threading
import time

from loguru import logger

# 端口刚被释放前会有旧进程还在退出的窗口（容器重建时尤其常见），
# 这里最多等这么久，每 2 秒试一次。
PORT_RETRY_SECONDS = 60
PORT_RETRY_INTERVAL = 2


def run_web_server(host='0.0.0.0', port=8000, debug=False):
    """在单独的线程中运行 web 服务器。

    端口被占用时不再静默退出：Werkzeug 打印 “Address already in use” 后抛 SystemExit，
    以前这个守护线程死掉、主进程照常跑定时任务，表现是容器 Up 但页面打不开。
    现在按间隔重试，等旧进程让出端口后接管；超过时限才放弃并写明确错误。
    """
    app = create_app()
    deadline = time.monotonic() + PORT_RETRY_SECONDS
    attempt = 0

    while True:
        attempt += 1
        try:
            app.run(host=host, port=port, debug=debug, use_reloader=False)
            return
        except SystemExit as e:
            if e.code in (0, None):
                return
            if time.monotonic() >= deadline:
                logger.error(
                    f"Web 端口 {port} 等待 {PORT_RETRY_SECONDS} 秒仍被占用，放弃启动"
                    f"（共尝试 {attempt} 次）。请检查是否有另一个实例占用该端口。"
                )
                return
            logger.warning(
                f"Web 端口 {port} 被占用，{PORT_RETRY_INTERVAL} 秒后重试"
                f"（第 {attempt} 次，最多等 {PORT_RETRY_SECONDS} 秒）"
            )
            time.sleep(PORT_RETRY_INTERVAL)


def start_web_server_thread(host='0.0.0.0', port=8000):
    """在后台线程中启动 web 服务器"""
    web_thread = threading.Thread(
        target=run_web_server,
        args=(host, port, False),
        daemon=True
    )
    web_thread.start()
    return web_thread
