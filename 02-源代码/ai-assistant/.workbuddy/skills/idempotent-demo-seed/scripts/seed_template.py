# -*- coding: utf-8 -*-
"""幂等演示数据播种模板。

用法：
    python scripts/seed.py            # 追加缺失数据（可反复执行，不会重复）
    python scripts/seed.py --reset    # 先删表再重建（慎用）
    python scripts/seed.py --json     # 机器可读的计数回报（给 CI / 启动器）

设计要点（详见 SKILL.md）：
  1. 守卫式插入：按**自然键**逐条判有没有，而不是「表非空就跳过」
  2. 不硬编码自增 id：插入 → flush → 用自然键查回来 → 拿 id 建关联
  3. 数据确定：不用 random；和时间相关的字段用固定的 timedelta 偏移
  4. 结尾回报计数：播种最常见的失败是「没报错但也没灌进去」
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import hash_password                            # noqa: E402
from app.db import SessionLocal, drop_all, init_db            # noqa: E402
from app.models import (Employee, Student, StudentScore,      # noqa: E402
                        SysAccount)

# --------------------------------------------------------------------------- #
# 演示账号是「唯一的真相」：CLI 提示 / README / 前端演示卡片 / E2E / conftest
# 都应从这张表派生或与它逐条对齐。
# --------------------------------------------------------------------------- #
ACCOUNTS: List[Tuple[str, str, str, str]] = [
    # username, password,    role,      display_name
    ("admin",   "admin123",   "admin",   "系统管理员"),
    ("manager", "manager123", "manager", "陈总（管理层）"),
    ("advisor", "advisor123", "employee", "王敏（顾问）"),
    ("student", "student123", "student", "张三（学生）"),
]

EMPLOYEES: List[Tuple[str, str, str]] = [
    # emp_no, name,  department
    ("E001", "王敏", "顾问部"),
    ("E002", "陈总", "管理层"),
]

STUDENTS: List[Tuple[str, str, str]] = [
    # student_no, name,  advisor_emp_no
    ("S2026001", "张三", "E001"),
    ("S2026002", "李四", "E001"),
]


def seed(reset: bool = False) -> Dict[str, int]:
    if reset:
        drop_all()
    init_db()

    db = SessionLocal()
    try:
        now = datetime.now()          # 只取一次，块内统一用同一个基准时间

        # ---------------- 员工（按自然键逐条守卫） ----------------
        for emp_no, name, dept in EMPLOYEES:
            if not db.query(Employee).filter(Employee.emp_no == emp_no).first():
                db.add(Employee(emp_no=emp_no, name=name, department=dept,
                                status="ACTIVE"))
        db.flush()                    # 拿到自增 id，但**不硬编码**

        # ---------------- 学员（关联走对象，不写字面量 id） ----------------
        for student_no, name, advisor_no in STUDENTS:
            if not db.query(Student).filter(Student.student_no == student_no).first():
                advisor = (db.query(Employee)
                           .filter(Employee.emp_no == advisor_no).one())
                db.add(Student(student_no=student_no, name=name,
                               advisor_id=advisor.id, status="ACTIVE",
                               enroll_date=(now - timedelta(days=180)).date()))
        db.flush()

        # ---------------- 账号 ----------------
        for username, password, role, display in ACCOUNTS:
            if not db.query(SysAccount).filter(SysAccount.username == username).first():
                db.add(SysAccount(username=username,
                                  password_hash=hash_password(password),
                                  role=role, display_name=display, is_active=True))

        # ---------------- 惰性数据：靠自然键查回来再挂上去 ----------------
        zhang = db.query(Student).filter(Student.student_no == "S2026001").one()
        if not db.query(StudentScore).filter(StudentScore.student_id == zhang.id).first():
            db.add(StudentScore(student_id=zhang.id, exam_name="雅思第一次",
                                subject="IELTS", score=6.5, full_score=9,
                                exam_date=(now - timedelta(days=60)).date()))

        db.commit()

        counts = {
            "employee": db.query(Employee).count(),
            "student": db.query(Student).count(),
            "account": db.query(SysAccount).count(),
            "student_score": db.query(StudentScore).count(),
        }
        return counts
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="建表 + 灌入演示数据（幂等）")
    parser.add_argument("--reset", action="store_true", help="先 drop_all 再重建（慎用）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 计数")
    args = parser.parse_args()

    counts = seed(reset=args.reset)

    if args.json:
        print(json.dumps({"ok": True, "reset": args.reset, "counts": counts},
                         ensure_ascii=False))
        return 0

    print("[seed] 完成：", counts)
    print("[seed] 演示账号：" + "  ".join(f"{u}/{p}" for u, p, *_ in ACCOUNTS))
    print("[seed] ⚠️ 弱口令仅用于本地演示，上线前必须全部重置（SECRET_KEY 同理）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
