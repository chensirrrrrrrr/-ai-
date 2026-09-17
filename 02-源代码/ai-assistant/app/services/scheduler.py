"""极简后台调度器：定期跑「主动待办推送」「考前提醒」与「定时报告生成」。

当前挂三个任务
--------------
1. `todo.run_once()` —— 扫待处理事项、推给员工、触达未提醒的心理预警；
2. `deadline.run_reminders()` —— 扫学业考务节点，到提前量就提醒学生 + 顾问（REQ-M4-04）；
3. `reports.run_scheduled_with_push()` —— 日/周报生成**并推送**给目标角色（AC-08）。

为什么不用 APScheduler
----------------------
这几个任务都只是「定期扫一遍业务表」，而 APScheduler 会带进 cron 解析、
作业存储、执行器三套概念和一票依赖。需求原文也只要求「配置定时任务或触发器」，
`threading` + `Event.wait()` 就够，且没有任何新依赖。

为什么默认关闭（`ENABLE_SCHEDULER=false`）
------------------------------------------
pytest / 冒烟 / 浏览器 E2E 都在同一份数据上跑，后台线程会持续写 `todo_push`、
`notification` 和 `report_record`、把断言和数据搞脏，还可能在测试进程退出时
留下半个事务。所以默认关，需要演示「真定时」时把开关打开。

可测性不依赖后台线程：`tick()` 与定时循环调用的是**同一个函数**，
而 `todo.run_once()` / `deadline.run_reminders()` / `reports.run_scheduled_with_push()`
本身也都是纯函数入口，测试直接调它们就等价于「跑了一轮定时任务」，不需要 sleep 等线程。
"""
from __future__ import annotations

import logging
import threading

from ..config import settings

logger = logging.getLogger(__name__)


class BackgroundScheduler:
    """守护线程版定时器：`wait(interval)` 可被 `stop()` 立即唤醒，退出不拖时长。"""

    def __init__(self, interval: int) -> None:
        self.interval = max(5, int(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bg-scheduler", daemon=True)
        self._thread.start()
        logger.info("后台调度器已启动：每 %d 秒跑一轮（待办推送 + 考前提醒 + 定时报告）",
                    self.interval)
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
            logger.info("后台调度器已停止")

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:                              # noqa: BLE001 - 线程里不能抛
                logger.exception("这一轮后台任务失败，下一轮继续")

    def tick(self) -> dict:
        """跑一轮：待办推送 → 考前提醒 → 定时报告生成+推送。

        三个任务共用一次 tick —— 都属于「定期扫一遍业务表」，没必要开三个线程。
        后两个各自单独兜异常：一个失败不该把前一个的结果一起丢掉。
        """
        from .reports import run_scheduled_with_push
        from .todo import run_once

        result = run_once()
        if result["pushed"] or result["alerts_delivered"]:
            logger.info("待办推送：%d 名员工，新推 %d 条，触达预警 %d 条",
                        result["staff_count"], result["pushed"],
                        result["alerts_delivered"])

        from ..db import SessionLocal

        db = SessionLocal()
        try:
            try:
                from .deadline import run_reminders

                reminded = run_reminders(db)
                db.commit()
                result["reminders"] = {"scanned": reminded["scanned"],
                                       "sent": reminded["sent"]}
                if reminded["sent"]:
                    logger.info("考前提醒：%d 个节点，发出 %d 条",
                                reminded["scanned"], reminded["sent"])
            except Exception:                              # noqa: BLE001 - 不能中断调度
                db.rollback()
                logger.exception("考前提醒失败，本轮跳过")
                result["reminders"] = {"error": True}

            try:
                scheduled = run_scheduled_with_push(db)
                db.commit()
                result["reports"] = {"created": scheduled["created_count"],
                                     "skipped": scheduled["skipped_count"],
                                     "pushed": scheduled.get("pushed_count", 0),
                                     "due": scheduled["due"]}
                if scheduled["created_count"]:
                    logger.info("定时报告：生成 %d 份、推送 %d 条（%s）",
                                scheduled["created_count"],
                                scheduled.get("pushed_count", 0),
                                "、".join(item["report_type"] for item in scheduled["created"]))
            except Exception:                              # noqa: BLE001 - 不能中断调度
                db.rollback()
                logger.exception("定时报告生成失败，本轮跳过")
                result["reports"] = {"error": True}
        finally:
            db.close()
        return result


# 旧名保留：加「定时报告生成」之前这个类只管待办，测试与外部引用用的是这个名字
TodoScheduler = BackgroundScheduler

scheduler = BackgroundScheduler(settings.scheduler_interval_seconds)


def autostart() -> bool:
    """按配置启动（应用启动时调用）。返回是否真的启动了。"""
    if not settings.enable_scheduler:
        logger.info("未开启后台调度（ENABLE_SCHEDULER=false）；"
                    "可手动调 POST /api/v1/todo/push 与 /api/v1/reports/scheduled/run")
        return False
    return scheduler.start()
