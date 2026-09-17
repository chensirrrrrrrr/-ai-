"""学业考务与考前提醒（REQ-M4-04）+ 业务进度明细（REQ-M4-05）。

两个需求的共同点：数据都在**学生自己的时间线**上，而系统过去只存了
`student.stage` 一个粗粒度状态，回答不了「3 天后要考雅思」「文书审到哪一步了」。

数据来源（TDD 风险 2 的落地口径）
--------------------------------
教务系统 / CRM 的接口未就绪，所以**先做人工导入兜底**：`source=manual`；
接口接上后改成 `source=academic_system` / `crm_system`，
接口契约（`student_id + kind + title + due_at`）不变。

幂等（关键）
------------
两处都用 `(source, external_id)` 唯一约束做 **upsert**：
- `external_id` 给了 → 按它更新（教务系统重复同步不会造重复节点）；
- `external_id` 没给 → 回落到自然键 `(student_id, kind, title, due_at)`
  去重，人工反复导入同一份表也不会翻倍。

提醒（「考前或截止前的智能提醒」）
----------------------------------
`remind_before_hours` 是每个节点的提前量（考试 72h、DDL 48h、签证 120h…），
到点由 `run_reminders()` 扫一遍，同时通知**学生本人**和**他的顾问** ——
只通知学生不够，顾问才是那个能推动事情的人。
`reminded_at` 保证**同一节点只提醒一次**（改了时间会重置，见 `upsert_*`）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Notification, Student, StudentDeadline, StudentProgress
from . import notify

logger = logging.getLogger(__name__)

DEADLINE_KINDS: Dict[str, str] = {
    "DDL": "材料/论文截止",
    "EXAM": "考试",
    "INTERVIEW": "面试",
    "VISA": "签证",
    "OTHER": "其他",
}
DEADLINE_STATUSES = ("OPEN", "DONE", "CANCELLED")

PROGRESS_PHASES: Dict[str, str] = {
    "DOC": "文书审核",
    "APPLY": "院校申请",
    "VISA": "签证办理",
    "OTHER": "其他",
}
PROGRESS_STATUSES = ("PENDING", "DOING", "DONE", "BLOCKED")

# 倒计时分档（前端卡片/看板的「紧急度」直接用它，避免各处自己算阈值）
URGENCY_BUCKETS: Sequence[Tuple[str, int]] = (
    ("overdue", 0),      # 已过期
    ("today", 1),        # 今天
    ("soon", 3),         # 3 天内
    ("week", 7),         # 一周内
    ("later", 10 ** 6),  # 更远
)


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def urgency_of(due_at: datetime, *, now: Optional[datetime] = None) -> str:
    """把一个截止时间归到 `overdue / today / soon / week / later`。"""
    moment = now or datetime.now()
    delta = due_at - moment
    if delta.total_seconds() < 0:
        return "overdue"
    days = delta.total_seconds() / 86400.0
    for name, limit in URGENCY_BUCKETS:
        if name == "overdue":
            continue
        if days < limit:
            return name
    return "later"


# --------------------------------------------------------------------------- #
# 考务节点：导入 / 查询
# --------------------------------------------------------------------------- #
def upsert_deadlines(db: Session, student_id: int, items: Iterable[Dict[str, Any]],
                     *, source: str = "manual") -> Dict[str, Any]:
    """幂等导入一批考务节点。返回 `{created, updated, skipped, items}`。

    幂等键两级：
    1. 给了 `external_id` → 按 `(source, external_id)` 命中；
    2. 没给 → 按 `(student_id, kind, title)` 命中（**不含 `due_at`**）。

    第 2 条刻意不含 `due_at`：教务口径里「同一个学生的同类型同名节点」就是同一个
    节点，改期是对它的**更新**而不是新增。早期版本把 `due_at` 也放进键里，
    结果是「人工改一次期就多出一个重复节点、旧的还挂着旧提醒」——这是真的错。

    命中已有节点且 `due_at` 变了 → **重置 `reminded_at`**，
    否则改期后的新时间点会被静默漏提醒。
    """
    created, updated, skipped, out = 0, 0, 0, []
    for raw in items:
        title = str(raw.get("title") or "").strip()
        due_at = _as_datetime(raw.get("due_at") or raw.get("due"))
        if not title or due_at is None:
            skipped += 1
            continue
        kind = str(raw.get("kind") or "DDL").strip().upper()
        if kind not in DEADLINE_KINDS:
            kind = "OTHER"
        external_id = str(raw.get("external_id")).strip() if raw.get("external_id") else None

        row: Optional[StudentDeadline] = None
        if external_id:
            row = (db.query(StudentDeadline)
                   .filter(StudentDeadline.source == source,
                           StudentDeadline.external_id == external_id).first())
        if row is None:
            row = (db.query(StudentDeadline)
                   .filter(StudentDeadline.student_id == student_id,
                           StudentDeadline.kind == kind,
                           StudentDeadline.title == title).first())

        remind = raw.get("remind_before_hours")
        remind = int(remind) if remind not in (None, "") else settings.deadline_remind_default_hours
        status = str(raw.get("status") or "OPEN").strip().upper()
        if status not in DEADLINE_STATUSES:
            status = "OPEN"

        if row is None:
            row = StudentDeadline(student_id=student_id, kind=kind, title=title,
                                  subject=raw.get("subject"), due_at=due_at,
                                  source=source, external_id=external_id,
                                  remind_before_hours=remind, status=status,
                                  note=raw.get("note"))
            db.add(row)
            created += 1
        else:
            if row.due_at != due_at:
                # 时间变了 → 上一轮的提醒作废，允许按新时间再提醒一次
                row.reminded_at = None
            row.due_at = due_at
            row.source = source
            row.external_id = external_id or row.external_id
            row.subject = raw.get("subject", row.subject)
            row.remind_before_hours = remind
            row.status = status
            row.note = raw.get("note", row.note)
            updated += 1
        db.flush()
        out.append(row)
    return {"created": created, "updated": updated, "skipped": skipped, "items": out}


def list_deadlines(db: Session, *, student_id: Optional[int] = None,
                   kind: Optional[str] = None, status: Optional[str] = None,
                   due_within_days: Optional[int] = None,
                   include_done: bool = True,
                   now: Optional[datetime] = None) -> List[StudentDeadline]:
    query = db.query(StudentDeadline)
    if student_id is not None:
        query = query.filter(StudentDeadline.student_id == student_id)
    if kind:
        query = query.filter(StudentDeadline.kind == kind.upper())
    if status:
        query = query.filter(StudentDeadline.status == status.upper())
    elif not include_done:
        query = query.filter(StudentDeadline.status == "OPEN")
    if due_within_days is not None:
        moment = now or datetime.now()
        query = query.filter(StudentDeadline.due_at <= moment + timedelta(days=due_within_days))
    return query.order_by(StudentDeadline.due_at.asc()).all()


def dump_deadline(row: StudentDeadline, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    moment = now or datetime.now()
    return {
        "id": row.id, "student_id": row.student_id,
        "kind": row.kind, "kind_label": DEADLINE_KINDS.get(row.kind, row.kind),
        "title": row.title, "subject": row.subject,
        "due_at": row.due_at.isoformat(timespec="minutes") if row.due_at else None,
        "days_left": round((row.due_at - moment).total_seconds() / 86400.0, 2) if row.due_at else None,
        "urgency": urgency_of(row.due_at, now=moment) if row.due_at else None,
        "source": row.source, "external_id": row.external_id,
        "remind_before_hours": row.remind_before_hours,
        "reminded_at": row.reminded_at.isoformat(timespec="seconds") if row.reminded_at else None,
        "status": row.status, "note": row.note,
    }


def deadline_summary(db: Session, *, student_id: Optional[int] = None,
                     now: Optional[datetime] = None) -> Dict[str, Any]:
    """按紧急度分档的概览（看板顶部那几个数字）。"""
    moment = now or datetime.now()
    rows = [r for r in list_deadlines(db, student_id=student_id, include_done=False,
                                      now=moment) if r.status == "OPEN"]
    buckets: Dict[str, int] = {name: 0 for name, _ in URGENCY_BUCKETS}
    for row in rows:
        buckets[urgency_of(row.due_at, now=moment)] += 1
    return {"total": len(rows), "buckets": buckets,
            "next": dump_deadline(rows[0], now=moment) if rows else None}


def mark_deadline(db: Session, deadline_id: int, status: str) -> Optional[StudentDeadline]:
    row = db.get(StudentDeadline, deadline_id)
    if row is None:
        return None
    status = str(status or "").strip().upper()
    if status not in DEADLINE_STATUSES:
        return None
    row.status = status
    db.flush()
    return row


# --------------------------------------------------------------------------- #
# 考前 / 截止前提醒
# --------------------------------------------------------------------------- #
def due_reminders(db: Session, *, now: Optional[datetime] = None,
                  limit: Optional[int] = None) -> List[StudentDeadline]:
    """到了提前量、还没提醒过的节点（按截止时间升序）。"""
    moment = now or datetime.now()
    batch = limit or settings.deadline_scan_batch
    rows = (db.query(StudentDeadline)
            .filter(StudentDeadline.status == "OPEN",
                    StudentDeadline.reminded_at.is_(None),
                    StudentDeadline.due_at >= moment - timedelta(days=30))
            .order_by(StudentDeadline.due_at.asc())
            .limit(max(1, batch)).all())
    return [r for r in rows
            if moment >= r.due_at - timedelta(hours=int(r.remind_before_hours or 0))]


def _reminder_body(row: StudentDeadline, student: Optional[Student],
                   now: datetime) -> str:
    days = (row.due_at - now).total_seconds() / 86400.0
    when = "已过期" if days < 0 else (
        "就在今天" if days < 1 else f"还有 {int(days)} 天")
    label = DEADLINE_KINDS.get(row.kind, row.kind)
    parts = [
        f"学生：{student.name if student else row.student_id}",
        f"类型：{label}",
        f"时间：{row.due_at.strftime('%Y-%m-%d %H:%M')}（{when}）",
    ]
    if row.subject:
        parts.append(f"科目/事项：{row.subject}")
    if row.note:
        parts.append(f"备注：{row.note}")
    return "\n".join(parts)


def run_reminders(db: Session, *, now: Optional[datetime] = None,
                  channel: Optional[str] = None,
                  dry_run: bool = False) -> Dict[str, Any]:
    """扫一轮提醒：节点到提前量就通知**学生本人 + 其顾问**（不 commit）。

    为什么同时通知顾问：提醒学生只是「告知」，推动事情落地的责任人通常是顾问。
    只发一条给学生的系统，在业务上等于没提醒。

    幂等：`reminded_at` 落库，重复跑不会重复轰炸；
    截止时间被改期时 `upsert_deadlines` 会把 `reminded_at` 清空，于是会按新时间再提醒。

    `dry_run=True` 只列「会提醒到谁」，不写通知、不动 `reminded_at` ——
    上线前想看一遍影响面时不至于真的打扰一圈人。
    """
    moment = now or datetime.now()
    rows = due_reminders(db, now=moment)
    sent, recipients = 0, []
    for row in rows:
        student = db.get(Student, row.student_id)
        title = f"【{DEADLINE_KINDS.get(row.kind, row.kind)}提醒】{row.title}"
        body = _reminder_body(row, student, moment)

        targets: List[Dict[str, Any]] = [{
            "recipient_type": "student", "recipient_id": row.student_id,
            "recipient_subject": student.student_no if student else None,
        }]
        if student is not None and student.advisor_id is not None:
            targets.append({"recipient_type": "employee",
                            "recipient_id": student.advisor_id})

        if dry_run:
            recipients.extend({
                "deadline_id": row.id, "student_id": row.student_id,
                "recipient_type": t["recipient_type"], "recipient_id": t["recipient_id"],
                "delivered_via": None,
            } for t in targets)
            continue

        notices = notify.send_many(db, targets, category="deadline", title=title,
                                   body=body, biz_type="student_deadline",
                                   biz_id=row.id, channel=channel)
        row.reminded_at = moment
        sent += len(notices)
        recipients.extend({
            "deadline_id": row.id, "student_id": row.student_id,
            "recipient_type": n.recipient_type, "recipient_id": n.recipient_id,
            "delivered_via": n.delivered_via,
        } for n in notices)
    db.flush()
    return {"scanned": len(rows), "sent": sent, "recipients": recipients,
            "dry_run": dry_run,
            "ran_at": moment.isoformat(timespec="seconds")}


# --------------------------------------------------------------------------- #
# 业务进度明细（REQ-M4-05）
# --------------------------------------------------------------------------- #
def upsert_progress(db: Session, student_id: int, items: Iterable[Dict[str, Any]],
                    *, source: str = "manual") -> Dict[str, Any]:
    created, updated, skipped, out = 0, 0, 0, []
    for raw in items:
        item = str(raw.get("item") or "").strip()
        if not item:
            skipped += 1
            continue
        phase = str(raw.get("phase") or "DOC").strip().upper()
        if phase not in PROGRESS_PHASES:
            phase = "OTHER"
        status = str(raw.get("status") or "PENDING").strip().upper()
        if status not in PROGRESS_STATUSES:
            status = "PENDING"
        external_id = str(raw.get("external_id")).strip() if raw.get("external_id") else None
        due_at = _as_datetime(raw.get("due_at") or raw.get("due"))

        row: Optional[StudentProgress] = None
        if external_id:
            row = (db.query(StudentProgress)
                   .filter(StudentProgress.source == source,
                           StudentProgress.external_id == external_id).first())
        if row is None:
            row = (db.query(StudentProgress)
                   .filter(StudentProgress.student_id == student_id,
                           StudentProgress.phase == phase,
                           StudentProgress.item == item).first())
        if row is None:
            row = StudentProgress(student_id=student_id, phase=phase, item=item,
                                  status=status, owner_id=raw.get("owner_id"),
                                  due_at=due_at, note=raw.get("note"),
                                  source=source, external_id=external_id)
            db.add(row)
            created += 1
        else:
            row.phase, row.status = phase, status
            row.owner_id = raw.get("owner_id", row.owner_id)
            row.due_at = due_at or row.due_at
            row.note = raw.get("note", row.note)
            updated += 1
        db.flush()
        out.append(row)
    return {"created": created, "updated": updated, "skipped": skipped, "items": out}


def list_progress(db: Session, *, student_id: int,
                  phase: Optional[str] = None) -> List[StudentProgress]:
    query = db.query(StudentProgress).filter(StudentProgress.student_id == student_id)
    if phase:
        query = query.filter(StudentProgress.phase == phase.upper())
    return query.order_by(StudentProgress.phase.asc(), StudentProgress.id.asc()).all()


def dump_progress(row: StudentProgress) -> Dict[str, Any]:
    return {
        "id": row.id, "student_id": row.student_id,
        "phase": row.phase, "phase_label": PROGRESS_PHASES.get(row.phase, row.phase),
        "item": row.item, "status": row.status, "owner_id": row.owner_id,
        "due_at": row.due_at.isoformat(timespec="minutes") if row.due_at else None,
        "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None,
        "note": row.note, "source": row.source, "external_id": row.external_id,
    }


def progress_board(db: Session, *, student_id: int) -> Dict[str, Any]:
    """按阶段分组 + 完成度（REQ-M4-05 要「可视化跟踪」）。"""
    rows = list_progress(db, student_id=student_id)
    groups: Dict[str, List[Dict[str, Any]]] = {p: [] for p in PROGRESS_PHASES}
    for row in rows:
        groups.setdefault(row.phase, []).append(dump_progress(row))
    done = sum(1 for r in rows if r.status == "DONE")
    blocked = [dump_progress(r) for r in rows if r.status == "BLOCKED"]
    return {
        "student_id": student_id,
        "total": len(rows), "done": done,
        "progress": round(done / len(rows), 4) if rows else 0.0,
        "blocked": blocked,
        "phases": [{"phase": key, "label": label, "items": groups.get(key, []),
                    "total": len(groups.get(key, [])),
                    "done": sum(1 for i in groups.get(key, []) if i["status"] == "DONE")}
                   for key, label in PROGRESS_PHASES.items()],
    }


def notifications_of(db: Session, student_id: int) -> List[Notification]:
    """某个学生的学业节点通知（界面上的「提醒记录」）。"""
    return (db.query(Notification)
            .filter(Notification.category == "deadline",
                    Notification.recipient_type == "student",
                    Notification.recipient_id == student_id)
            .order_by(Notification.id.desc()).all())
