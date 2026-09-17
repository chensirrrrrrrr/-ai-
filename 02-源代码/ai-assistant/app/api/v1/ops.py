"""运营域：活动与报名 / 报告 / 员工日报 / 知识库元数据。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from urllib.parse import quote

from ...config import settings
from ...core import AppError, Conflict, NotFound, ok
from ...db import get_db
from ...models import (Activity, ActivityEnrollment, Employee, EmployeeReport,
                       KnowledgeDoc, ReportRecord)
from ...schemas import (DailyReportCreate, EnrollBody, KbDocumentCreate,
                        ReportGenerate, ReportPushRequest)
from ...services import audit, reports
from ...services.dify import APP_REPORTER, dify_client
from ..deps import Principal, client_ip, get_principal, require_roles

EXPORT_FORMATS = reports.EXPORT_FORMATS


def _content_disposition(filename: str, extension: str) -> str:
    """下载文件名里带中文时的正确写法（RFC 6266）。

    ⚠️ 直接把中文塞进 header 会被 Starlette 按 **latin-1** 编码并抛
    `UnicodeEncodeError`（实测踩到）。必须给一个 ASCII 回退名，再追加
    `filename*=UTF-8''<percent-encoded>`，让浏览器取真实中文名、老客户端取回退名。
    """
    return (f'attachment; filename="report.{extension}"; '
            f"filename*=UTF-8''{quote(f'{filename}.{extension}')}")

router = APIRouter(tags=["operation"])

staff_only = require_roles("employee", "manager", "admin")
manager_only = require_roles("manager", "admin")


# --------------------------------------------------------------------------- #
# 活动
# --------------------------------------------------------------------------- #
@router.get("/activities", summary="活动列表")
def list_activities(status: Optional[str] = None,
                    limit: int = Query(50, ge=1, le=200),
                    principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)) -> dict:
    query = db.query(Activity)
    if status:
        query = query.filter(Activity.status == status)
    rows = query.order_by(Activity.start_at.desc()).limit(limit).all()
    return ok({"total": len(rows),
               "items": [{"id": a.id, "title": a.title, "category": a.category,
                          "start_at": a.start_at.isoformat() if a.start_at else None,
                          "location": a.location, "capacity": a.capacity,
                          "enrolled_count": a.enrolled_count, "status": a.status}
                         for a in rows]})


@router.post("/activities", summary="创建活动")
def create_activity(body: dict, request: Request,
                    principal: Principal = Depends(staff_only),
                    db: Session = Depends(get_db)) -> dict:
    if not body.get("title"):
        raise AppError("title 必填")
    row = Activity(
        title=body["title"], category=body.get("category"),
        description=body.get("description"),
        start_at=datetime.fromisoformat(body["start_at"]) if body.get("start_at") else None,
        end_at=datetime.fromisoformat(body["end_at"]) if body.get("end_at") else None,
        location=body.get("location"), capacity=body.get("capacity"),
        status=body.get("status", "OPEN"),
    )
    db.add(row)
    db.flush()
    audit.record(db, action="activity.create", actor_id=principal.subject,
                 actor_role=principal.role, resource="activity", resource_id=row.id,
                 detail={"title": row.title}, ip=client_ip(request))
    db.commit()
    return ok({"id": row.id, "title": row.title, "status": row.status})


@router.post("/activities/{activity_id}/enroll", summary="活动报名")
def enroll_activity(activity_id: int, body: EnrollBody, request: Request,
                    principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)) -> dict:
    activity = db.get(Activity, activity_id)
    if activity is None:
        raise NotFound(f"活动 {activity_id} 不存在")
    if activity.status != "OPEN":
        raise Conflict(f"活动当前状态为 {activity.status}，不可报名")

    student_id = principal.ref_id if principal.role == "student" else body.student_id
    lead_id = body.lead_id
    if student_id is None and lead_id is None:
        raise AppError("报名需要提供 student_id 或 lead_id")

    if activity.capacity is not None and activity.enrolled_count >= activity.capacity:
        raise Conflict("名额已满")

    row = ActivityEnrollment(activity_id=activity_id, student_id=student_id,
                             lead_id=lead_id, status="ENROLLED")
    db.add(row)
    activity.enrolled_count += 1
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise Conflict("该学生已报名此活动") from exc

    audit.record(db, action="activity.enroll", actor_id=principal.subject,
                 actor_role=principal.role, resource="activity_enrollment", resource_id=row.id,
                 detail={"activity_id": activity_id}, ip=client_ip(request))
    db.commit()
    return ok({"id": row.id, "activity_id": activity_id, "status": row.status,
               "enrolled_count": activity.enrolled_count})


@router.get("/activities/{activity_id}/enrollments", summary="活动报名名单")
def list_enrollments(activity_id: int, principal: Principal = Depends(staff_only),
                     db: Session = Depends(get_db)) -> dict:
    rows = (db.query(ActivityEnrollment)
            .filter(ActivityEnrollment.activity_id == activity_id)
            .order_by(ActivityEnrollment.id.desc()).all())
    return ok({"total": len(rows),
               "items": [{"id": r.id, "student_id": r.student_id, "lead_id": r.lead_id,
                          "status": r.status, "enrolled_at": r.enrolled_at.isoformat()}
                         for r in rows]})


# --------------------------------------------------------------------------- #
# 报告（Dify 报告工作流出数）
# --------------------------------------------------------------------------- #
@router.get("/reports/types", summary="五类业务报告的口径清单")
def report_types(principal: Principal = Depends(manager_only)) -> dict:
    """报告类型 + 默认周期 + 覆盖范围。前端「生成报告」下拉直接用这个，避免前端写死。"""
    return ok({"total": len(reports.REPORT_TYPES), "items": reports.type_catalog(),
               "legacy": dict(reports.LEGACY_TYPES)})


@router.post("/reports/scheduled/run", summary="手动触发一轮定时报告生成（含推送）")
def run_scheduled_reports(force: bool = Query(False, description="忽略「今天该不该生成」"),
                          push: bool = Query(True, description="生成后是否推送给目标角色"),
                          request: Request = None,
                          principal: Principal = Depends(manager_only),
                          db: Session = Depends(get_db)) -> dict:
    """定时任务的同一入口：`force=false` 只生成今天该生成的（日报每天、其余周一）。

    幂等：同一 (类型, 报告期, 定时来源) 已存在就跳过 —— 手动点两次不会生成两份。
    `push=true`（默认）时顺手推送给目标角色，补齐 SRS 4.5.3 / AC-08 的「定时推送」；
    推送本身也是幂等的（同一报告对同一账号只推一次）。
    """
    runner = reports.run_scheduled_with_push if push else reports.run_scheduled
    result = runner(db, force=force)
    audit.record(db, action="report.scheduled_run", actor_id=principal.subject,
                 actor_role=principal.role, resource="report_record", resource_id=None,
                 detail={"due": result["due"], "created": result["created_count"],
                         "skipped": result["skipped_count"], "force": force,
                         "pushed": result.get("pushed_count", 0)},
                 ip=client_ip(request))
    db.commit()
    return ok(result)


@router.post("/reports/{report_id}/push", summary="推送报告给目标角色（SRS 4.5.3 / AC-08）")
def push_report(report_id: int, body: ReportPushRequest, request: Request,
                principal: Principal = Depends(manager_only),
                db: Session = Depends(get_db)) -> dict:
    """按角色把报告推出去。

    角色不传则取 `reports.REPORT_TARGET_ROLES` 的默认口径；
    通道不传走站内（企业微信/短信未配凭证时同样降级站内，并在
    `delivered_via` 上如实标出来）。
    """
    row = db.get(ReportRecord, report_id)
    if row is None:
        raise NotFound(f"报告 {report_id} 不存在")
    outcome = reports.push(db, row, roles=body.roles, channel=body.channel)
    audit.record(db, action="report.push", actor_id=principal.subject,
                 actor_role=principal.role, resource="report_record", resource_id=report_id,
                 detail={"roles": outcome["roles"], "sent": outcome["sent"],
                         "skipped": outcome["skipped"], "channel": body.channel},
                 ip=client_ip(request))
    db.commit()
    outcome["context"] = {
        "requested_roles": outcome["roles"],
        "channel": body.channel or settings.notify_default_channel,
        "ready_channels": settings.notify_ready_channels,
        "note": "未配置凭证的通道会自动降级为站内，delivered_via 字段可直接看出实际通道",
    }
    return ok(outcome)


@router.get("/reports/{report_id}/deliveries", summary="报告推送记录")
def report_deliveries(report_id: int, principal: Principal = Depends(manager_only),
                      db: Session = Depends(get_db)) -> dict:
    row = db.get(ReportRecord, report_id)
    if row is None:
        raise NotFound(f"报告 {report_id} 不存在")
    items = reports.deliveries(db, report_id)
    return ok({"report_id": report_id, "total": len(items), "items": items,
               "targets": reports.REPORT_TARGET_ROLES.get(
                   reports.normalize_type(row.report_type), ())})



@router.post("/reports/generate", summary="生成业务报告（五类口径，含真实聚合）")
async def generate_report(body: ReportGenerate, request: Request,
                          principal: Principal = Depends(manager_only),
                          db: Session = Depends(get_db)) -> dict:
    """先做**真实数据聚合**（`services.reports.build`），再按需叠一段 Dify 叙述。

    接上真实 Dify 时，`reporter` 工作流收到的是我们已经算好的统计口径，
    它只负责「把数字讲成人话」；mock 模式下叙述由本地模板渲染。
    **两条路的数字完全一致**，不会出现「mock 报告和 live 报告对不上」。
    """
    try:
        report_key = reports.normalize_type(body.report_type)
    except ValueError as exc:
        raise AppError(str(exc)) from exc

    params = dict(body.params or {})
    start, end = reports.resolve_period(
        report_key, start=params.get("period_start") or params.get("start"),
        end=params.get("period_end") or params.get("end"))
    snapshot = reports.build(db, report_key, start, end)

    outputs = await dify_client.run_workflow(
        APP_REPORTER,
        {"report_type": report_key, "title": body.title,
         "period_start": start.isoformat(), "period_end": end.isoformat(),
         "metrics": snapshot["metrics"], "summary": snapshot["summary"]},
        f"uas-{principal.subject}",
    )
    data = outputs.get("outputs", {})

    row = reports.generate(db, report_type=report_key, params=params,
                           period=(start, end), title=body.title,
                           generated_by=principal.ref_id,
                           dify_runner={"id": outputs.get("id"),
                                        "content": data.get("content")
                                                   if settings.is_dify_live else ""})
    if not row.content:
        row.status = "FAILED"

    audit.record(db, action="report.generate", actor_id=principal.subject,
                 actor_role=principal.role, resource="report_record", resource_id=row.id,
                 detail={"report_type": report_key, "period": row.snapshot["period"]["label"],
                         "status": row.status},
                 ip=client_ip(request))
    db.commit()
    db.refresh(row)
    return ok({"id": row.id, "status": row.status, "title": row.title,
               "report_type": row.report_type,
               "period": row.snapshot["period"],
               "summary": row.snapshot["summary"],
               "metrics": row.snapshot["metrics"],
               "export_formats": EXPORT_FORMATS,
               "preview": (row.content or "")[:200]})


@router.get("/reports", summary="报告列表（支持按类型/来源筛选 + 分页）")
def list_reports(report_type: Optional[str] = None,
                 from_schedule: Optional[bool] = Query(
                     None, description="true=只看定时生成 / false=只看人工生成"),
                 limit: int = Query(50, ge=1, le=200),
                 offset: int = Query(0, ge=0),
                 principal: Principal = Depends(manager_only),
                 db: Session = Depends(get_db)) -> dict:
    """报告列表。

    ⚠️ 这里曾经是「取最新 50 条」，没有筛选也没有分页 —— 报告一多就出现两个问题：
    1. 老报告永远够不到（第 51 条之后在前端不存在）；
    2. 「定时报告」会被后来源源不断的人工报告挤出可视窗口，
       连"今天定时任务到底生成了没有"都看不出来。
    所以补上 `from_schedule` 筛选与 `limit/offset`，
    并把 `total` 改成**筛选后的真实总数**（原来是当页条数，很容易被误读成总量）。
    """
    query = db.query(ReportRecord)
    if report_type:
        # 兼容旧值：按 `weekly` 查时也要能查到 weekly_digest
        try:
            query = query.filter(ReportRecord.report_type
                                 == reports.normalize_type(report_type))
        except ValueError:
            query = query.filter(ReportRecord.report_type == report_type)
    if from_schedule is not None:
        query = query.filter(ReportRecord.from_schedule.is_(bool(from_schedule)))

    total = query.count()
    scheduled = (query.filter(ReportRecord.from_schedule.is_(True)).count())
    rows = (query.order_by(ReportRecord.id.desc())
            .offset(offset).limit(limit).all())
    return ok({"total": total, "count": len(rows), "offset": offset, "limit": limit,
               "scheduled_total": scheduled,
               "types": [{"key": key, **meta} for key, meta in reports.REPORT_TYPES.items()],
               "items": [{"id": r.id, "title": r.title, "report_type": r.report_type,
                          "status": r.status,
                          "period": (r.snapshot or {}).get("period"),
                          "summary": (r.snapshot or {}).get("summary"),
                          "from_schedule": bool(r.from_schedule),
                          "exports": r.exported_formats or [],
                          "created_at": r.created_at.isoformat()}
                         for r in rows]})


@router.get("/reports/{report_id}", summary="报告详情（结构化快照）")
def get_report(report_id: int, principal: Principal = Depends(manager_only),
               db: Session = Depends(get_db)) -> dict:
    row = db.get(ReportRecord, report_id)
    if row is None:
        raise NotFound(f"报告 {report_id} 不存在")
    return ok({"id": row.id, "title": row.title, "status": row.status,
               "report_type": row.report_type, "content": row.content,
               "snapshot": row.snapshot, "params": row.params,
               "period_start": row.period_start.isoformat() if row.period_start else None,
               "period_end": row.period_end.isoformat() if row.period_end else None,
               "from_schedule": bool(row.from_schedule),
               "exports": row.exported_formats or [],
               "export_formats": EXPORT_FORMATS,
               "dify_run_id": row.dify_run_id,
               "created_at": row.created_at.isoformat()})


@router.get("/reports/{report_id}/export", summary="导出报告（xlsx / pdf / print / md）")
def export_report(report_id: int, format: str = Query("xlsx", alias="format"),
                  request: Request = None,
                  principal: Principal = Depends(manager_only),
                  db: Session = Depends(get_db)) -> Response:
    """导出只读 `snapshot`，**不重新聚合** —— 否则导出件的数字可能和页面上的不一致。

    - `xlsx`：真 .xlsx（多 sheet，标准库写出）
    - `print`：打印就绪 HTML，浏览器「另存为 PDF」；**中文渲染一定正确**，推荐路径
    - `pdf`：服务端直出 PDF（矢量文字、可提取）。用的是 PDF 预定义 CJK 字体、
      不嵌字体文件 —— 桌面阅读器（Acrobat/WPS/Foxit）正常，Chrome/Edge 内置阅读器
      可能显示异常，所以在 UI 上把 `print` 作为主推荐。
    - `md`：markdown 原文
    """
    row = db.get(ReportRecord, report_id)
    if row is None:
        raise NotFound(f"报告 {report_id} 不存在")
    snapshot = row.snapshot
    if not snapshot:
        raise Conflict(f"报告 {report_id} 没有结构化快照，重新生成后再导出")

    fmt = (format or "xlsx").lower()
    if fmt not in EXPORT_FORMATS:
        raise AppError(f"不支持导出格式 {fmt!r}；可选：{list(EXPORT_FORMATS)}")

    stamp = (snapshot["period"]["label"]).replace(" ~ ", "_").replace(" ", "")
    filename = f"{snapshot['title']}_{stamp}"

    if fmt == "xlsx":
        payload = reports.render_excel(snapshot)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        headers = {"Content-Disposition": _content_disposition(filename, "xlsx")}
    elif fmt == "pdf":
        payload = reports.render_pdf(snapshot)
        media = "application/pdf"
        headers = {"Content-Disposition": _content_disposition(filename, "pdf")}
    elif fmt == "print":
        payload = reports.render_print_html(snapshot).encode("utf-8")
        media = "text/html; charset=utf-8"
        headers = {"Content-Disposition": "inline"}
    else:
        payload = (row.content or "").encode("utf-8")
        media = "text/markdown; charset=utf-8"
        headers = {"Content-Disposition": _content_disposition(filename, "md")}

    # 导出留痕：谁把哪份报告导成了什么格式（合规审计常见要求）
    formats = list(row.exported_formats or [])
    if fmt not in formats:
        formats.append(fmt)
        row.exported_formats = formats
        audit.record(db, action="report.export", actor_id=principal.subject,
                     actor_role=principal.role, resource="report_record",
                     resource_id=report_id, detail={"format": fmt, "bytes": len(payload)},
                     ip=client_ip(request))
        db.commit()

    return Response(content=payload, media_type=media, headers=headers)


@router.get("/reports/{report_id}/download", summary="下载报告（markdown 文本）",
            response_class=PlainTextResponse)
def download_report(report_id: int, principal: Principal = Depends(manager_only),
                    db: Session = Depends(get_db)) -> PlainTextResponse:
    row = db.get(ReportRecord, report_id)
    if row is None:
        raise NotFound(f"报告 {report_id} 不存在")
    filename = f"report_{report_id}.md"
    return PlainTextResponse(
        row.content or "",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        media_type="text/markdown; charset=utf-8",
    )


# --------------------------------------------------------------------------- #
# 员工日报（支持语音录入）
# --------------------------------------------------------------------------- #
@router.post("/reports/daily", summary="提交员工日报（manual / voice）")
def submit_daily_report(body: DailyReportCreate, request: Request,
                        principal: Principal = Depends(staff_only),
                        db: Session = Depends(get_db)) -> dict:
    if db.get(Employee, body.employee_id) is None:
        raise NotFound(f"员工 {body.employee_id} 不存在")

    report_date = body.report_date or date.today()
    dup = (db.query(EmployeeReport)
           .filter(EmployeeReport.employee_id == body.employee_id,
                   EmployeeReport.report_date == report_date).first())
    row = dup or EmployeeReport(employee_id=body.employee_id, report_date=report_date,
                               content=body.content)
    row.content = body.content
    row.summary = body.content[:80]
    row.source = body.source
    row.raw_audio_url = body.raw_audio_url
    if dup is None:
        db.add(row)
    db.flush()

    audit.record(db, action="report.daily_submit", actor_id=principal.subject,
                 actor_role=principal.role, resource="employee_report", resource_id=row.id,
                 detail={"employee_id": body.employee_id, "source": body.source},
                 ip=client_ip(request))
    db.commit()
    return ok({"id": row.id, "report_date": report_date.isoformat(),
               "updated": dup is not None, "source": row.source})


@router.get("/reports/daily/summary", summary="日报汇总（按日）")
def daily_summary(report_date: Optional[date] = None,
                  principal: Principal = Depends(staff_only),
                  db: Session = Depends(get_db)) -> dict:
    target = report_date or date.today()
    rows = (db.query(EmployeeReport)
            .filter(EmployeeReport.report_date == target).all())
    submitted = {r.employee_id for r in rows}
    total_emp = db.query(func.count(Employee.id)).filter(Employee.status == "ACTIVE").scalar() or 0
    missing = [e.id for e in db.query(Employee).filter(Employee.status == "ACTIVE").all()
               if e.id not in submitted]
    return ok({
        "date": target.isoformat(),
        "total_employees": total_emp,
        "submitted": len(submitted),
        "missing": missing,
        "voice_sourced": sum(1 for r in rows if r.source == "voice"),
        "items": [{"employee_id": r.employee_id, "summary": r.summary,
                   "source": r.source, "content": r.content} for r in rows],
    })


@router.get("/reports/daily/trend", summary="日报提交趋势（近 N 天）")
def daily_trend(days: int = Query(7, ge=1, le=30),
                principal: Principal = Depends(staff_only),
                db: Session = Depends(get_db)) -> dict:
    start = date.today() - timedelta(days=days - 1)
    rows = (db.query(EmployeeReport.report_date, func.count(EmployeeReport.id))
            .filter(EmployeeReport.report_date >= start)
            .group_by(EmployeeReport.report_date).all())
    series = {d.isoformat(): c for d, c in rows}
    return ok({"days": days, "start": start.isoformat(),
               "series": [{"date": (start + timedelta(days=i)).isoformat(),
                           "count": series.get((start + timedelta(days=i)).isoformat(), 0)}
                          for i in range(days)]})


# --------------------------------------------------------------------------- #
# 知识库元数据（真实切片在 Dify 数据集）
# --------------------------------------------------------------------------- #
@router.post("/kb/documents", summary="登记知识文档（与 Dify 数据集建立映射）")
def create_kb_document(body: KbDocumentCreate, request: Request,
                       principal: Principal = Depends(staff_only),
                       db: Session = Depends(get_db)) -> dict:
    row = KnowledgeDoc(**body.model_dump(), status="INDEXED" if body.dify_dataset_id else "DRAFT",
                       effective_at=datetime.now())
    db.add(row)
    db.flush()
    audit.record(db, action="kb.document_create", actor_id=principal.subject,
                 actor_role=principal.role, resource="knowledge_doc", resource_id=row.id,
                 detail={"title": body.title}, ip=client_ip(request))
    db.commit()
    return ok({"id": row.id, "title": row.title, "status": row.status,
               "dify_dataset_id": row.dify_dataset_id})


@router.get("/kb/documents", summary="知识文档列表")
def list_kb_documents(category: Optional[str] = None, status: Optional[str] = None,
                      principal: Principal = Depends(staff_only),
                      db: Session = Depends(get_db)) -> dict:
    query = db.query(KnowledgeDoc)
    if category:
        query = query.filter(KnowledgeDoc.category == category)
    if status:
        query = query.filter(KnowledgeDoc.status == status)
    rows = query.order_by(KnowledgeDoc.id.desc()).limit(100).all()
    return ok({"total": len(rows),
               "items": [{"id": d.id, "title": d.title, "category": d.category,
                          "version": d.version, "chunk_count": d.chunk_count,
                          "status": d.status} for d in rows]})
