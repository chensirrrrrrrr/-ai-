# -*- coding: utf-8 -*-
"""后台调度器（`app/services/scheduler.py`）单测。

为什么值得补：调度器默认关闭（`ENABLE_SCHEDULER=false`，避免测试进程被后台线程写脏），
于是它的**起停、循环兜异常、三个任务的编排与隔离**这几段平时根本没人跑 ——
覆盖率一直停在 60% 出头。但「定时任务坏了」在生产上是静默的：线程照转，任务不干活。

这里不走「真起线程 + sleep 等一轮」那条慢路子（interval 下限 5s），而是
① 直接测起停状态机；② 用假任务替换三个真实任务来测 `tick()` 的编排与错误隔离；
③ 单独测循环体吞异常（interval 临时压到 0.01s）。
"""
from __future__ import annotations

import time

from app.config import settings
from app.services import deadline as deadline_mod
from app.services import reports as reports_mod
from app.services import scheduler as sch
from app.services import todo as todo_mod


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #
def test_interval_has_a_floor():
    """interval 下限 5s：给个 1 也按 5 走，避免把业务表扫成死循环。"""
    assert sch.BackgroundScheduler(1).interval == 5
    assert sch.BackgroundScheduler(0).interval == 5
    assert sch.BackgroundScheduler(900).interval == 900


def test_start_is_idempotent_and_stop_is_safe():
    s = sch.BackgroundScheduler(60)
    assert s.running is False

    assert s.start() is True
    assert s.running is True
    assert s.start() is False, "重复 start 不该再起一条线程"

    s.stop()
    assert s.running is False
    s.stop()          # 二次 stop 不应抛


def test_todo_scheduler_alias_points_to_same_class():
    """旧名保留：外部/测试引用的是 TodoScheduler。"""
    assert sch.TodoScheduler is sch.BackgroundScheduler


def test_loop_swallows_task_exception(monkeypatch):
    """线程里不能抛：单轮失败要记日志后继续，否则调度器一轮就死。"""
    s = sch.BackgroundScheduler(60)
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("模拟单轮失败")

    monkeypatch.setattr(s, "tick", boom, raising=True)
    s.interval = 0.01                       # 绕开 5s 下限，让循环跑得动
    assert s.start() is True
    time.sleep(0.15)
    s.stop()

    assert calls["n"] >= 1, "循环体应当至少跑过一轮"
    assert s.running is False


# --------------------------------------------------------------------------- #
# tick()：编排三个任务
# --------------------------------------------------------------------------- #
def _patch_ok(monkeypatch, *, pushed=1, sent=1, created=1, pushed_count=2):
    monkeypatch.setattr(todo_mod, "run_once", lambda: {
        "staff_count": 3, "pushed": pushed, "alerts_delivered": 0})
    monkeypatch.setattr(deadline_mod, "run_reminders",
                        lambda db: {"scanned": 2, "sent": sent})
    monkeypatch.setattr(reports_mod, "run_scheduled_with_push", lambda db: {
        "created_count": created, "skipped_count": 0,
        "pushed_count": pushed_count, "due": ["daily_digest"],
        "created": [{"report_type": "daily_digest"}]})


def test_tick_runs_all_three_jobs(monkeypatch):
    """一轮 = 待办推送 → 考前提醒 → 定时报告生成+推送，结果都挂在同一个 dict 上。"""
    _patch_ok(monkeypatch)
    result = sch.BackgroundScheduler(60).tick()

    assert result["staff_count"] == 3
    assert result["pushed"] == 1
    assert result["reminders"] == {"scanned": 2, "sent": 1}
    assert result["reports"] == {"created": 1, "skipped": 0, "pushed": 2,
                                 "due": ["daily_digest"]}


def test_tick_with_nothing_to_do_still_returns_shape(monkeypatch):
    """没有待推/待提醒/待生成时，三个键依然要在，值归零而不是缺键。"""
    _patch_ok(monkeypatch, pushed=0, sent=0, created=0, pushed_count=0)
    result = sch.BackgroundScheduler(60).tick()

    assert result["pushed"] == 0
    assert result["reminders"] == {"scanned": 2, "sent": 0}
    assert result["reports"]["created"] == 0


def test_tick_isolates_job_failures(monkeypatch):
    """后两个任务各自兜异常：一个失败不该把前一个的结果一起丢掉。"""
    monkeypatch.setattr(todo_mod, "run_once", lambda: {
        "staff_count": 3, "pushed": 1, "alerts_delivered": 0})

    def boom_reminders(db):
        raise RuntimeError("考前提醒炸了")

    monkeypatch.setattr(deadline_mod, "run_reminders", boom_reminders)
    monkeypatch.setattr(reports_mod, "run_scheduled_with_push", lambda db: {
        "created_count": 1, "skipped_count": 0, "pushed_count": 0,
        "due": [], "created": [{"report_type": "daily_digest"}]})

    result = sch.BackgroundScheduler(60).tick()
    assert result["reminders"] == {"error": True}
    assert result["reports"]["created"] == 1, "提醒失败不能连累报告任务"
    assert result["pushed"] == 1, "前一个任务的成果也不能丢"

    # 反过来：报告炸了，提醒的成果要留下
    monkeypatch.setattr(deadline_mod, "run_reminders",
                        lambda db: {"scanned": 1, "sent": 1})

    def boom_reports(db):
        raise RuntimeError("报告生成炸了")

    monkeypatch.setattr(reports_mod, "run_scheduled_with_push", boom_reports)
    result2 = sch.BackgroundScheduler(60).tick()
    assert result2["reports"] == {"error": True}
    assert result2["reminders"] == {"scanned": 1, "sent": 1}


# --------------------------------------------------------------------------- #
# autostart()：按开关决定
# --------------------------------------------------------------------------- #
def test_autostart_respects_flag(monkeypatch):
    monkeypatch.setattr(settings, "enable_scheduler", False)
    assert sch.autostart() is False
    assert sch.scheduler.running is False

    monkeypatch.setattr(settings, "enable_scheduler", True)
    assert sch.autostart() is True
    assert sch.scheduler.running is True
    sch.scheduler.stop()
    assert sch.scheduler.running is False
