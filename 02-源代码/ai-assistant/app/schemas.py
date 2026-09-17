"""Pydantic v2 请求/响应模型。

统一响应信封（code/message/data/trace_id）由 core.ok() 组装，这里只描述 data 内部结构。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Role = Literal["visitor", "student", "employee", "manager", "admin"]


# --------------------------------------------------------------------------- #
# 认证
# --------------------------------------------------------------------------- #
class TokenRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)


# --------------------------------------------------------------------------- #
# 对话 / 语音录入
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    session_id: str = Field(..., max_length=64)
    message: str = Field(..., min_length=1, max_length=4000)
    channel: str = "web"
    user_id: Optional[str] = None
    role: Role = "visitor"
    conversation_id: Optional[str] = Field(None, description="Dify 会话 ID，多轮对话时回传")
    client_ts: Optional[int] = None
    inputs: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 客户域
# --------------------------------------------------------------------------- #
class LeadCreate(BaseModel):
    name: str = Field(..., max_length=64)
    phone: str = Field(..., max_length=32)
    source: Optional[str] = None
    intention_country: Optional[str] = None
    intention_stage: Optional[str] = None
    owner_id: Optional[int] = None
    remark: Optional[str] = None


class LeadStatusUpdate(BaseModel):
    status: Literal["NEW", "FOLLOWING", "SIGNED", "LOST"]
    remark: Optional[str] = None


class FollowupCreate(BaseModel):
    content: str = Field(..., min_length=1)
    follow_type: str = "PHONE"
    next_plan: Optional[str] = None
    next_follow_at: Optional[datetime] = None
    idempotency_key: Optional[str] = None


class ScreeningRequest(BaseModel):
    lead_id: Optional[int] = None
    source_type: Literal["TEXT", "PDF", "EXCEL"] = "TEXT"
    text: Optional[str] = Field(None, description="source_type=TEXT 时直接给正文")
    raw_file_url: Optional[str] = None
    source_name: Optional[str] = Field(None, description="原始材料文件名，便于追溯")
    idempotency_key: Optional[str] = None


class ScreeningReviewRequest(BaseModel):
    """人工复核（REQ-M1-06）。

    CONFIRM  = 确认 AI 结论；OVERRIDE = 推翻并给出修正结论。
    OVERRIDE 必须带 conclusion —— 「推翻但不说改成什么」在业务上没意义。
    """

    action: Literal["CONFIRM", "OVERRIDE"]
    conclusion: Optional[Literal["符合", "不符合", "信息不足"]] = None
    hit_products: Optional[List[Any]] = None
    extracted_fields: Optional[Dict[str, Any]] = None
    remark: Optional[str] = Field(None, max_length=512)


class BatchScreeningItem(BaseModel):
    """批量研判中的一条材料（REQ-M1-07 的「批量上传与批处理」）。"""

    lead_id: Optional[int] = None
    source_type: Literal["TEXT", "PDF", "EXCEL"] = "TEXT"
    text: Optional[str] = None
    raw_file_url: Optional[str] = None
    source_name: Optional[str] = None


class BatchScreeningRequest(BaseModel):
    """两种入参方式二选一：

    - `items`：逐条显式给材料（前端「批量上传」走这条）；
    - `lead_ids`：只给客户 ID，正文由服务端按客户档案 + 最近跟进拼出来
      （前端「勾选客户批量研判」走这条，省得前端自己去拼文本）。
    """

    items: Optional[List[BatchScreeningItem]] = None
    lead_ids: Optional[List[int]] = None
    batch_id: Optional[str] = Field(None, max_length=64)
    stop_on_error: bool = False


class RuleItem(BaseModel):
    id: Optional[str] = Field(None, max_length=32)
    field: str = Field(..., max_length=32, description="规范字段名，见 /screening/rules/template")
    op: Literal["in", "not_in", "eq", "ne", "gte", "lte", "between", "matches", "exists"]
    value: Optional[Any] = None
    weight: float = Field(1.0, ge=0)
    desc: Optional[str] = Field(None, max_length=255)


class RuleProduct(BaseModel):
    key: str = Field(..., max_length=64, description="产品标识，如 postgrad_direct")
    name: str = Field(..., max_length=64, description="产品名，如 硕士直申")
    logic: Literal["all", "any"] = "all"
    threshold: float = Field(0.75, gt=0, le=1)
    required_fields: List[str] = Field(default_factory=list,
                                       description="缺这些字段即判「信息不足」，不判不符合")
    rules: List[RuleItem]


class RuleImportRequest(BaseModel):
    """导入一版《用户画像研判规则》（REQ-M1-03）。

    结构校验在这里（pydantic 报错定位到具体字段），**语义**校验在
    `services/rules.parse_rule_document()`（字段名是否规范、op 是否支持、
    between 是否给了区间……），两层都过不了就不落库。
    """

    version: str = Field("V1.0", max_length=32)
    note: Optional[str] = Field(None, max_length=512)
    activate: bool = True
    source_name: Optional[str] = Field(None, max_length=255)
    products: List[RuleProduct]

    def document(self) -> Dict[str, Any]:
        return {"version": self.version, "note": self.note,
                "products": [p.model_dump() for p in self.products]}


# --------------------------------------------------------------------------- #
# 主动待办推送 / 预警触达
# --------------------------------------------------------------------------- #
class TodoPushRequest(BaseModel):
    """手动触发一轮主动推送。

    `categories` 不传 = 全部类别；`all_staff` 只有管理层能用。
    """

    categories: Optional[List[str]] = None
    all_staff: bool = False


class AlertNotifyRequest(BaseModel):
    """心理预警触达（REQ：立即触发并记录原因，辅助老师介入）。

    `alert_ids` 不传 = 把「还没触达过」的全部触达一遍（定时/批量场景）；
    传了则按 ID 精准重发（例如换处理人后重新通知）。
    """

    alert_ids: Optional[List[int]] = None
    channel: Literal["chat", "internal", "wecom", "sms"] = "chat"


# --------------------------------------------------------------------------- #
# 学员域
# --------------------------------------------------------------------------- #
class ScoreCreate(BaseModel):
    student_id: int
    exam_name: str = Field(..., max_length=64)
    subject: str = Field(..., max_length=32)
    score: float = Field(..., ge=0)
    full_score: float = Field(100, gt=0)
    exam_date: Optional[date] = None


class LeaveApply(BaseModel):
    student_id: int
    request_type: Literal["LEAVE", "EXAM", "OTHER"] = "LEAVE"
    start_date: Optional[date] = None
    days: Optional[int] = Field(None, ge=1, le=180)
    reason: Optional[str] = Field(None, max_length=512)
    idempotency_key: Optional[str] = Field(None, max_length=64)


class ApproveBody(BaseModel):
    approve: bool = True
    remark: Optional[str] = Field(None, max_length=512)


class TicketCreate(BaseModel):
    content: str = Field(..., min_length=1)
    student_id: Optional[int] = None
    category: Optional[str] = None


class TicketUpdate(BaseModel):
    status: Optional[Literal["OPEN", "PROCESSING", "RESOLVED", "CLOSED"]] = None
    handler_id: Optional[int] = None
    category: Optional[str] = None
    satisfaction: Optional[int] = Field(None, ge=0, le=5)


# --------------------------------------------------------------------------- #
# 运营域 / 数据
# --------------------------------------------------------------------------- #
class EnrollBody(BaseModel):
    student_id: Optional[int] = None
    lead_id: Optional[int] = None


class ReportGenerate(BaseModel):
    """生成业务报告。

    `report_type` 用 `str` 而不是 `Literal`：五类口径由 `services/reports.REPORT_TYPES`
    注册表定义，还兼容 weekly/monthly/custom 三个历史值。写成 Literal 会让
    「加一类报告要改两处」，而且报错信息变成 422 而不是能看懂的业务提示。
    合法性在服务层用 `normalize_type()` 校验，失败返回 400 + 可选值清单。
    """

    report_type: str = "weekly_digest"
    title: Optional[str] = Field(None, max_length=128,
                                 description="不传则按「报告名（报告期）」自动生成")
    params: Dict[str, Any] = Field(default_factory=dict)


class ReportPushRequest(BaseModel):
    """报告推送（SRS 4.5.3 / AC-08）。

    `roles` 不传 = 用 `reports.REPORT_TARGET_ROLES` 的默认口径；
    `channel` 不传 = 走站内（未配凭证的通道会降级为站内）。
    """

    roles: Optional[List[str]] = None
    channel: Optional[Literal["internal", "wecom", "sms", "webhook"]] = None


# --------------------------------------------------------------------------- #
# 学业考务（REQ-M4-04）与业务进度明细（REQ-M4-05）
# --------------------------------------------------------------------------- #
class DeadlineItem(BaseModel):
    """一条考务节点。

    `external_id` 给了就按它做幂等 upsert（教务系统对接用）；
    没给就回落到自然键 `(student_id, kind, title, due_at)` 去重，
    所以人工反复导入同一份表也不会翻倍。
    """

    title: str = Field(..., max_length=128)
    due_at: str = Field(..., description="ISO 时间，如 2026-10-01 09:00 或 2026-10-01")
    kind: Literal["DDL", "EXAM", "INTERVIEW", "VISA", "OTHER"] = "DDL"
    subject: Optional[str] = Field(None, max_length=64)
    source: str = Field("manual", max_length=24)
    external_id: Optional[str] = Field(None, max_length=64)
    remind_before_hours: Optional[int] = Field(None, ge=0, le=24 * 30)
    status: Literal["OPEN", "DONE", "CANCELLED"] = "OPEN"
    note: Optional[str] = Field(None, max_length=512)


class DeadlineImport(BaseModel):
    """批量导入考务节点（人工兜底入口，也是将来教务系统同步的同一契约）。"""

    source: str = Field("manual", max_length=24)
    items: List[DeadlineItem]


class DeadlineStatusBody(BaseModel):
    status: Literal["OPEN", "DONE", "CANCELLED"]


class ProgressItem(BaseModel):
    phase: Literal["DOC", "APPLY", "VISA", "OTHER"] = "DOC"
    item: str = Field(..., max_length=128)
    status: Literal["PENDING", "DOING", "DONE", "BLOCKED"] = "PENDING"
    owner_id: Optional[int] = None
    due_at: Optional[str] = None
    source: str = Field("manual", max_length=24)
    external_id: Optional[str] = Field(None, max_length=64)
    note: Optional[str] = Field(None, max_length=512)


class ProgressImport(BaseModel):
    source: str = Field("manual", max_length=24)
    items: List[ProgressItem]


class ReminderRunBody(BaseModel):
    """手动跑一轮提醒（定时任务同一入口）。

    `channel` 不传走站内；未配凭证的通道会降级为站内，`delivered_via` 如实标注。
    """

    channel: Optional[Literal["internal", "wecom", "sms", "webhook"]] = None
    dry_run: bool = Field(False, description="只看会提醒哪些节点，不真的发、不写 reminded_at")


class DailyReportCreate(BaseModel):
    employee_id: int
    report_date: Optional[date] = None
    content: str = Field(..., min_length=1)
    source: Literal["manual", "voice"] = "manual"
    raw_audio_url: Optional[str] = None


class Nl2SqlRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    # 留空则按登录角色推导（employee->sales / manager->manager / admin->admin）；
    # 显式指定时仍会被模板自身的 scopes 二次鉴权，越权直接 403。
    role_scope: str = Field("", description="sales / academic / manager / admin")
    max_rows: int = Field(50, ge=1, le=200)


class KbDocumentCreate(BaseModel):
    title: str = Field(..., max_length=128)
    category: Optional[str] = None
    source_url: Optional[str] = None
    file_path: Optional[str] = None
    version: str = "V1.0"
    dify_dataset_id: Optional[str] = None
    chunk_count: int = 0
