"""ORM 模型：业务表 + 1 张认证表（sys_account）。

表名与《技术方案设计文档》第 6.1 节一致；`sys_account` 为本版落地时新增，
用于统一认证（原方案把登录隐含在「员工/学生」实体里，实现时显式拆表更清晰）。
下列 6 张是 2026-09-15 补齐需求查验缺口时新增的：

- `screening_rule` —— 《用户画像研判规则》的可导入 / 可版本化副本（REQ-M1-03）；
- `student_deadline` —— 论文 DDL / 考试时间等学业考务节点（REQ-M4-04）；
- `student_progress` —— 文书 / 申请 / 签证的进度明细（REQ-M4-05）；
- `student_promotion` —— 增值转化推荐留痕，含频控与退订（REQ-M4-07）；
- `notification` —— 统一通知 / 触达记录（报告推送、工单解决、审批结果、考前提醒…）；
- `pending_action` —— 写操作二次确认（SRS 4.3.4）。

类型兼容：`BigIntPK` 在 SQLite 上退化为 INTEGER（否则自增主键不生效），
`JSON` 在 SQLite 上存 TEXT、在 MySQL 上存原生 JSON 列。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, ForeignKey, Integer,
                        JSON, Numeric, String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

# SQLite 上 BIGINT 主键不会自增，必须降级为 INTEGER
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now, nullable=False)


# --------------------------------------------------------------------------- #
# 组织域
# --------------------------------------------------------------------------- #
class Employee(Base, TimestampMixin):
    """员工与组织架构：顾问、老师、管理层共用一张表，用 role 区分。"""

    __tablename__ = "employee"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    emp_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    department: Mapped[Optional[str]] = mapped_column(String(64))
    title: Mapped[Optional[str]] = mapped_column(String(64))
    phone: Mapped[Optional[str]] = mapped_column(String(32))
    email: Mapped[Optional[str]] = mapped_column(String(128))
    manager_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employee.id"))
    # 业务角色：advisor(顾问) / teacher(带教/跟进) / manager(管理层) / admin
    biz_role: Mapped[str] = mapped_column(String(32), default="advisor", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)


class EmployeeReport(Base, TimestampMixin):
    """员工日报，支持语音录入（source=voice，留存原始音频地址）。"""

    __tablename__ = "employee_report"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    raw_audio_url: Mapped[Optional[str]] = mapped_column(String(512))
    transcript: Mapped[Optional[str]] = mapped_column(Text)


# --------------------------------------------------------------------------- #
# 客户域
# --------------------------------------------------------------------------- #
class CustomerLead(Base, TimestampMixin):
    """意向客户主表。"""

    __tablename__ = "customer_lead"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source: Mapped[Optional[str]] = mapped_column(String(32))
    intention_country: Mapped[Optional[str]] = mapped_column(String(64))
    intention_stage: Mapped[Optional[str]] = mapped_column(String(32))
    # NEW / FOLLOWING / SIGNED / LOST
    status: Mapped[str] = mapped_column(String(16), default="NEW", nullable=False, index=True)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employee.id"), index=True)
    remark: Mapped[Optional[str]] = mapped_column(String(512))


class CustomerFollowup(Base):
    """客户跟进记录。"""

    __tablename__ = "customer_followup"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("customer_lead.id"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    follow_type: Mapped[str] = mapped_column(String(16), default="PHONE", nullable=False)
    next_plan: Mapped[Optional[str]] = mapped_column(String(512))
    next_follow_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employee.id"))
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class LeadScreening(Base):
    """客户研判结果（由 Dify 研判工作流产出，支持人工复核回写）。

    复核留痕的设计（REQ-M1-06）：
    - `ai_conclusion` 保存 **AI 原始结论**，首次复核时快照写入，此后不再改动；
    - `conclusion` 是**对外生效的结论** —— 人工推翻时被覆写；
    - 于是「AI 说什么 / 人改成什么」两个值同时可查，既能给业务看最终口径，
      也能把「AI 判错的那批」导出来做规则优化（见 `/screening/corrections`）。
    """

    __tablename__ = "lead_screening"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    lead_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customer_lead.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(16), default="TEXT", nullable=False)
    source_name: Mapped[Optional[str]] = mapped_column(String(255))
    # 一次批量研判共用一个 batch_id，便于按批回捞「研判结果清单」（REQ-M1-07）
    batch_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    raw_file_url: Mapped[Optional[str]] = mapped_column(String(512))
    extracted_fields: Mapped[Optional[dict]] = mapped_column(JSON)
    missing_fields: Mapped[Optional[list]] = mapped_column(JSON)
    hit_products: Mapped[Optional[list]] = mapped_column(JSON)
    # 符合 / 不符合 / 信息不足 —— 人工复核后为最终生效值
    conclusion: Mapped[str] = mapped_column(String(32), nullable=False)
    # AI 原始结论快照（复核留痕用；未复核时为 None）
    ai_conclusion: Mapped[Optional[str]] = mapped_column(String(32))
    evidence: Mapped[Optional[list]] = mapped_column(JSON)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(5, 4))
    # PENDING 待复核 / CONFIRMED 已确认 / OVERRIDDEN 已推翻
    review_status: Mapped[str] = mapped_column(String(16), default="PENDING",
                                               nullable=False, index=True)
    reviewed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("employee.id"))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    review_remark: Mapped[Optional[str]] = mapped_column(String(512))
    dify_run_id: Mapped[Optional[str]] = mapped_column(String(64))
    # 本次研判依据的规则版本（REQ-M1-03：结论必须能追溯到规则条目与规则版本）
    rule_version: Mapped[Optional[str]] = mapped_column(String(32))
    # 判定来源：local（本地规则引擎）/ dify / local+dify —— 不静默降级，前端可见
    rule_source: Mapped[Optional[str]] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


# --------------------------------------------------------------------------- #
# 学员域
# --------------------------------------------------------------------------- #
class Student(Base, TimestampMixin):
    """学生主表。"""

    __tablename__ = "student"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    student_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    gender: Mapped[Optional[str]] = mapped_column(String(8))
    phone: Mapped[Optional[str]] = mapped_column(String(32))
    email: Mapped[Optional[str]] = mapped_column(String(128))
    # 敏感字段：只存密文/脱敏值，明文不落库
    id_card_masked: Mapped[Optional[str]] = mapped_column(String(32))
    country_target: Mapped[Optional[str]] = mapped_column(String(64))
    program_level: Mapped[Optional[str]] = mapped_column(String(32))
    # 申请阶段：PREPARING / APPLYING / OFFERED / VISA / ENROLLED
    stage: Mapped[str] = mapped_column(String(32), default="PREPARING", nullable=False, index=True)
    advisor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employee.id"), index=True)
    enroll_date: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)


class StudentScore(Base):
    """学生成绩。"""

    __tablename__ = "student_score"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("student.id"), nullable=False, index=True)
    exam_name: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    full_score: Mapped[float] = mapped_column(Numeric(6, 2), default=100, nullable=False)
    exam_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class StudentRequest(Base):
    """学生行政服务申请（请假 / 考务 / 其他）。"""

    __tablename__ = "student_request"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("student.id"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(16), default="LEAVE", nullable=False, index=True)
    content: Mapped[Optional[dict]] = mapped_column(JSON)
    # PENDING / APPROVED / REJECTED
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False, index=True)
    approver_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employee.id"))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    remark: Mapped[Optional[str]] = mapped_column(String(512))
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class StudentMentalProfile(Base, TimestampMixin):
    """心理健康画像（一份档案对应一名学生）。"""

    __tablename__ = "student_mental_profile"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("student.id"), unique=True, nullable=False)
    emotion_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2))
    risk_level: Mapped[str] = mapped_column(String(16), default="LOW", nullable=False)
    tags: Mapped[Optional[list]] = mapped_column(JSON)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    last_assessed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class MentalAlert(Base):
    """心理预警记录。"""

    __tablename__ = "mental_alert"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("student.id"), nullable=False, index=True)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    evidence: Mapped[Optional[str]] = mapped_column(Text)
    # OPEN / FOLLOWING / CLOSED
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False, index=True)
    handler_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employee.id"))
    # ---- 触达通道（REQ：一旦识别高危「立即触发预警并记录原因」，辅助老师介入）----
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    notified_to: Mapped[Optional[int]] = mapped_column(ForeignKey("employee.id"))
    notify_channel: Mapped[Optional[str]] = mapped_column(String(16))
    # 触达时生成的干预建议（留痕，避免「通知了但没说该做什么」）
    intervention: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class AfterSalesTicket(Base):
    """售后反馈工单。"""

    __tablename__ = "after_sales_ticket"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    student_id: Mapped[Optional[int]] = mapped_column(ForeignKey("student.id"), index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(String(512))
    category: Mapped[Optional[str]] = mapped_column(String(32))
    # OPEN / PROCESSING / RESOLVED / CLOSED
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False, index=True)
    handler_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employee.id"))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    satisfaction: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


# --------------------------------------------------------------------------- #
# 运营域
# --------------------------------------------------------------------------- #
class Activity(Base):
    """活动信息。"""

    __tablename__ = "activity"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    category: Mapped[Optional[str]] = mapped_column(String(32))
    description: Mapped[Optional[str]] = mapped_column(Text)
    start_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    end_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    location: Mapped[Optional[str]] = mapped_column(String(128))
    capacity: Mapped[Optional[int]] = mapped_column(Integer)
    enrolled_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # DRAFT / OPEN / CLOSED
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class ActivityEnrollment(Base):
    """活动报名（学生与意向客户都能报名，故两个外键均可空）。"""

    __tablename__ = "activity_enrollment"
    __table_args__ = (UniqueConstraint("activity_id", "student_id", name="uq_activity_student"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    activity_id: Mapped[int] = mapped_column(ForeignKey("activity.id"), nullable=False, index=True)
    student_id: Mapped[Optional[int]] = mapped_column(ForeignKey("student.id"), index=True)
    lead_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customer_lead.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="ENROLLED", nullable=False)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class CourseProject(Base, TimestampMixin):
    """课程 / 升学项目库。"""

    __tablename__ = "course_project"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(32))
    country: Mapped[Optional[str]] = mapped_column(String(64))
    degree_level: Mapped[Optional[str]] = mapped_column(String(32))
    tuition: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    duration_months: Mapped[Optional[int]] = mapped_column(Integer)
    description: Mapped[Optional[str]] = mapped_column(Text)
    # 匹配条件（REQ-M2-04：按学历背景与意向国家自动匹配推荐）
    # `target_stage` 是**意向阶段**，多个用逗号分隔（如 "硕士,博士"）
    target_stage: Mapped[Optional[str]] = mapped_column(String(64))
    language_require: Mapped[Optional[str]] = mapped_column(String(64))
    gpa_min: Mapped[Optional[float]] = mapped_column(Numeric(4, 2))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)


class KnowledgeDoc(Base, TimestampMixin):
    """知识库文档与切片元数据（真实切片存在 Dify 数据集里）。"""

    __tablename__ = "knowledge_doc"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(512))
    file_path: Mapped[Optional[str]] = mapped_column(String(512))
    version: Mapped[str] = mapped_column(String(32), default="V1.0", nullable=False)
    effective_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # 与 Dify 数据集的映射关系，便于后台管理与失效下线
    dify_dataset_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    dify_document_id: Mapped[Optional[str]] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # DRAFT / INDEXED / OFFLINE
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", nullable=False)


class ReportRecord(Base):
    """报告生成记录。

    五类业务报告（需求 M5）：`customer_ops` 全域客户经营分析 / `daily_digest` 日报汇总（日）/
    `weekly_digest` 日报汇总（周）/ `mental_weekly` 心理健康周报 / `complaint_weekly` 投诉处理周报。
    `weekly` / `monthly` / `custom` 是历史值，仍然接受（映射到最接近的新类型）。

    `snapshot` 存**真实统计快照**：在线查阅、Excel、PDF 三种出口都读同一份，
    避免「导出时重算一遍、数字跟页面上对不上」这种最难查的口径漂移。
    """

    __tablename__ = "report_record"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    report_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    params: Mapped[Optional[dict]] = mapped_column(JSON)
    content: Mapped[Optional[str]] = mapped_column(Text)
    # 报告口径期（含首尾），定时生成用 (type, period_start, period_end) 做幂等
    period_start: Mapped[Optional[date]] = mapped_column(Date, index=True)
    period_end: Mapped[Optional[date]] = mapped_column(Date, index=True)
    snapshot: Mapped[Optional[dict]] = mapped_column(JSON)
    # 由定时任务生成（区别于人工点击「生成报告」）
    from_schedule: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    exported_formats: Mapped[Optional[list]] = mapped_column(JSON)
    file_path: Mapped[Optional[str]] = mapped_column(String(512))
    # PENDING / RUNNING / DONE / FAILED
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False, index=True)
    generated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("employee.id"))
    dify_run_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


# --------------------------------------------------------------------------- #
# 全局
# --------------------------------------------------------------------------- #
class TodoPush(Base):
    """主动待办推送记录。

    这张表承担两件事，别只当成日志：
    1. **留痕**：谁在什么时候被问了什么、明细多少条、有没有处理（`acknowledged_at`）；
    2. **幂等 / 频控**：`dedupe_key` 唯一 = 「同一员工同一类待办在同一时间窗内只推一次」，
       否则每轮定时任务都会刷一条，员工会被同一个提醒反复轰炸。

    待办本身是 `services/todo.collect()` **现场算**出来的，不落表 ——
    否则「审批完了待办还在」这种状态不同步迟早会发生。
    """

    __tablename__ = "todo_push"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employee.id"), index=True)
    # 登录账号名：定时任务按账号维度推，且「只看自己的推送」用它可以精确过滤
    subject: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    question: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[Optional[dict]] = mapped_column(JSON)
    channel: Mapped[str] = mapped_column(String(16), default="chat", nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(64))
    ack_remark: Mapped[Optional[str]] = mapped_column(String(255))


class AuditLog(Base):
    """操作审计日志：每笔写操作一条，含 trace_id 便于全链路回放。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    actor_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    actor_role: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource: Mapped[Optional[str]] = mapped_column(String(64))
    resource_id: Mapped[Optional[str]] = mapped_column(String(64))
    detail: Mapped[Optional[dict]] = mapped_column(JSON)
    ip: Mapped[Optional[str]] = mapped_column(String(64))
    trace_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class SysAccount(Base, TimestampMixin):
    """统一认证账号（本版新增），关联 employee 或 student。"""

    __tablename__ = "sys_account"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # visitor / student / employee / manager
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ref_id: Mapped[Optional[int]] = mapped_column(Integer)
    display_name: Mapped[Optional[str]] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ScreeningRule(Base):
    """《用户画像研判规则》的**可导入、可版本化**副本（REQ-M1-03）。

    为什么规则要落库而不是硬编码：
    SRS 4.1.3 明确要求「支持导入并版本化管理《用户画像研判规则》，规则调整无需发版」，
    4.1.5 又要求「研判必须以甲方提供的规则文件为唯一依据」。硬编码在 python 里
    两条都不满足 —— 改规则要改代码、要重新部署，而且「依据的是哪一版规则」无从追溯。

    版本模型：一个 product_key（产品）同时只有一版 `ACTIVE`，其余为
    `DRAFT`（导入了但没启用）或 `ARCHIVED`（被新版本顶掉）。
    研判时取 ACTIVE，并把 `version` 写进 `lead_screening.rule_version`。
    """

    __tablename__ = "screening_rule"
    __table_args__ = (UniqueConstraint("product_key", "version", name="uq_rule_product_version"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    product_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), default="V1.0", nullable=False)
    # all = 全部规则必须命中；any = 命中任一即算符合
    logic: Mapped[str] = mapped_column(String(8), default="all", nullable=False)
    # 规则条目数组：[{id, field, op, value, weight, desc}]
    rules: Mapped[Optional[list]] = mapped_column(JSON)
    # 缺这些字段 => 「信息不足」，不允许直接判「不符合」（SRS 4.1.5 业务规则）
    required_fields: Mapped[Optional[list]] = mapped_column(JSON)
    threshold: Mapped[float] = mapped_column(Numeric(5, 4), default=0.75, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(String(512))
    # DRAFT / ACTIVE / ARCHIVED
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", nullable=False, index=True)
    source_name: Mapped[Optional[str]] = mapped_column(String(255))
    imported_by: Mapped[Optional[str]] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class StudentDeadline(Base):
    """学业考务节点：论文 DDL / 考试时间等（REQ-M4-04）。

    数据来源两条路：`source=manual` 由顾问/教务人工录入（TDD 风险 2 的兜底方案：
    「无接口时先做人工导入兜底」），`source=academic_system` 留给教务系统对接；
    `external_id` 是教务系统主键，用来做**幂等 upsert**，重复导入不会产生重复节点。

    `remind_before_hours` + `reminded_at` 支撑「考前或截止前的智能提醒」：
    到点由 `services/deadline.run_reminders()` 扫一遍，提醒学生本人与其顾问。
    """

    __tablename__ = "student_deadline"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_deadline_source_ext"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("student.id"), nullable=False, index=True)
    # DDL（论文/材料截止）/ EXAM（考试）/ INTERVIEW（面试）/ VISA（签证）/ OTHER
    kind: Mapped[str] = mapped_column(String(16), default="DDL", nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    subject: Mapped[Optional[str]] = mapped_column(String(64))
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(64))
    remind_before_hours: Mapped[int] = mapped_column(Integer, default=48, nullable=False)
    reminded_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # OPEN / DONE / CANCELLED
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False, index=True)
    note: Mapped[Optional[str]] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class StudentProgress(Base):
    """留学生意业务进度明细：文书审核 / 院校申请 / 签证办理（REQ-M4-05）。

    `student.stage` 只有一个粗粒度阶段，回答不了「文书审核到哪一步了、谁在跟」。
    这张表按 `phase + item` 存细项，`source=manual` 为人工导入兜底
    （TDD 7.4 的 CRM/申请系统对接未就绪前的过渡），接上系统后改成
    `source=crm_system` 并用 `external_id` 幂等 upsert，接口契约不变。
    """

    __tablename__ = "student_progress"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_progress_source_ext"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("student.id"), nullable=False, index=True)
    # DOC 文书 / APPLY 院校申请 / VISA 签证 / OTHER
    phase: Mapped[str] = mapped_column(String(16), default="DOC", nullable=False, index=True)
    item: Mapped[str] = mapped_column(String(128), nullable=False)
    # PENDING / DOING / DONE / BLOCKED
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False, index=True)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employee.id"))
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(64))


class StudentPromotion(Base):
    """增值转化的推荐留痕（REQ-M4-07）。

    需求要求「智能识别升学意向 → 适时推送 → **控制频次** + **明确的退订入口**」，
    这三件事都必须有记录，否则「频次控制」无从实现（没有历史就不知道推过几次），
    退订也没地方落。`dedupe_key` = `student_id:offer_id:窗口` 保证同一窗口不重复推。
    """

    __tablename__ = "student_promotion"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("student.id"), nullable=False, index=True)
    course_project_id: Mapped[Optional[int]] = mapped_column(ForeignKey("course_project.id"),
                                                             index=True)
    offer_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 触发依据：从哪个字段识别出升学意向（可追溯，别只给一个 AI 结论）
    trigger: Mapped[Optional[str]] = mapped_column(String(255))
    script: Mapped[Optional[str]] = mapped_column(Text)
    # PUSHED / ACCEPTED / REJECTED / UNSUBSCRIBED
    status: Mapped[str] = mapped_column(String(16), default="PUSHED", nullable=False, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    pushed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    handled_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    handled_remark: Mapped[Optional[str]] = mapped_column(String(255))


class Notification(Base):
    """统一通知 / 触达记录。

    为什么单独建表而不是各业务表各加一列 `notified_at`：
    「系统主动告知某人」在本项目里有六处（报告推送、工单解决、请假审批结果、
    DDL 考前提醒、增值推荐、心理预警触达）。各写各的列会得到六套半成品 ——
    有的只记时间不记内容、有的记了但界面看不到、有的根本没有「读没读」。
    统一成一张表后，前端只要一个「我的通知」，后端只要一个 `notify.send()`。

    `channel` 是**请求的通道**，`delivered_via` 是**实际生效的通道**：
    企业微信 / 短信没配置时降级为站内（internal）。降级必须如实记录，
    否则「已触达」会变成一句无法验证的话（与 `services/material.py` 的
    `parser` 字段同一口径：不静默降级）。
    """

    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # student / employee
    recipient_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    recipient_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 登录账号名；有账号的收件人填上，便于「只看自己的」精确过滤
    recipient_subject: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    # report / ticket / leave / deadline / promotion / alert / screening / system
    category: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    body: Mapped[Optional[str]] = mapped_column(Text)
    # 关联业务对象，前端可以点回原单据
    biz_type: Mapped[Optional[str]] = mapped_column(String(32))
    biz_id: Mapped[Optional[str]] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(16), default="internal", nullable=False)
    delivered_via: Mapped[Optional[str]] = mapped_column(String(16))
    # SENT / FAILED
    status: Mapped[str] = mapped_column(String(16), default="SENT", nullable=False, index=True)
    detail: Mapped[Optional[dict]] = mapped_column(JSON)
    delivered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


class PendingAction(Base):
    """写操作的「二次确认」暂存（SRS 4.3.4 / TDD 3.2）。

    需求原文：指令式操作（审批、状态变更）**执行前需向员工回显确认信息**，避免误操作。
    实现方式：写类工具第一次调用只创建一条 PENDING 记录并把**回显文案**返回给调用方
    （Dify / 前端），带 `confirm=<token>` 再调一次才真正执行。

    为什么要落库而不是放在内存里：
    - 确认动作可能发生在**另一个进程/另一次请求**（Dify 的一轮对话 ↔ 工具回调）；
    - `expires_at` + `status` 支持过期与取消，且每次确认都有留痕（谁确认的、什么时候）。
    """

    __tablename__ = "pending_action"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    actor_subject: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    params: Mapped[Optional[dict]] = mapped_column(JSON)
    # 回显给操作人的确认文案（人话，不是 JSON）
    preview: Mapped[str] = mapped_column(String(512), nullable=False)
    # PENDING / CONFIRMED / CANCELLED / EXPIRED
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    result: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)


ALL_TABLES = (
    Employee, EmployeeReport,
    CustomerLead, CustomerFollowup, LeadScreening, ScreeningRule,
    Student, StudentScore, StudentRequest, StudentMentalProfile,
    MentalAlert, AfterSalesTicket, StudentDeadline, StudentProgress, StudentPromotion,
    Activity, ActivityEnrollment, CourseProject, KnowledgeDoc, ReportRecord,
    Notification, PendingAction,
    AuditLog,
)
# 业务表（含本版为补齐需求新增的 6 张）。
# 与《技术方案设计文档》6.1 的「数据表总览」对不上时以本文件为准，
# 文档里的数字要跟着改（另两张：`todo_push`、`sys_account`）。
BUSINESS_TABLE_COUNT = len(ALL_TABLES)
