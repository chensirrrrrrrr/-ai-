"""组织域：组织架构查询（M3-06）与新人入职指引（M3-07）。

- 组织架构：`employee` 表已有 `department / title / manager_id / biz_role`，
  这里只做查询与树形组装，**不改模型**。
- 新人入职指引：内容在 `services/onboarding.py`（版本化知识资产，
  同步登记进 `knowledge_doc` 的 `NEO` 分类），本模块只做查询组装。

两者都是**只读**接口，因此不写审计日志（审计只覆盖写操作）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ...core import NotFound, ok
from ...db import get_db
from ...models import Employee
from ...services import onboarding
from ..deps import Principal, require_roles

router = APIRouter(tags=["org"])

staff_only = require_roles("employee", "manager", "admin")

# 组织架构/入职指引属内部信息，仅员工及以上可见（学员与访客不可见）
EmployeeNotFound = "员工"


def _brief(emp: Employee) -> Dict[str, Any]:
    return {"id": emp.id, "emp_no": emp.emp_no, "name": emp.name,
            "department": emp.department, "title": emp.title,
            "biz_role": emp.biz_role, "manager_id": emp.manager_id}


def _contact_card(emp: Employee) -> Dict[str, Any]:
    """花名册/联系人用的详版（含联系方式）。"""
    card = _brief(emp)
    card.update({"phone": emp.phone, "email": emp.email, "status": emp.status})
    return card


# --------------------------------------------------------------------------- #
# M3-06 组织架构查询
# --------------------------------------------------------------------------- #
@router.get("/org/tree", summary="组织架构树（按汇报线 manager_id 组装）")
def org_tree(department: Optional[str] = None,
             principal: Principal = Depends(staff_only),
             db: Session = Depends(get_db)) -> dict:
    query = db.query(Employee).filter(Employee.status == "ACTIVE")
    if department:
        query = query.filter(Employee.department == department)
    rows = query.order_by(Employee.department.asc(), Employee.id.asc()).all()

    by_id = {e.id: e for e in rows}
    children: Dict[int, List[int]] = {e.id: [] for e in rows}
    roots: List[int] = []
    for e in rows:
        if e.manager_id and e.manager_id != e.id and e.manager_id in by_id:
            children[e.manager_id].append(e.id)
        else:
            roots.append(e.id)

    def build(node_id: int, path: set) -> Optional[Dict[str, Any]]:
        """递归组装；`path` 用于在数据异常成环时断开，避免无限递归。"""
        if node_id in path:
            return None
        emp = by_id[node_id]
        node = _brief(emp)
        node["children"] = [c for c in (build(c, path | {node_id})
                                        for c in children[node_id]) if c]
        return node

    tree = [n for n in (build(r, set()) for r in roots) if n]

    # 数据里若存在孤立环（互相为上级），上面不会出现在 roots 中 —— 兜底补上
    reachable: set = set()

    def walk(node_id: int) -> None:
        if node_id in reachable:
            return
        reachable.add(node_id)
        for c in children[node_id]:
            walk(c)

    for r in roots:
        walk(r)
    orphan_roots = [e.id for e in rows if e.id not in reachable]
    tree += [n for n in (build(o, set()) for o in orphan_roots) if n]

    departments = {}
    for e in rows:
        departments[e.department or "未分组"] = departments.get(e.department or "未分组", 0) + 1

    return ok({
        "total": len(rows),
        "root_count": len(tree),
        "filtered_by_department": department,
        "departments": [{"department": k, "headcount": v}
                        for k, v in sorted(departments.items(), key=lambda kv: -kv[1])],
        "tree": tree,
    })


@router.get("/org/departments", summary="部门概览（人数 + 负责人 + 成员）")
def org_departments(principal: Principal = Depends(staff_only),
                    db: Session = Depends(get_db)) -> dict:
    rows = (db.query(Employee).filter(Employee.status == "ACTIVE")
            .order_by(Employee.department.asc(), Employee.id.asc()).all())
    buckets: Dict[str, List[Employee]] = {}
    for e in rows:
        buckets.setdefault(e.department or "未分组", []).append(e)

    items = []
    for name, members in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        head = next((m for m in members if m.biz_role == "manager"), None)
        items.append({
            "department": name,
            "headcount": len(members),
            "head": _brief(head) if head else None,
            "members": [_brief(m) for m in members],
        })
    return ok({"total": len(items), "items": items})


@router.get("/org/employees", summary="员工花名册（支持部门 / 关键字 / 岗位筛选）")
def list_employees(department: Optional[str] = None,
                   keyword: Optional[str] = None,
                   biz_role: Optional[str] = None,
                   status: str = "ACTIVE",
                   page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100),
                   principal: Principal = Depends(staff_only),
                   db: Session = Depends(get_db)) -> dict:
    query = db.query(Employee)
    if status:
        query = query.filter(Employee.status == status)
    if department:
        query = query.filter(Employee.department == department)
    if biz_role:
        query = query.filter(Employee.biz_role == biz_role)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(Employee.name.like(like)
                             | Employee.emp_no.like(like)
                             | Employee.title.like(like))

    total = query.count()
    rows = (query.order_by(Employee.department.asc(), Employee.id.asc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return ok({"total": total, "page": page, "page_size": page_size,
               "items": [_contact_card(e) for e in rows]})


@router.get("/org/employees/{employee_id}", summary="员工详情（含上级 / 下属 / 同部门）")
def get_employee(employee_id: int,
                 principal: Principal = Depends(staff_only),
                 db: Session = Depends(get_db)) -> dict:
    emp = db.get(Employee, employee_id)
    if emp is None:
        raise NotFound(f"{EmployeeNotFound} {employee_id} 不存在")

    manager = db.get(Employee, emp.manager_id) if emp.manager_id else None
    subordinates = (db.query(Employee)
                    .filter(Employee.manager_id == employee_id,
                            Employee.status == "ACTIVE")
                    .order_by(Employee.id.asc()).all())
    same_dept = (db.query(Employee)
                 .filter(Employee.department == emp.department,
                         Employee.id != employee_id,
                         Employee.status == "ACTIVE")
                 .order_by(Employee.id.asc()).all())

    out = _contact_card(emp)
    out.update({
        "manager": _brief(manager) if manager else None,
        "subordinates": [_brief(s) for s in subordinates],
        "colleagues": [_brief(c) for c in same_dept],
    })
    return ok(out)


# --------------------------------------------------------------------------- #
# M3-07 新人入职指引
# --------------------------------------------------------------------------- #
@router.get("/org/onboarding/guide", summary="新人入职指引（全文：阶段步骤 + FAQ + 元信息）")
def onboarding_guide(principal: Principal = Depends(staff_only)) -> dict:
    return ok(onboarding.guide())


@router.get("/org/onboarding/checklist", summary="入职待办清单（可按阶段过滤）")
def onboarding_checklist(stage: Optional[str] = Query(None, description="D0 / D1 / W1 / M1 / M3"),
                         principal: Principal = Depends(staff_only)) -> dict:
    return ok(onboarding.checklist(stage))


@router.get("/org/onboarding/faq", summary="新人常见问题（关键词检索）")
def onboarding_faq(keyword: Optional[str] = None,
                   limit: int = Query(20, ge=1, le=50),
                   principal: Principal = Depends(staff_only)) -> dict:
    return ok(onboarding.search_faq(keyword, limit))


@router.get("/org/onboarding/contacts",
            summary="入职关键联系人（默认按当前登录人解析，可指定 employee_id）")
def onboarding_contacts(employee_id: Optional[int] = None,
                        principal: Principal = Depends(staff_only),
                        db: Session = Depends(get_db)) -> dict:
    target = employee_id if employee_id is not None else principal.ref_id
    if employee_id is not None and db.get(Employee, employee_id) is None:
        raise NotFound(f"{EmployeeNotFound} {employee_id} 不存在")
    return ok(onboarding.resolve_contacts(db, target))
