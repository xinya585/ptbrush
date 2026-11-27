"""主线程与 Web 线程共享的运行时对象。

调度器在主线程里建好，注册到这里之后，Web 线程保存配置时就能直接改调度，
不用重启容器让新配置生效。
"""

_scheduler = None

BRUSH_JOB_ID = "brush_job"
# 删种与状态检查的独立任务（周期由 [brush].check_interval 控制，与刷流周期分开）
CHECK_JOB_ID = "brush_check_job"


def set_scheduler(scheduler):
    """由 main.py 在创建调度器后调用一次。"""
    global _scheduler
    _scheduler = scheduler




def reschedule_brush(minutes) -> bool:
    """把刷流任务改成每 minutes 分钟一次；返回是否真的改成功了。

    调度器还没注册（比如单独起 web 服务跑测试）时返回 False，调用方据此
    决定要不要提示用户重启。
    """
    scheduler = _scheduler
    if scheduler is None:
        return False
    try:
        minutes = max(5, int(minutes))
    except (TypeError, ValueError):
        return False
    try:
        scheduler.reschedule_job(BRUSH_JOB_ID, trigger="interval", minutes=minutes)
        return True
    except Exception:
        return False


def reschedule_check(minutes) -> bool:
    """把删种/状态检查任务改成每 minutes 分钟一次；返回是否真的改成功了。"""
    scheduler = _scheduler
    if scheduler is None:
        return False
    try:
        minutes = max(1, int(minutes))
    except (TypeError, ValueError):
        return False
    try:
        scheduler.reschedule_job(CHECK_JOB_ID, trigger="interval", minutes=minutes)
        return True
    except Exception:
        return False
