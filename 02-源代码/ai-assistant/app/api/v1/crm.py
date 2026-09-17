"""客户域：意向客户 / 跟进记录 / 客户研判（含材料解析、人工复核、批量研判）。"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import (APIRouter, Depends, File, Form, Query, Request,
                     UploadFile)
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...config import settings
from ...core import AppError, Conflict, NotFound, ok
from ...db import get_db
from ...models import CustomerFollowup, CustomerLead, LeadScreening, ScreeningRule
from ...schemas import (BatchScreeningItem, BatchScreeningRequest,
                        FollowupCreate, LeadCreate, LeadStatusUpdate,
                        RuleImportRequest, ScreeningRequest, ScreeningReviewRequest)
from ...services import audit, material, rules
from ...services.dify import APP_SCREENER, dify_client
from ..deps import Principal, client_ip, get_principal, require_roles

router = APIRouter(tags=["crm"])

staff_only = require_roles("employee", "manager", "admin")
manager_only = require_roles("manager", "admin")


@router.post("/leads", summary="新增意向客户")
def create_lead(body: LeadCreate, request: Request,
                principal: Principal = Depends(staff_only),
                db: Session = Depends(get_db)) -> dict:
    exists = db.query(CustomerLead).filter(CustomerLead.phone == body.phone).first()
    if exists:
        raise Conflict(f"手机号 {body.phone} 已存在意向客户记录（id={exists.id}）")

    lead = CustomerLead(**body.model_dump())
    if lead.owner_id is None and principal.ref_id:
        lead.owner_id = principal.ref_id
    db.add(lead)
    db.flush()
    audit.record(db, action="lead.create", actor_id=principal.subject, actor_role=principal.role,
                 resource="customer_lead", resource_id=lead.id,
                 detail={"phone": body.phone}, ip=client_ip(request))
    db.commit()
    db.refresh(lead)
    return ok({"id": lead.id, "status": lead.status, "owner_id": lead.owner_id})


@router.get("/leads", summary="查询意向客户列表")
def list_leads(status: Optional[str] = None,
               keyword: Optional[str] = None,
               owner_id: Optional[int] = None,
               page: int = Query(1, ge=1),
               page_size: int = Query(20, ge=1, le=100),
               principal: Principal = Depends(staff_only),
               db: Session = Depends(get_db)) -> dict:
    query = db.query(CustomerLead)
    if status:
        query = query.filter(CustomerLead.status == status)
    if keyword:
        query = query.filter(CustomerLead.name.like(f"%{keyword}%"))
    if owner_id:
        query = query.filter(CustomerLead.owner_id == owner_id)

    total = query.count()
    items = (query.order_by(CustomerLead.id.desc())
             .offset((page - 1) * page_size).limit(page_size).all())
    return ok({
        "total": total, "page": page, "page_size": page_size,
        "items": [{"id": x.id, "name": x.name, "phone": x.phone, "source": x.source,
                   "intention_country": x.intention_country, "status": x.status,
                   "owner_id": x.owner_id, "created_at": x.created_at.isoformat()}
                  for x in items],
    })


@router.get("/leads/{lead_id}", summary="意向客户详情")
def get_lead(lead_id: int, principal: Principal = Depends(staff_only),
             db: Session = Depends(get_db)) -> dict:
    lead = db.get(CustomerLead, lead_id)
    if lead is None:
        raise NotFound(f"意向客户 {lead_id} 不存在")
    followups = (db.query(CustomerFollowup)
                 .filter(CustomerFollowup.lead_id == lead_id)
                 .order_by(CustomerFollowup.created_at.desc()).all())
    return ok({
        "lead": {"id": lead.id, "name": lead.name, "phone": lead.phone,
                 "source": lead.source, "intention_country": lead.intention_country,
                 "intention_stage": lead.intention_stage, "status": lead.status,
                 "owner_id": lead.owner_id, "remark": lead.remark},
        "followups": [{"id": f.id, "content": f.content, "follow_type": f.follow_type,
                       "created_at": f.created_at.isoformat()} for f in followups],
    })


@router.patch("/leads/{lead_id}/status", summary="更新意向客户状态")
def update_lead_status(lead_id: int, body: LeadStatusUpdate, request: Request,
                       principal: Principal = Depends(staff_only),
                       db: Session = Depends(get_db)) -> dict:
    lead = db.get(CustomerLead, lead_id)
    if lead is None:
        raise NotFound(f"意向客户 {lead_id} 不存在")
    before = lead.status
    lead.status = body.status
    if body.remark:
        lead.remark = body.remark
    audit.record(db, action="lead.status_change", actor_id=principal.subject,
                 actor_role=principal.role, resource="customer_lead", resource_id=lead_id,
                 detail={"before": before, "after": body.status}, ip=client_ip(request))
    db.commit()
    return ok({"id": lead_id, "before": before, "status": lead.status})


@router.post("/leads/{lead_id}/followups", summary="新增跟进记录（支持幂等键）")
def add_followup(lead_id: int, body: FollowupCreate, request: Request,
                 principal: Principal = Depends(staff_only),
                 db: Session = Depends(get_db)) -> dict:
    lead = db.get(CustomerLead, lead_id)
    if lead is None:
        raise NotFound(f"意向客户 {lead_id} 不存在")

    if body.idempotency_key:
        dup = (db.query(CustomerFollowup)
               .filter(CustomerFollowup.idempotency_key == body.idempotency_key).first())
        if dup:
            return ok({"id": dup.id, "duplicated": True})

    row = CustomerFollowup(
        lead_id=lead_id, content=body.content, follow_type=body.follow_type,
        next_plan=body.next_plan, next_follow_at=body.next_follow_at,
        owner_id=principal.ref_id, idempotency_key=body.idempotency_key,
    )
    db.add(row)
    # 首次跟进自动把客户推进到 FOLLOWING
    if lead.status == "NEW":
        lead.status = "FOLLOWING"
    db.flush()
    audit.record(db, action="lead.followup_create", actor_id=principal.subject,
                 actor_role=principal.role, resource="customer_followup", resource_id=row.id,
                 detail={"lead_id": lead_id}, ip=client_ip(request))
    db.commit()
    db.refresh(row)
    return ok({"id": row.id, "lead_id": lead_id, "created_at": row.created_at.isoformat(),
               "duplicated": False})


@router.get("/leads/{lead_id}/followups", summary="查询某客户的跟进记录")
def list_followups(lead_id: int, principal: Principal = Depends(staff_only),
                   db: Session = Depends(get_db)) -> dict:
    rows = (db.query(CustomerFollowup)
            .filter(CustomerFollowup.lead_id == lead_id)
            .order_by(CustomerFollowup.created_at.desc()).all())
    return ok({"total": len(rows),
               "items": [{"id": r.id, "content": r.content, "follow_type": r.follow_type,
                          "next_plan": r.next_plan, "owner_id": r.owner_id,
                          "created_at": r.created_at.isoformat()} for r in rows]})


# --------------------------------------------------------------------------- #
# 材料研判（M1）
#
# 路由声明顺序有讲究：`/screening/{screening_id}` 会吃掉任何同段路径，
# 所以 `/screening/upload`、`/screening/batch`、`/screening/corrections`、
# `/screening/files/{...}` 必须**排在它前面**（否则 "corrections" 会被当成
# int 主键解析失败 → 422）。
# --------------------------------------------------------------------------- #
SCREENING_SEGMENT = "screening"

# 落盘文件名里只留这些字符，避免出现 ../ 或 Windows 非法字符
_UNSAFE_NAME_RE = re.compile(r"[^0-9A-Za-z._\-\u4e00-\u9fa5]")
# 落盘时前面加了 10 位随机前缀防重名，回读时要能还原出原始文件名
_STORED_PREFIX_RE = re.compile(r"^[0-9a-f]{10}_(?P<name>.+)$")


def _upload_root() -> Path:
    root = Path(settings.upload_dir)
    if not root.is_absolute():
        root = Path.cwd() / root        # 与 DATABASE_URL 同口径：相对项目根
    return root / SCREENING_SEGMENT


def _safe_name(name: str) -> str:
    base = Path(name or "material").name
    cleaned = _UNSAFE_NAME_RE.sub("_", base).strip("._")
    return (cleaned or "material")[:80]


def _store_material(filename: str, content: bytes) -> str:
    """落盘并返回可回访的相对路径（形如 `2026-09-15/ab12cd34_简历.pdf`）。

    生产接对象存储时只改这里 + `raw_file_url` 的拼法，接口契约不变。
    """
    folder = datetime.now().strftime("%Y-%m-%d")
    target_dir = _upload_root() / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex[:10]}_{_safe_name(filename)}"
    (target_dir / stored).write_bytes(content)
    return f"{folder}/{stored}"


def _original_name(stored: str) -> str:
    """从落盘文件名还原上传时的原始文件名（`ab12cd34ef_简历.pdf` → `简历.pdf`）。

    没有这步的话，「只给 raw_file_url 再发起研判」这条链路会把研判记录的材料名
    写成带随机前缀的落盘名，业务侧看着莫名其妙。
    """
    base = Path(stored).name
    match = _STORED_PREFIX_RE.match(base)
    return match.group("name") if match else base


def _resolve_upload_path(rel_path: str) -> Path:
    root = _upload_root().resolve()
    target = (root / (rel_path or "")).resolve()
    # 目录穿越（../../etc/passwd）一律按「不存在」处理，不回显真实路径
    if target != root and root not in target.parents:
        raise NotFound("材料文件不存在")
    if not target.is_file():
        raise NotFound(f"材料文件不存在：{rel_path}")
    return target


def _rel_from_url(raw_file_url: Optional[str]) -> Optional[str]:
    """把 `/api/v1/screening/files/<rel>` 还原成落盘相对路径（其余一律忽略）。"""
    if not raw_file_url:
        return None
    prefix = f"{settings.api_v1_prefix}/screening/files/"
    if not raw_file_url.startswith(prefix):
        return None
    return raw_file_url[len(prefix):].lstrip("/")


def _preview(text: str, limit: int = 600) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _evidence_with_warnings(evidence: Any, warnings: List[str]) -> List[dict]:
    """把解析告警并入 evidence —— REQ-M1-05 要求依据可追溯，
    「这段正文是兜底解析器抽出来的」本身就该是可追溯信息的一部分。"""
    items: List[dict] = list(evidence or [])
    for warning in warnings or []:
        items.append({"rule": "材料解析提示", "excerpt": warning})
    return items


def _screening_brief(row: LeadScreening) -> dict:
    return {
        "id": row.id,
        "lead_id": row.lead_id,
        "source_type": row.source_type,
        "source_name": row.source_name,
        "batch_id": row.batch_id,
        "conclusion": row.conclusion,
        "ai_conclusion": row.ai_conclusion,
        "review_status": row.review_status,
        "revised": row.review_status == "OVERRIDDEN",
        "hit_products": row.hit_products,
        "confidence": float(row.confidence or 0),
        "missing_fields": row.missing_fields or [],
        "dify_run_id": row.dify_run_id,
        "rule_version": row.rule_version,
        "rule_source": row.rule_source,
    }


def _screening_detail(row: LeadScreening) -> dict:
    detail = _screening_brief(row)
    detail.update({
        "extracted_fields": row.extracted_fields or {},
        "evidence": row.evidence or [],
        "raw_file_url": row.raw_file_url,
        "reviewed_by": row.reviewed_by,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "review_remark": row.review_remark,
        "created_at": row.created_at.isoformat(),
    })
    return detail


def _resolve_text(source_type: str, text: Optional[str],
                  raw_file_url: Optional[str],
                  source_name: Optional[str]) -> Tuple[str, Optional[str], List[str]]:
    """拿到送研判的正文：优先显式 text，其次按已上传文件重解析。

    「重解析」这条很重要：客户端只上传不解析（例如第三方系统直接调上传接口），
    随后凭 raw_file_url 发起研判也能跑通，不必自己再读一遍文件。
    """
    if text and text.strip():
        return text.strip(), source_name, []

    rel_path = _rel_from_url(raw_file_url)
    if rel_path:
        path = _resolve_upload_path(rel_path)
        result = material.parse_material(path.name, path.read_bytes())
        return result.text, source_name or _original_name(path.name), list(result.warnings)

    if source_type == "TEXT":
        raise AppError("source_type=TEXT 时必须提供 text")
    raise AppError(
        f"source_type={source_type} 时必须提供已上传材料的 raw_file_url（或直接给出解析后的 text）")


async def _screen_one(db: Session, principal: Principal, *,
                      lead_id: Optional[int], source_type: str,
                      text: Optional[str], raw_file_url: Optional[str],
                      source_name: Optional[str] = None,
                      batch_id: Optional[str] = None) -> LeadScreening:
    """跑一次研判并落一行结果（不 commit，由调用方决定事务边界）。

    判定口径（顺序很重要）
    ----------------------
    1. **字段**：本地规则抽取优先，Dify 侧返回值只补空缺。本地规则确定性、
       可复现、可解释（REQ-M1-05 要求可追溯），Dify 在 mock 下是占位实现。
    2. **结论**：只要库里有生效的《用户画像研判规则》（REQ-M1-03），就以
       **本地规则引擎**的判定为准（`services/rules.py`），并把规则版本写进
       `rule_version`；库里的规则被清空时才回落到 Dify 结论。
       理由：SRS 4.1.5 要求「以甲方提供的规则文件为唯一依据」「结论可追溯到
       规则条目 + 原文片段」—— 这两条只有规则引擎能保证。
    3. Dify 的结论**不丢弃**：与规则引擎不一致时作为一条 evidence 记下来，
       这正是下一版规则该重点看的地方（和 `/screening/corrections` 一个思路）。
    """
    if lead_id is not None and db.get(CustomerLead, lead_id) is None:
        raise NotFound(f"意向客户 {lead_id} 不存在")

    resolved_text, resolved_name, warnings = _resolve_text(
        source_type, text, raw_file_url, source_name)

    outputs = await dify_client.run_workflow(
        APP_SCREENER,
        {"text": resolved_text, "source_type": source_type,
         "raw_file_url": raw_file_url or ""},
        f"uas-{principal.subject}",
    )
    data = outputs.get("outputs", {})
    dify_conclusion = data.get("conclusion") or None

    fields = material.merge_fields(
        material.extract_fields(resolved_text),
        material.normalize_fields(data.get("extracted_fields")),
    )

    active_rules = rules.load_active(db)
    verdict = rules.evaluate(active_rules, fields, resolved_text) if active_rules else None

    if verdict is not None:
        conclusion = verdict["conclusion"]
        # `hit_products` 存**每个产品各自的判定**（REQ-M1-04 明确要求「双产品
        # 分别输出是否符合」，只存命中的那几个产品会丢掉「另一个为什么没符合」）。
        hit_products = [{
            "key": p["product_key"], "name": p["product_name"],
            "conclusion": p["conclusion"], "match": p["match"],
            "confidence": p["confidence"], "rule_version": p["version"],
            "reason": p["reason"],
        } for p in verdict["products"]]
        confidence = verdict["confidence"]
        evidence = _evidence_with_warnings(verdict["evidence"], warnings)
        if dify_conclusion and dify_conclusion != conclusion:
            evidence.append({
                "product": "—",
                "rule": "dify_disagree",
                "desc": "Dify 侧结论与规则引擎不一致（以规则引擎为准，供规则优化参考）",
                "actual": dify_conclusion,
                "expect": conclusion,
                "hit": False,
            })
        rule_version = "、".join(f"{k}:{v}" for k, v in verdict["rule_versions"].items())
        rule_source = "local"
    else:
        # 没有生效规则 —— 如实标明来源，不要假装是规则引擎判的
        conclusion = dify_conclusion or "信息不足"
        hit_products = data.get("hit_products")
        confidence = data.get("confidence")
        evidence = _evidence_with_warnings(data.get("evidence"), warnings)
        rule_version = None
        rule_source = "dify"

    row = LeadScreening(
        lead_id=lead_id,
        source_type=source_type,
        source_name=resolved_name,
        batch_id=batch_id,
        raw_file_url=raw_file_url,
        extracted_fields=fields,
        missing_fields=material.missing_fields(fields),
        hit_products=hit_products,
        conclusion=conclusion,
        ai_conclusion=conclusion,          # AI 原结论快照，供复核留痕与规则优化
        evidence=evidence,
        confidence=confidence,
        review_status="PENDING",
        dify_run_id=outputs.get("id"),
        rule_version=rule_version,
        rule_source=rule_source,
    )
    db.add(row)
    db.flush()
    return row


@router.post("/screening/upload", summary="上传材料并解析（PDF / Excel / 文本）")
async def upload_material(request: Request,
                          file: UploadFile = File(...),
                          lead_id: Optional[int] = Form(None),
                          principal: Principal = Depends(staff_only),
                          db: Session = Depends(get_db)) -> dict:
    """REQ-M1-01 多格式接入 + REQ-M1-02 文档解析与字段抽取。

    只做「落盘 + 解析 + 抽字段」，**不落研判记录** —— 让用户在界面上先看到
    抽出来的字段、必要时改正文，再决定发起研判（human-in-the-loop）。
    """
    filename = file.filename or "material"
    content = await file.read()
    if lead_id is not None and db.get(CustomerLead, lead_id) is None:
        raise NotFound(f"意向客户 {lead_id} 不存在")

    result = material.parse_material(filename, content)
    rel_path = _store_material(filename, content)
    fields = material.extract_fields(result.text)

    payload = result.to_dict()
    payload.update({
        "stored_path": rel_path,
        "raw_file_url": f"{settings.api_v1_prefix}/screening/files/{rel_path}",
        "lead_id": lead_id,
        "fields": fields,
        "field_rows": material.field_rows(fields),
        "missing_fields": material.missing_fields(fields),
        "preview": _preview(result.text),
    })
    audit.record(db, action="screening.upload", actor_id=principal.subject,
                 actor_role=principal.role, resource="lead_screening", resource_id=None,
                 detail={"filename": filename, "source_type": result.source_type,
                         "parser": result.parser, "size": result.size,
                         "chars": len(result.text), "stored_path": rel_path},
                 ip=client_ip(request))
    db.commit()
    return ok(payload)


@router.get("/screening/files/{rel_path:path}", summary="下载已上传的材料原件")
def download_material(rel_path: str,
                      principal: Principal = Depends(staff_only)) -> FileResponse:
    target = _resolve_upload_path(rel_path)
    return FileResponse(target, filename=_original_name(target.name))


def _items_from_leads(db: Session, lead_ids: List[int]) -> List[BatchScreeningItem]:
    """按客户档案 + 最近 3 条跟进拼出材料正文。

    刻意放服务端：档案里的意向国家/阶段/来源正是研判最需要的输入，
    后端拼装口径统一，前端只管勾选客户，不必自己造文本。
    """
    items: List[BatchScreeningItem] = []
    for lead_id in lead_ids:
        lead = db.get(CustomerLead, lead_id)
        if lead is None:
            raise NotFound(f"意向客户 {lead_id} 不存在")
        parts = [
            f"客户姓名：{lead.name}",
            f"意向国家：{lead.intention_country or '未填写'}",
            f"意向阶段：{lead.intention_stage or '未填写'}",
            f"线索来源：{lead.source or '未填写'}",
        ]
        if lead.remark:
            parts.append(f"备注：{lead.remark}")
        followups = (db.query(CustomerFollowup)
                     .filter(CustomerFollowup.lead_id == lead_id)
                     .order_by(CustomerFollowup.created_at.desc()).limit(3).all())
        for followup in reversed(followups):
            parts.append(f"跟进记录：{followup.content}")
        items.append(BatchScreeningItem(lead_id=lead_id, source_type="TEXT",
                                        text="\n".join(parts),
                                        source_name=f"客户档案-{lead.name}"))
    return items


@router.post("/screening/batch", summary="批量研判（批量材料 / 批量客户 → 结果清单）")
async def batch_analyze(body: BatchScreeningRequest, request: Request,
                        principal: Principal = Depends(staff_only),
                        db: Session = Depends(get_db)) -> dict:
    """REQ-M1-07 批量上传与批处理，输出研判结果清单。

    单条失败用 SAVEPOINT 隔离（`begin_nested`），只回滚这一条 —— 一条材料格式
    不对不该毁掉整批已成功的结果，这也是「批量」这个功能的基本盘。
    """
    items: List[BatchScreeningItem] = list(body.items or [])
    if body.lead_ids:
        items.extend(_items_from_leads(db, body.lead_ids))
    if not items:
        raise AppError("批量研判至少要给一条材料（items）或一个客户（lead_ids）")
    if len(items) > settings.screening_batch_max:
        raise AppError(
            f"单次批量最多 {settings.screening_batch_max} 条，本次 {len(items)} 条，请分批提交")

    batch_id = body.batch_id or f"batch-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
    results: List[Dict[str, Any]] = []
    succeeded = failed = 0

    for index, item in enumerate(items):
        try:
            with db.begin_nested():
                row = await _screen_one(
                    db, principal, lead_id=item.lead_id, source_type=item.source_type,
                    text=item.text, raw_file_url=item.raw_file_url,
                    source_name=item.source_name, batch_id=batch_id)
            succeeded += 1
            results.append({
                "index": index, "ok": True, "id": row.id,
                "source_name": row.source_name, "lead_id": row.lead_id,
                "conclusion": row.conclusion, "confidence": float(row.confidence or 0),
                "missing_fields": row.missing_fields or [], "error": None,
            })
        except AppError as exc:
            failed += 1
            results.append({
                "index": index, "ok": False, "id": None,
                "source_name": item.source_name, "lead_id": item.lead_id,
                "conclusion": None, "confidence": 0, "missing_fields": [],
                "error": exc.message,
            })
        except Exception as exc:                          # noqa: BLE001 - 单条异常不牵连整批
            failed += 1
            results.append({
                "index": index, "ok": False, "id": None,
                "source_name": item.source_name, "lead_id": item.lead_id,
                "conclusion": None, "confidence": 0, "missing_fields": [],
                "error": f"{type(exc).__name__}: {exc}",
            })
        if failed and body.stop_on_error:
            break

    by_conclusion: Dict[str, int] = {}
    for item in results:
        if item["ok"]:
            by_conclusion[item["conclusion"]] = by_conclusion.get(item["conclusion"], 0) + 1

    audit.record(db, action="screening.batch", actor_id=principal.subject,
                 actor_role=principal.role, resource="lead_screening", resource_id=batch_id,
                 detail={"batch_id": batch_id, "total": len(items),
                         "succeeded": succeeded, "failed": failed},
                 ip=client_ip(request))
    db.commit()
    return ok({
        "batch_id": batch_id,
        "total": len(items),
        "processed": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "by_conclusion": by_conclusion,
        "items": results,
    })


@router.get("/screening/corrections", summary="人工修正清单（回写用于规则优化）")
def list_corrections(limit: int = Query(50, ge=1, le=200),
                     principal: Principal = Depends(staff_only),
                     db: Session = Depends(get_db)) -> dict:
    """REQ-M1-06 的「修正结果回写用于规则优化」。

    只列被推翻的记录，并把 `AI 原结论 → 人工结论` 的迁移分布统计出来 ——
    哪一类误判最多，就是下一版《用户画像研判规则》该先改的地方。
    """
    rows = (db.query(LeadScreening)
            .filter(LeadScreening.review_status == "OVERRIDDEN")
            .order_by(LeadScreening.id.desc()).limit(limit).all())
    grouped = (db.query(LeadScreening.ai_conclusion, LeadScreening.conclusion,
                        func.count(LeadScreening.id))
               .filter(LeadScreening.review_status == "OVERRIDDEN")
               .group_by(LeadScreening.ai_conclusion, LeadScreening.conclusion).all())
    by_transition = {f"{ai or '未知'} → {human}": count for ai, human, count in grouped}
    missing_counter: Dict[str, int] = {}
    for row in rows:
        for item in row.missing_fields or []:
            label = item.get("label") or item.get("key")
            missing_counter[label] = missing_counter.get(label, 0) + 1

    return ok({
        "total": len(rows),
        "by_transition": by_transition,
        "frequent_missing_fields": dict(
            sorted(missing_counter.items(), key=lambda kv: kv[1], reverse=True)),
        "items": [{
            "id": row.id, "lead_id": row.lead_id, "source_type": row.source_type,
            "source_name": row.source_name,
            "ai_conclusion": row.ai_conclusion, "conclusion": row.conclusion,
            "confidence": float(row.confidence or 0),
            "remark": row.review_remark, "reviewed_by": row.reviewed_by,
            "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
            "missing_fields": row.missing_fields or [],
        } for row in rows],
    })


@router.post("/screening/analyze", summary="提交材料发起研判（Dify 工作流）")
async def analyze(body: ScreeningRequest, request: Request,
                  principal: Principal = Depends(staff_only),
                  db: Session = Depends(get_db)) -> dict:
    row = await _screen_one(db, principal, lead_id=body.lead_id,
                            source_type=body.source_type, text=body.text,
                            raw_file_url=body.raw_file_url, source_name=body.source_name)
    audit.record(db, action="screening.analyze", actor_id=principal.subject,
                 actor_role=principal.role, resource="lead_screening", resource_id=row.id,
                 detail={"conclusion": row.conclusion, "source_type": row.source_type,
                         "missing_fields": [x["label"] for x in (row.missing_fields or [])]},
                 ip=client_ip(request))
    db.commit()
    db.refresh(row)
    return ok(_screening_brief(row))


# --------------------------------------------------------------------------- #
# 《用户画像研判规则》的导入与版本化（REQ-M1-03）
#
# ⚠️ 路由声明顺序：`GET /screening/rules` 与 `GET /screening/{screening_id}`
# 段数相同（都是 2 段），必须排在后者**之前**，否则会被吃掉当成 screening_id=「rules」。
# --------------------------------------------------------------------------- #
def _rule_brief(row: ScreeningRule) -> dict:
    return {
        "id": row.id, "product_key": row.product_key, "product_name": row.product_name,
        "version": row.version, "logic": row.logic,
        "threshold": float(row.threshold or 0.75),
        "rule_count": len(row.rules or []),
        "required_fields": list(row.required_fields or []),
        "status": row.status, "note": row.note, "source_name": row.source_name,
        "imported_by": row.imported_by,
        "imported_at": row.imported_at.isoformat(timespec="seconds") if row.imported_at else None,
        "rules": row.rules or [],
    }


@router.get("/screening/rules", summary="《用户画像研判规则》清单（含当前生效版本）")
def list_screening_rules(status: Optional[str] = Query(None, description="DRAFT / ACTIVE / ARCHIVED"),
                         principal: Principal = Depends(staff_only),
                         db: Session = Depends(get_db)) -> dict:
    """REQ-M1-03：规则可导入、可版本化，且能看出「现在用的是哪一版」。

    `active` 是**判定真正依据的那一份**；`items` 是全部历史版本
    （ARCHIVED 的留着做「结论当时依据的是哪一版」的追溯）。
    """
    active = rules.rule_snapshot(db)
    items = [_rule_brief(r) for r in rules.load_rules(db, status=status)]
    by_status: Dict[str, int] = {}
    for row in rules.load_rules(db):
        by_status[row.status] = by_status.get(row.status, 0) + 1
    return ok({"total": len(items), "active": active, "by_status": by_status,
               "items": items})


@router.get("/screening/rules/active", summary="当前生效的研判规则")
def active_screening_rules(principal: Principal = Depends(staff_only),
                           db: Session = Depends(get_db)) -> dict:
    return ok(rules.rule_snapshot(db))


@router.get("/screening/rules/template", summary="规则文档模板 + 可用字段与操作符")
def screening_rule_template(principal: Principal = Depends(staff_only)) -> dict:
    """给甲方/运维一份「照着填就能导入」的骨架。

    包含：示例规则文档、9 个规范字段名（含中文标签）、9 个支持的操作符及其语义。
    字段名写错是导入失败最常见的原因，所以把可选值直接摊在这里，
    而不是让人去翻代码。
    """
    return ok({
        "document": rules.default_rule_document(),
        "fields": [{"key": k, "label": material.FIELD_LABELS[k]}
                   for k in material.KEY_FIELDS],
        "ops": [
            {"op": "in", "desc": "值域命中（字符串按包含匹配，可用 | 表示或，如 雅思|IELTS）"},
            {"op": "not_in", "desc": "值域不命中"},
            {"op": "eq", "desc": "等于（同样是包含语义，适合枚举字段）"},
            {"op": "ne", "desc": "不等于"},
            {"op": "gte", "desc": "数值 ≥ 期望值（GPA 写 3.5/4.0 时取分子）"},
            {"op": "lte", "desc": "数值 ≤ 期望值"},
            {"op": "between", "desc": "数值落在 [min, max] 闭区间，value 传数组"},
            {"op": "matches", "desc": "正则命中"},
            {"op": "exists", "desc": "该字段已抽取到（不需要 value）"},
        ],
        "conclusion_values": [rules.CONFORM, rules.NON_CONFORM, rules.INSUFFICIENT],
        "sample_source": rules.SAMPLE_SOURCE,
        "note": ("模板里附的是**示例规则**，不是甲方口径。"
                 "甲方《用户画像研判规则》到位后按同一结构转 JSON 导入即可，"
                 "导入后旧版本自动归档，研判结论会带上新版本号。"),
    })


@router.post("/screening/rules", summary="导入《用户画像研判规则》（可指定版本与是否立即生效）")
def import_screening_rules(body: RuleImportRequest, request: Request,
                           principal: Principal = Depends(manager_only),
                           db: Session = Depends(get_db)) -> dict:
    """导入一版规则。

    这是「规则调整无需发版」的落地点：甲方改规则 → 转成 JSON 导入 → 立即生效，
    不需要改代码、不需要重新部署。

    `activate=true`（默认）时同产品的旧版本自动转 `ARCHIVED`，
    研判结论里会带上新的版本号，于是「某条结论依据的是哪一版规则」永远查得到。
    """
    try:
        rows = rules.import_rules(
            db, body.document(), activate=body.activate,
            imported_by=principal.subject,
            source_name=body.source_name or "甲方导入",
        )
    except rules.RuleDocError as exc:
        raise AppError(f"规则文档不合法：{exc}") from exc

    audit.record(db, action="screening_rule.import", actor_id=principal.subject,
                 actor_role=principal.role, resource="screening_rule",
                 resource_id=rows[0].id if rows else None,
                 detail={"version": body.version, "products": [r.product_key for r in rows],
                         "activate": body.activate, "source_name": body.source_name},
                 ip=client_ip(request))
    db.commit()
    return ok({"imported": len(rows), "activated": body.activate,
               "items": [_rule_brief(r) for r in rows],
               "active": rules.rule_snapshot(db)})


@router.post("/screening/rules/{rule_id}/activate", summary="切换为生效版本（旧版本自动归档）")
def activate_screening_rule(rule_id: int, request: Request,
                            principal: Principal = Depends(manager_only),
                            db: Session = Depends(get_db)) -> dict:
    row = db.get(ScreeningRule, rule_id)
    if row is None:
        raise NotFound(f"规则 {rule_id} 不存在")
    before = row.status
    rules.activate_rule(db, row)
    audit.record(db, action="screening_rule.activate", actor_id=principal.subject,
                 actor_role=principal.role, resource="screening_rule", resource_id=row.id,
                 detail={"product_key": row.product_key, "version": row.version,
                         "before": before, "after": row.status},
                 ip=client_ip(request))
    db.commit()
    return ok({"id": row.id, "product_key": row.product_key, "version": row.version,
               "before": before, "status": row.status,
               "active": rules.rule_snapshot(db)})


@router.post("/screening/rules/{rule_id}/archive", summary="归档一版规则（不再参与判定）")
def archive_screening_rule(rule_id: int, request: Request,
                           principal: Principal = Depends(manager_only),
                           db: Session = Depends(get_db)) -> dict:
    row = db.get(ScreeningRule, rule_id)
    if row is None:
        raise NotFound(f"规则 {rule_id} 不存在")
    rules.deactivate_rule(db, row)
    audit.record(db, action="screening_rule.archive", actor_id=principal.subject,
                 actor_role=principal.role, resource="screening_rule", resource_id=row.id,
                 detail={"product_key": row.product_key, "version": row.version},
                 ip=client_ip(request))
    db.commit()
    return ok({"id": row.id, "status": row.status, "active": rules.rule_snapshot(db)})


@router.patch("/screening/{screening_id}/review", summary="人工复核（确认 / 推翻并回写）")
def review_screening(screening_id: int, body: ScreeningReviewRequest, request: Request,
                     principal: Principal = Depends(staff_only),
                     db: Session = Depends(get_db)) -> dict:
    """REQ-M1-06 人工复核。

    - `CONFIRM`：认下 AI 结论，`review_status → CONFIRMED`；
    - `OVERRIDE`：推翻，必须给出修正结论，`conclusion` 被覆写、
      `ai_conclusion` 保留原值，`review_status → OVERRIDDEN`。
    两种动作都写审计（before/after），保证「谁在什么时候改了什么」可回放。
    """
    row = db.get(LeadScreening, screening_id)
    if row is None:
        raise NotFound(f"研判结果 {screening_id} 不存在")

    before = {"conclusion": row.conclusion, "review_status": row.review_status}

    if body.action == "CONFIRM":
        row.review_status = "CONFIRMED"
    else:
        if not body.conclusion:
            raise AppError("action=OVERRIDE 时必须给出修正后的 conclusion")
        if body.conclusion == row.conclusion:
            raise Conflict(
                f"修正结论与当前结论同为「{body.conclusion}」，如需确认请用 action=CONFIRM")
        row.conclusion = body.conclusion
        row.review_status = "OVERRIDDEN"

    if body.hit_products is not None:
        row.hit_products = body.hit_products
    if body.extracted_fields is not None:
        row.extracted_fields = material.merge_fields(
            material.normalize_fields(body.extracted_fields), row.extracted_fields)
        row.missing_fields = material.missing_fields(row.extracted_fields)
    if body.remark is not None:
        row.review_remark = body.remark

    row.reviewed_by = principal.ref_id
    row.reviewed_at = datetime.now()
    db.flush()

    audit.record(db, action="screening.review", actor_id=principal.subject,
                 actor_role=principal.role, resource="lead_screening",
                 resource_id=screening_id,
                 detail={"action": body.action, "before": before,
                         "after": {"conclusion": row.conclusion,
                                   "review_status": row.review_status},
                         "ai_conclusion": row.ai_conclusion, "remark": body.remark},
                 ip=client_ip(request))
    db.commit()
    db.refresh(row)
    return ok({**_screening_detail(row), "before": before})


@router.get("/screening/{screening_id}", summary="查询研判结果")
def get_screening(screening_id: int, principal: Principal = Depends(staff_only),
                  db: Session = Depends(get_db)) -> dict:
    row = db.get(LeadScreening, screening_id)
    if row is None:
        raise NotFound(f"研判结果 {screening_id} 不存在")
    return ok(_screening_detail(row))


@router.get("/screening", summary="研判结果列表")
def list_screenings(conclusion: Optional[str] = None,
                    review_status: Optional[str] = None,
                    lead_id: Optional[int] = None,
                    batch_id: Optional[str] = None,
                    limit: int = Query(20, ge=1, le=100),
                    principal: Principal = Depends(staff_only),
                    db: Session = Depends(get_db)) -> dict:
    query = db.query(LeadScreening)
    if conclusion:
        query = query.filter(LeadScreening.conclusion == conclusion)
    if review_status:
        query = query.filter(LeadScreening.review_status == review_status)
    if lead_id:
        query = query.filter(LeadScreening.lead_id == lead_id)
    if batch_id:
        query = query.filter(LeadScreening.batch_id == batch_id)

    rows = query.order_by(LeadScreening.id.desc()).limit(limit).all()

    def _group(column) -> Dict[str, int]:
        return dict(db.query(column, func.count(LeadScreening.id)).group_by(column).all())

    by_review_status = _group(LeadScreening.review_status)
    return ok({
        "total": len(rows),
        "by_conclusion": _group(LeadScreening.conclusion),
        "by_review_status": by_review_status,
        "pending_review": by_review_status.get("PENDING", 0),
        "items": [_screening_brief(row) for row in rows],
    })


_ = get_principal  # 保留导出，便于外部按需覆写依赖
