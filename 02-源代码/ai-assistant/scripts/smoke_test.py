# -*- coding: utf-8 -*-
"""端到端冒烟测试：对**真实运行中的服务**发 HTTP 请求，覆盖全部核心链路。

用法：
    # 先起服务（推荐用一键启动器，端口以它为准）
    python scripts/launcher.py --headless --no-browser
    # 再跑冒烟；--base 省略时自动读 .run/pids.json，兜底 launcher 的默认端口
    python scripts/smoke_test.py [--base http://127.0.0.1:8010]

产出：reports/smoke_report.md（含每条用例的耗时与结果）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx                                                    # noqa: E402

from app.core import sign_tool_payload                          # noqa: E402
from tests.material_fixtures import make_pdf, make_xlsx          # noqa: E402

# 端口与运行态文件的**唯一权威源**是 scripts/launcher.py。
# 本脚本曾把 --base 默认值硬编码成 8000，而 launcher 默认起在 8010 ——
# 服务由 launcher 起时，手工跑本脚本不加 --base 会全部打空、报一堆莫名 FAIL。
try:
    from scripts.launcher import DEFAULT_BACKEND_PORT, HOST, PID_FILE   # noqa: E402
except Exception:                                               # noqa: BLE001
    # 只在 launcher 缺失/被改名时兜底，正常路径不会走到这里
    DEFAULT_BACKEND_PORT, HOST = 8010, "127.0.0.1"
    PID_FILE = Path(__file__).resolve().parent.parent / ".run" / "pids.json"

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

RESULTS: list[tuple[str, str, str, float, str]] = []


def default_base() -> str:
    """推断后端地址，供 --base 的默认值使用。

    优先级：launcher 实际启动的端口（`.run/pids.json` 的 `backend_port`）
    → launcher 的默认端口。绝不自己硬编码，免得与 launcher 漂移。
    """
    try:
        port = int(json.loads(PID_FILE.read_text(encoding="utf-8"))["backend_port"])
        return f"http://{HOST}:{port}"
    except Exception:                                           # noqa: BLE001
        return f"http://{HOST}:{DEFAULT_BACKEND_PORT}"


def _walk_tree(nodes: list[dict]):
    """深度遍历组织架构树，逐个吐出节点（用于校验「树里的节点数 == 在职员工数」）。"""
    for node in nodes:
        yield node
        yield from _walk_tree(node.get("children") or [])


def record(name: str, ok: bool, detail: str, cost_ms: float) -> None:
    RESULTS.append((name, "PASS" if ok else "FAIL", detail, cost_ms, ""))
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {name:<44} {cost_ms:7.1f}ms  {detail}")


def check(client: httpx.Client, name: str, method: str, path: str, *,
          expect: int = 200, expect_code: int = 0, **kwargs):
    url = path
    started = time.perf_counter()
    try:
        resp = client.request(method, url, **kwargs)
        cost = (time.perf_counter() - started) * 1000
        payload = None
        try:
            payload = resp.json()
        except Exception:                                       # noqa: BLE001
            payload = {"_raw": resp.text[:120]}
        code = payload.get("code") if isinstance(payload, dict) else None
        ok = resp.status_code == expect and (expect_code is None or code == expect_code)
        detail = f"HTTP {resp.status_code} code={code}"
        if not ok:
            detail += f" body={json.dumps(payload, ensure_ascii=False)[:160]}"
        record(name, ok, detail, cost)
        return payload if ok else None
    except Exception as exc:                                    # noqa: BLE001
        cost = (time.perf_counter() - started) * 1000
        record(name, False, f"EXCEPTION {exc}", cost)
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=default_base(),
                        help="后端地址；默认读 .run/pids.json，兜底 launcher 默认端口")
    args = parser.parse_args()

    started_at = datetime.now()
    print(f"== 冒烟测试目标: {args.base}  开始 {started_at:%Y-%m-%d %H:%M:%S}\n")

    with httpx.Client(base_url=args.base, timeout=30.0) as client:
        # ---------------- 存活与认证 ----------------
        check(client, "健康检查 /health", "GET", "/api/v1/health")
        check(client, "就绪探针 /ready", "GET", "/api/v1/ready")
        # OpenAPI 规范文档不是统一响应体，没有 code 字段，只校验 HTTP 200
        check(client, "OpenAPI 文档", "GET", "/openapi.json", expect_code=None)

        tok = check(client, "登录（顾问）", "POST", "/api/v1/auth/token",
                    json={"username": "advisor", "password": "advisor123"})
        staff_h = {"Authorization": f"Bearer {tok['data']['access_token']}"} if tok else {}

        mgr = check(client, "登录（管理层）", "POST", "/api/v1/auth/token",
                    json={"username": "manager", "password": "manager123"})
        mgr_h = {"Authorization": f"Bearer {mgr['data']['access_token']}"} if mgr else {}

        stu = check(client, "登录（学生）", "POST", "/api/v1/auth/token",
                    json={"username": "student", "password": "student123"})
        stu_h = {"Authorization": f"Bearer {stu['data']['access_token']}"} if stu else {}

        vis = check(client, "访客 Token", "POST", "/api/v1/auth/visitor")
        vis_h = {"Authorization": f"Bearer {vis['data']['access_token']}"} if vis else {}

        check(client, "错误密码被拒（预期 401）", "POST", "/api/v1/auth/token",
              expect=401, expect_code=40100,
              json={"username": "advisor", "password": "wrong-password"})
        check(client, "无 Token 被拒（预期 401）", "GET", "/api/v1/auth/me",
              expect=401, expect_code=40100)

        # ---------------- 对话链路 ----------------
        chat = check(client, "客服问答（知识类，带引用）", "POST", "/api/v1/chat/message",
                     headers=vis_h,
                     json={"session_id": "smoke-1", "message": "你们机构在成都的校区在哪？"})
        if chat:
            d = chat["data"]
            record("  └ 命中客服 Agent + 有引用",
                   d["agent"] == "customer_service" and bool(d["references"]),
                   f"agent={d['agent']} intent={d['intent']} refs={len(d['references'])}", 0.0)

        isolated = check(client, "权限隔离（访客说请假）", "POST", "/api/v1/chat/message",
                         headers=vis_h,
                         json={"session_id": "smoke-2", "message": "我要请假 2 天"})
        if isolated:
            d = isolated["data"]
            record("  └ 被降级到客服 Agent",
                   d["agent"] == "customer_service",
                   f"agent={d['agent']} confidence={d['confidence']}", 0.0)

        check(client, "学生请假意图命中", "POST", "/api/v1/chat/message", headers=stu_h,
              json={"session_id": "smoke-3", "message": "我要请假 2 天"})

        # 流式
        started = time.perf_counter()
        with client.stream("POST", "/api/v1/chat/stream", headers=vis_h,
                           json={"session_id": "smoke-4",
                                 "message": "你们机构在成都的校区在哪"}) as resp:
            text = "".join(resp.iter_text())
        cost = (time.perf_counter() - started) * 1000
        record("流式对话 SSE", resp.status_code == 200 and "[DONE]" in text and "route" in text,
               f"HTTP {resp.status_code} 收到 {text.count('data:')} 个事件", cost)

        # 语音录入
        audio = b"RIFF" + b"\x11" * 4096
        asr = check(client, "语音录入（请假）", "POST", "/api/v1/chat/asr", headers=stu_h,
                    files={"file": ("leave.wav", audio, "audio/wav")},
                    data={"purpose": "leave_apply"})
        if asr:
            slots = asr["data"]["structured"]
            record("  └ 槽位抽取正确",
                   slots.get("days") == 2 and slots.get("start_date") == "2026-09-15",
                   f"slots={json.dumps(slots, ensure_ascii=False)}", 0.0)

        check(client, "语音录入（日报）", "POST", "/api/v1/chat/asr", headers=staff_h,
              files={"file": ("daily.m4a", b"ID3" + b"\x22" * 4096, "audio/m4a")},
              data={"purpose": "daily_report"})

        # ---------------- 客户域 ----------------
        phone = "135" + uuid.uuid4().hex[:8]
        lead = check(client, "新增意向客户", "POST", "/api/v1/leads", headers=staff_h,
                     json={"name": "冒烟测试客户", "phone": phone,
                           "intention_country": "英国", "source": "smoke"})
        check(client, "重复手机号被拒（预期 409）", "POST", "/api/v1/leads", headers=staff_h,
              expect=409, expect_code=40900,
              json={"name": "冒烟测试客户", "phone": phone})
        check(client, "客户列表分页查询", "GET", "/api/v1/leads",
              headers=staff_h, params={"page": 1, "page_size": 5})

        if lead:
            lead_id = lead["data"]["id"]
            key = "smoke-" + uuid.uuid4().hex
            check(client, "新增跟进记录（首次）", "POST",
                  f"/api/v1/leads/{lead_id}/followups", headers=staff_h,
                  json={"content": "电话沟通，客户关注英国 G5 录取要求", "idempotency_key": key})
            dup = check(client, "跟进记录幂等（重复提交）", "POST",
                        f"/api/v1/leads/{lead_id}/followups", headers=staff_h,
                        json={"content": "电话沟通，客户关注英国 G5 录取要求",
                              "idempotency_key": key})
            if dup:
                record("  └ 命中幂等返回同一条", dup["data"]["duplicated"] is True,
                       f"id={dup['data']['id']} duplicated=True", 0.0)
            check(client, "更新客户状态", "PATCH", f"/api/v1/leads/{lead_id}/status",
                  headers=staff_h, json={"status": "FOLLOWING"})

        scr = check(client, "客户研判（Dify 工作流）", "POST", "/api/v1/screening/analyze",
                    headers=staff_h,
                    json={"source_type": "TEXT",
                          "text": "学生本科 211，GPA 3.4/4.0，雅思 7.0，目标英国硕士"})
        if scr:
            d = scr["data"]
            record("  └ 研判结论与置信度",
                   d["conclusion"] == "符合" and d["confidence"] > 0.5,
                   f"结论={d['conclusion']} 置信度={d['confidence']}", 0.0)

        # ---------------- M1 材料上传解析 / 人工复核 / 批量研判 ----------------
        xlsx_bytes = make_xlsx([
            ["姓名", "年龄", "学历", "毕业院校", "专业", "GPA", "雅思", "意向国家", "意向阶段"],
            ["钱九", "23", "本科", "西南财经大学", "金融学", "3.4/4.0", "7.0", "英国", "待签约"],
        ])
        upload = check(client, "材料上传解析（xlsx → 字段抽取）", "POST",
                       "/api/v1/screening/upload", headers=staff_h,
                       files={"file": ("客户登记表.xlsx", xlsx_bytes, XLSX_MIME)})
        if upload:
            d = upload["data"]
            record("  └ 关键字段抽齐且无缺失",
                   d["source_type"] == "EXCEL" and d["fields"].get("name") == "钱九"
                   and d["fields"].get("school") == "西南财经大学"
                   and not d["missing_fields"],
                   f"解析器={d['parser']} 抽到 {len(d['field_rows'])} 项 / 缺失 {len(d['missing_fields'])} 项", 0.0)
            rel_path = d["raw_file_url"].split("/api/v1/screening/files/")[1]
            started = time.perf_counter()
            got = client.get(f"/api/v1/screening/files/{rel_path}", headers=staff_h)
            record("  └ 材料原件可下载且字节一致",
                   got.status_code == 200 and got.content == xlsx_bytes,
                   f"HTTP {got.status_code} {len(got.content)}B",
                   (time.perf_counter() - started) * 1000)

        pdf_bytes = make_pdf(["Name: Zhao Liu", "IELTS 7.0  GPA 3.4/4.0", "Target Country: UK"])
        check(client, "材料上传解析（PDF 简历）", "POST", "/api/v1/screening/upload",
              headers=staff_h, files={"file": ("resume.pdf", pdf_bytes, "application/pdf")})

        check(client, "材料上传对学生不可见（预期 403）", "POST", "/api/v1/screening/upload",
              expect=403, expect_code=40300, headers=stu_h,
              files={"file": ("a.txt", b"x", "text/plain")})
        check(client, "不支持的格式被拒（预期 400）", "POST", "/api/v1/screening/upload",
              expect=400, expect_code=40000, headers=staff_h,
              files={"file": ("scan.png", b"\x89PNG\r\n\x1a\n", "image/png")})

        # 只给原件链接、不给正文也要能研判（第三方系统常见调法）
        reparsed = None
        if upload:
            reparsed = check(client, "研判（仅给原件链接，服务端重解析）", "POST",
                             "/api/v1/screening/analyze", headers=staff_h,
                             json={"source_type": "EXCEL",
                                   "raw_file_url": upload["data"]["raw_file_url"]})
            if reparsed:
                d = reparsed["data"]
                record("  └ 材料名回到原始文件名",
                       d["source_name"] == "客户登记表.xlsx",
                       f"source_name={d['source_name']} 结论={d['conclusion']}", 0.0)

        if reparsed:
            sid = reparsed["data"]["id"]
            ai_conclusion = reparsed["data"]["ai_conclusion"]
            confirmed = check(client, "人工复核：确认 AI 结论", "PATCH",
                              f"/api/v1/screening/{sid}/review", headers=staff_h,
                              json={"action": "CONFIRM", "remark": "冒烟：与材料一致"})
            if confirmed:
                d = confirmed["data"]
                record("  └ 状态落为 CONFIRMED 且结论未变",
                       d["review_status"] == "CONFIRMED" and d["conclusion"] == ai_conclusion,
                       f"review_status={d['review_status']} conclusion={d['conclusion']}", 0.0)

            flipped = "不符合" if ai_conclusion != "不符合" else "符合"
            second = check(client, "新建一条用于推翻的研判", "POST", "/api/v1/screening/analyze",
                           headers=staff_h,
                           json={"source_type": "TEXT",
                                 "text": "硕士，均分 68，目标加拿大"})
            if second:
                sid2 = second["data"]["id"]
                ai2 = second["data"]["ai_conclusion"]
                overridden = check(client, "人工复核：推翻并回写", "PATCH",
                                   f"/api/v1/screening/{sid2}/review", headers=staff_h,
                                   json={"action": "OVERRIDE", "conclusion": flipped,
                                         "remark": "冒烟：AI 判错，人工修正"})
                if overridden:
                    d = overridden["data"]
                    record("  └ 结论被覆写且 AI 原结论留痕",
                           d["conclusion"] == flipped and d["ai_conclusion"] == ai2
                           and d["review_status"] == "OVERRIDDEN",
                           f"{d['ai_conclusion']} → {d['conclusion']}", 0.0)

        corrections = check(client, "人工修正清单（规则优化素材）", "GET",
                            "/api/v1/screening/corrections", headers=staff_h)
        if corrections:
            d = corrections["data"]
            record("  └ 列出被推翻记录并按结论迁移聚合",
                   d["total"] >= 1 and bool(d["by_transition"]),
                   f"{d['total']} 条 / 迁移 {list(d['by_transition'])[:2]}", 0.0)

        batch = check(client, "批量研判（3 份材料 → 结果清单）", "POST",
                      "/api/v1/screening/batch", headers=staff_h,
                      json={"items": [
                          {"source_type": "TEXT", "text": "本科，GPA 3.5/4.0，雅思 7.0，目标英国",
                           "source_name": "材料A"},
                          {"source_type": "TEXT", "text": "想出国读书，还没想好去哪",
                           "source_name": "材料B"},
                          {"source_type": "TEXT", "text": "硕士，均分 85，目标澳洲",
                           "source_name": "材料C"},
                      ]})
        if batch:
            d = batch["data"]
            record("  └ 批次号 + 成败计数 + 结果清单",
                   d["succeeded"] == 3 and d["failed"] == 0 and len(d["items"]) == 3,
                   f"batch={d['batch_id']} 结论分布={d['by_conclusion']}", 0.0)
            listing = check(client, "按批号回捞研判清单", "GET", "/api/v1/screening",
                            headers=staff_h, params={"batch_id": d["batch_id"]})
            if listing:
                record("  └ 批内 3 条全部可查", listing["data"]["total"] == 3,
                       f"total={listing['data']['total']}", 0.0)

        partial = check(client, "批量研判：单条失败被隔离", "POST", "/api/v1/screening/batch",
                        headers=staff_h,
                        json={"items": [
                            {"source_type": "TEXT", "text": "本科，GPA 3.5/4.0"},
                            {"source_type": "TEXT"},
                            {"source_type": "TEXT", "text": "硕士，均分 85，目标澳洲"},
                        ]})
        if partial:
            d = partial["data"]
            record("  └ 一条失败不带走其余两条",
                   d["succeeded"] == 2 and d["failed"] == 1
                   and all(item["id"] for item in d["items"] if item["ok"]),
                   f"成功 {d['succeeded']} / 失败 {d['failed']}", 0.0)

        check(client, "批量研判：超上限被拒（预期 400）", "POST", "/api/v1/screening/batch",
              expect=400, expect_code=40000, headers=staff_h,
              json={"items": [{"source_type": "TEXT", "text": f"材料{i}"} for i in range(21)]})
        check(client, "研判列表按复核状态过滤", "GET", "/api/v1/screening", headers=staff_h,
              params={"review_status": "OVERRIDDEN", "limit": 5})

        # ---------------- 学员域（审批闭环） ----------------
        check(client, "学生列表", "GET", "/api/v1/students", headers=staff_h)
        check(client, "申请进度时间轴", "GET", "/api/v1/students/1/progress", headers=stu_h)
        check(client, "越权读他人档案（预期 403）", "GET", "/api/v1/students/2",
              expect=403, expect_code=40300, headers=stu_h)

        leave = check(client, "提交请假申请", "POST", "/api/v1/leave/apply", headers=stu_h,
                      json={"student_id": 1, "request_type": "LEAVE",
                            "start_date": "2026-10-08", "days": 2, "reason": "看病",
                            "idempotency_key": "smoke-leave-" + uuid.uuid4().hex})
        if leave:
            rid = leave["data"]["id"]
            approved = check(client, "审批请假（通过）", "POST",
                             f"/api/v1/leave/{rid}/approve", headers=staff_h,
                             json={"approve": True, "remark": "已核实"})
            if approved:
                record("  └ 状态流转 PENDING→APPROVED",
                       approved["data"]["status"] == "APPROVED", "status=APPROVED", 0.0)
            check(client, "重复审批被拒（预期 409）", "POST",
                  f"/api/v1/leave/{rid}/approve", expect=409, expect_code=40900,
                  headers=staff_h, json={"approve": True})

        ticket = check(client, "创建售后工单", "POST", "/api/v1/tickets", headers=stu_h,
                       json={"content": "签证材料清单回复较慢，希望加快。", "category": "签证"})
        if ticket:
            tid = ticket["data"]["id"]
            check(client, "更新工单状态", "PATCH", f"/api/v1/tickets/{tid}",
                  headers=staff_h, json={"status": "RESOLVED", "satisfaction": 5})
        check(client, "工单列表", "GET", "/api/v1/tickets", headers=staff_h,
              params={"status": "RESOLVED"})

        alerts = check(client, "心理预警（管理层可读）", "GET", "/api/v1/alerts",
                       headers=mgr_h)
        check(client, "心理预警（普通员工被拒，预期 403）", "GET", "/api/v1/alerts",
              expect=403, expect_code=40300, headers=staff_h)
        if alerts and alerts["data"]["items"]:
            aid = alerts["data"]["items"][0]["id"]
            check(client, "更新预警跟进状态", "PATCH", f"/api/v1/alerts/{aid}",
                  headers=mgr_h, json={"status": "FOLLOWING"})

        # ---------------- 运营域 ----------------
        check(client, "活动列表", "GET", "/api/v1/activities", headers=stu_h)
        act = check(client, "创建活动", "POST", "/api/v1/activities", headers=mgr_h,
                    json={"title": "冒烟测试活动", "capacity": 10,
                          "start_at": "2026-11-01T10:00:00"})
        if act:
            check(client, "活动报名", "POST",
                  f"/api/v1/activities/{act['data']['id']}/enroll", headers=stu_h,
                  json={"student_id": 1})

        rep = check(client, "生成报告（Dify 工作流）", "POST", "/api/v1/reports/generate",
                    headers=mgr_h,
                    json={"report_type": "weekly", "title": "冒烟周报",
                          "params": {"week": "2026-W37"}})
        if rep:
            rid = rep["data"]["id"]
            check(client, "报告详情", "GET", f"/api/v1/reports/{rid}", headers=mgr_h)
            started = time.perf_counter()
            r = client.get(f"/api/v1/reports/{rid}/download", headers=mgr_h)
            record("报告下载", r.status_code == 200 and "attachment" in r.headers.get(
                "content-disposition", ""), f"HTTP {r.status_code} {len(r.text)}B",
                (time.perf_counter() - started) * 1000)

        check(client, "提交员工日报（语音来源）", "POST", "/api/v1/reports/daily",
              headers=staff_h,
              json={"employee_id": 1, "content": "冒烟测试：今日跟进 3 位客户。",
                    "source": "voice", "raw_audio_url": "oss://demo/smoke.m4a"})
        check(client, "日报汇总", "GET", "/api/v1/reports/daily/summary", headers=staff_h)
        check(client, "知识文档登记", "POST", "/api/v1/kb/documents", headers=staff_h,
              json={"title": "冒烟测试文档", "category": "测试", "dify_dataset_id": "ds-smoke"})

        # ---------------- 组织架构 / 新人入职指引 ----------------
        tree = check(client, "组织架构树", "GET", "/api/v1/org/tree", headers=staff_h)
        if tree:
            d = tree["data"]
            record("  └ 汇报线成树且覆盖全部在职员工",
                   d["total"] >= 3 and d["root_count"] >= 1
                   and sum(1 for _ in _walk_tree(d["tree"])) == d["total"],
                   f"员工 {d['total']} 人 / 根节点 {d['root_count']} / 部门 {len(d['departments'])}", 0.0)
        check(client, "部门概览", "GET", "/api/v1/org/departments", headers=staff_h)
        emp = check(client, "员工花名册（关键字）", "GET", "/api/v1/org/employees",
                    headers=staff_h, params={"keyword": "王"})
        if emp:
            record("  └ 关键字命中唯一员工",
                   emp["data"]["total"] == 1 and emp["data"]["items"][0]["name"] == "王敏",
                   f"命中 {emp['data']['total']} 条", 0.0)
        check(client, "员工详情（含汇报关系）", "GET", "/api/v1/org/employees/1", headers=staff_h)
        check(client, "组织架构对学生不可见（预期 403）", "GET", "/api/v1/org/tree",
              expect=403, expect_code=40300, headers=stu_h)

        guide = check(client, "新人入职指引（全文）", "GET", "/api/v1/org/onboarding/guide",
                      headers=staff_h)
        if guide:
            d = guide["data"]
            record("  └ 五阶段步骤齐全且每项都有负责人与入口",
                   [s["key"] for s in d["stages"]] == ["D0", "D1", "W1", "M1", "M3"]
                   and all(i["owner"] and i["channel"]
                           for s in d["stages"] for i in s["items"]),
                   f"{d['version']} · {d['stats']['stage_count']} 阶段 / "
                   f"{d['stats']['item_count']} 项 / {d['stats']['faq_count']} FAQ", 0.0)
        check(client, "入职待办清单（按阶段）", "GET", "/api/v1/org/onboarding/checklist",
              headers=staff_h, params={"stage": "D1"})
        faq = check(client, "新人常见问题检索", "GET", "/api/v1/org/onboarding/faq",
                    headers=staff_h, params={"keyword": "报到要带什么材料"})
        if faq:
            record("  └ 整句提问也能召回 FAQ",
                   faq["data"]["total"] >= 1
                   and faq["data"]["items"][0]["id"] == "F-01",
                   f"命中 {faq['data']['total']} 条，首条 {faq['data']['items'][0]['q']}", 0.0)
        contacts = check(client, "入职关键联系人（按登录人解析）",
                         "GET", "/api/v1/org/onboarding/contacts", headers=staff_h)
        if contacts:
            person = {i["key"]: i for i in contacts["data"]["items"]}
            record("  └ 直属上级来自 employee.manager_id",
                   (person["manager"]["contact"] or {}).get("name") == "陈总",
                   f"上级={((person['manager']['contact'] or {}).get('name'))}", 0.0)
        check(client, "入职指引知识条目已登记（category=NEO）", "GET", "/api/v1/kb/documents",
              headers=staff_h, params={"category": "NEO"})
        onboarding = check(client, "对话：新人入职指引意图", "POST", "/api/v1/chat/message",
                           headers=staff_h,
                           json={"session_id": "smoke-neo", "message": "新人入职指引"})
        if onboarding:
            d = onboarding["data"]
            record("  └ 路由到 onboarding 且答复带版本出处",
                   d["intent"] == "onboarding" and "入职" in d["answer"]
                   and bool(d["references"]),
                   f"agent={d['agent']} intent={d['intent']} refs={len(d['references'])}", 0.0)

        # ---------------- 主动待办推送 / 心理预警触达 ----------------
        staff_cats: set = set()
        pending = check(client, "主动待办清单（顾问）", "GET", "/api/v1/todo/pending",
                        headers=staff_h)
        if pending:
            d = pending["data"]
            cats = {t["category"] for t in d["items"]}
            record("  └ 四类业务待办 + 主动询问话术 + 不含敏感类",
                   {"approval", "ticket", "followup", "screening_review"} <= cats
                   and "mental_alert" not in cats and "有没有" in d["digest"],
                   f"{d['total']} 件 / {d['category_count']} 类 / 最高 {d['highest_severity']}", 0.0)

            staff_cats = cats

        mgr_pending = check(client, "主动待办清单（管理层）", "GET", "/api/v1/todo/pending",
                            headers=mgr_h)
        if mgr_pending:
            d = mgr_pending["data"]
            mgr_cats = {t["category"] for t in d["items"]}
            # 待办是「现场算」的：预警一旦全部触达完，「待触达心理预警」这一类
            # 就该自然消失。所以不能断言「必然出现」——那等于把断言绑死在种子数据的
            # 初始状态上，同一份数据第二次跑必然红（上一轮把自己要断言的前提消耗掉了）。
            # 真正的不变量是两条：① 管理层是员工的超集；② 敏感类只出现在管理层。
            record("  └ 管理层待办 ⊇ 员工待办（角色超集）",
                   staff_cats <= mgr_cats,
                   f"员工 {len(staff_cats)} 类 / 管理层 {len(mgr_cats)} 类", 0.0)

        undelivered = check(client, "心理预警（未触达计数）", "GET", "/api/v1/alerts",
                            headers=mgr_h, params={"notified": False})
        if undelivered and mgr_pending:
            n = undelivered["data"]["undelivered"]
            mgr_cats = {t["category"] for t in mgr_pending["data"]["items"]}
            record("  └ 有未触达预警时必现「待触达心理预警」",
                   (n > 0) == ("mental_alert" in mgr_cats),
                   f"未触达 {n} 条 → 待办{'含' if 'mental_alert' in mgr_cats else '不含'}该类", 0.0)

        check(client, "待办清单对访客不可见（预期 403）", "GET", "/api/v1/todo/pending",
              expect=403, expect_code=40300, headers=vis_h)

        push1 = check(client, "手动推送一轮待办", "POST", "/api/v1/todo/push",
                      headers=staff_h, json={"categories": ["screening_review"]})
        if push1:
            d = push1["data"]
            record("  └ 推送落库（留痕）",
                   bool(d["records"]) and d["records"][0]["count"] >= 1,
                   f"pushed={d['pushed']} records={len(d['records'])}", 0.0)

        push2 = check(client, "重复推送命中频控（幂等）", "POST", "/api/v1/todo/push",
                      headers=staff_h, json={"categories": ["screening_review"]})
        if push2:
            d = push2["data"]
            record("  └ 同一时间窗不重复提醒同一个人",
                   d["pushed"] == 0 and d["records"][0]["duplicated"] is True,
                   f"pushed={d['pushed']} duplicated=True", 0.0)

        check(client, "全员推送限管理层（预期 403）", "POST", "/api/v1/todo/push",
              expect=403, expect_code=40300, headers=staff_h, json={"all_staff": True})

        pushes = check(client, "推送记录（只看自己）", "GET", "/api/v1/todo/pushes",
                       headers=staff_h)
        if pushes:
            d = pushes["data"]
            record("  └ 记录只含本人 + 未处理计数",
                   bool(d["items"]) and all(r["subject"] == "advisor" for r in d["items"]),
                   f"{d['total']} 条 / 未处理 {d['unacknowledged']} 条", 0.0)
            ack = check(client, "标记待办已处理", "PATCH",
                        f"/api/v1/todo/pushes/{d['items'][0]['id']}/ack",
                        headers=staff_h, json={"remark": "冒烟：已处理"})
            if ack:
                record("  └ 幂等标记（重复调用不报错）",
                       ack["data"]["acknowledged_at"] is not None,
                       f"already={ack['data']['already']}", 0.0)

        notify = check(client, "心理预警触达（未触达全量）", "POST", "/api/v1/alerts/notify",
                       headers=mgr_h, json={})
        if notify:
            d = notify["data"]
            if d["total"]:
                first = d["items"][0]
                record("  └ 留痕：处理人 + 干预建议",
                       bool(first["notified_to"]) and len(first["advice"]) >= 3,
                       f"触达 {d['total']} 条 → {first['notified_to_name']}", 0.0)
            else:
                record("  └ 已触达的不重复写（幂等）", True, d["message"], 0.0)

        check(client, "预警触达限管理层（预期 403）", "POST", "/api/v1/alerts/notify",
              expect=403, expect_code=40300, headers=staff_h, json={})
        check(client, "预警列表按触达状态过滤", "GET", "/api/v1/alerts", headers=mgr_h,
              params={"notified": False})

        todo_chat = check(client, "对话：主动待办推送意图", "POST", "/api/v1/chat/message",
                          headers=staff_h,
                          json={"session_id": "smoke-todo",
                                "message": "今天有什么待办要处理？"})
        if todo_chat:
            d = todo_chat["data"]
            record("  └ 路由 todo_push 且答复是「先问再答」",
                   d["intent"] == "todo_push" and "有没有" in d["answer"],
                   f"agent={d['agent']} intent={d['intent']}", 0.0)

        alert_chat = check(client, "对话：心理预警汇总（管理层）", "POST",
                           "/api/v1/chat/message", headers=mgr_h,
                           json={"session_id": "smoke-alert",
                                 "message": "现在有没有心理预警？"})
        if alert_chat:
            d = alert_chat["data"]
            record("  └ 路由 alert_digest 且给出建议动作",
                   d["intent"] == "alert_digest" and "建议动作" in d["answer"],
                   f"intent={d['intent']}", 0.0)

        check(client, "对话：取数问法仍走 NL2SQL 模板（不被新规则截胡）", "POST",
              "/api/v1/chat/message", headers=mgr_h,
              json={"session_id": "smoke-alert-nl2sql", "message": "查一下心理预警情况"})

        # ---------------- M5 五类业务报告 ----------------
        types = check(client, "报告口径清单（五类）", "GET", "/api/v1/reports/types",
                      headers=mgr_h)
        if types:
            record("  └ 五类报告齐全且带默认周期",
                   len(types["data"]["items"]) == 5
                   and all(t["default_days"] >= 1 for t in types["data"]["items"]),
                   "、".join(t["key"] for t in types["data"]["items"]), 0.0)

        report_id = None
        for key, label in (("customer_ops", "全域客户经营分析"),
                           ("daily_digest", "日报汇总（日）"),
                           ("weekly_digest", "日报汇总（周）"),
                           ("mental_weekly", "心理健康周报"),
                           ("complaint_weekly", "投诉处理周报")):
            created = check(client, f"生成报告：{label}", "POST", "/api/v1/reports/generate",
                            headers=mgr_h, json={"report_type": key})
            if created:
                d = created["data"]
                if report_id is None:
                    report_id = d["id"]
                record("  └ 真实聚合：报告期 + 指标 + 结论",
                       bool(d["metrics"]) and d["period"]["days"] >= 1 and bool(d["summary"]),
                       f"#{d['id']} {d['period']['label']} 指标 {len(d['metrics'])} 项", 0.0)

        check(client, "生成报告（兼容旧值 weekly）", "POST", "/api/v1/reports/generate",
              headers=mgr_h, json={"report_type": "weekly", "title": "冒烟兼容周报"})
        check(client, "未知报告类型被拒（预期 400）", "POST", "/api/v1/reports/generate",
              expect=400, expect_code=40000, headers=mgr_h, json={"report_type": "nope"})

        if report_id:
            for fmt, tag in (("xlsx", "Excel"), ("pdf", "PDF"),
                             ("print", "打印版 HTML"), ("md", "Markdown")):
                started = time.perf_counter()
                resp = client.get(f"/api/v1/reports/{report_id}/export",
                                  params={"format": fmt}, headers=mgr_h)
                cost = (time.perf_counter() - started) * 1000
                record(f"报告导出：{tag}",
                       resp.status_code == 200 and len(resp.content) > 300,
                       f"HTTP {resp.status_code} {len(resp.content)}B", cost)
            check(client, "导出格式不支持被拒（预期 400）", "GET",
                  f"/api/v1/reports/{report_id}/export", expect=400, expect_code=40000,
                  headers=mgr_h, params={"format": "docx"})
            check(client, "报告导出限管理层（预期 403）", "GET",
                  f"/api/v1/reports/{report_id}/export", expect=403, expect_code=40300,
                  headers=staff_h, params={"format": "xlsx"})

        sched = check(client, "手动触发定时报告生成", "POST",
                      "/api/v1/reports/scheduled/run", headers=mgr_h, params={"force": "true"})
        if sched:
            d = sched["data"]
            record("  └ 幂等：重复触发只跳过、不重复生成",
                   d["created_count"] + d["skipped_count"] == 5,
                   f"生成 {d['created_count']} / 跳过 {d['skipped_count']}", 0.0)

        # ---------------- 数据能力 ----------------
        q1 = check(client, "NL2SQL：客户线索统计", "POST", "/api/v1/nl2sql/query",
                   headers=staff_h, json={"question": "统计一下客户线索的数量"})
        if q1:
            d = q1["data"]
            record("  └ 命中受控模板并返回预览 SQL",
                   d["template_id"] == "lead_status_stats" and d["sql_preview"].lower().startswith("select"),
                   f"模板={d['template_id']} 行数={d['row_count']}", 0.0)
        q2 = check(client, "NL2SQL：按姓名查跟进", "POST", "/api/v1/nl2sql/query",
                   headers=staff_h, json={"question": "帮我查一下赵六的跟进记录"})
        if q2:
            record("  └ 人名参数抽取正确", "赵六" in q2["data"]["sql_preview"],
                   q2["data"]["sql_preview"][:80], 0.0)
        check(client, "NL2SQL：越权查敏感模板（预期 403）", "POST", "/api/v1/nl2sql/query",
              expect=403, expect_code=40300, headers=staff_h,
              json={"question": "查一下心理预警情况"})
        check(client, "审计日志（管理层）", "GET", "/api/v1/audit/logs",
              headers=mgr_h, params={"limit": 5})
        check(client, "审计日志（员工被拒，预期 403）", "GET", "/api/v1/audit/logs",
              expect=403, expect_code=40300, headers=staff_h)

        # ---------------- 内部工具回调 ----------------
        check(client, "工具清单", "GET", "/internal/tools")

        def call_tool(name: str, params: dict, tag: str, expect: int = 200):
            body = json.dumps({"params": params}, ensure_ascii=False).encode()
            ts = str(time.time())
            return check(client, tag, "POST", f"/internal/tools/{name}", expect=expect,
                         expect_code=0 if expect == 200 else None,
                         content=body,
                         headers={"content-type": "application/json",
                                  "x-tool-timestamp": ts,
                                  "x-tool-signature": sign_tool_payload(body, ts)})

        call_tool("lead_lookup", {"name": "赵六"}, "工具：查客户（签名校验通过）")
        call_tool("student_scores", {"student_id": 1}, "工具：查成绩")
        call_tool("pending_requests", {}, "工具：待审批申请")
        call_tool("nl2sql", {"question": "统计一下客户线索的数量"}, "工具：NL2SQL")
        body = json.dumps({"params": {"name": "赵六"}}, ensure_ascii=False).encode()
        check(client, "工具：伪造签名被拒（预期 403）", "POST", "/internal/tools/lead_lookup",
              expect=403, expect_code=None, content=body,
              headers={"content-type": "application/json",
                       "x-tool-timestamp": str(time.time()),
                       "x-tool-signature": "0" * 64})

    # ---------------- 汇总 ----------------
    passed = sum(1 for r in RESULTS if r[1] == "PASS")
    failed = len(RESULTS) - passed
    total_ms = sum(r[3] for r in RESULTS)

    lines = [
        "# 冒烟测试报告",
        "",
        f"- 目标服务：`{args.base}`",
        f"- 执行时间：{started_at:%Y-%m-%d %H:%M:%S}",
        f"- 用例总数：**{len(RESULTS)}**，通过 **{passed}**，失败 **{failed}**",
        f"- 累计请求耗时：{total_ms:.0f} ms",
        "",
        "| # | 用例 | 结果 | 耗时(ms) | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for i, (name, flag, detail, cost, _) in enumerate(RESULTS, 1):
        mark = "✅" if flag == "PASS" else "❌"
        lines.append(f"| {i} | {name} | {mark} {flag} | {cost:.1f} | {detail} |")
    lines.append("")
    lines.append(f"结论：**{'全部通过' if failed == 0 else f'{failed} 条未通过，需排查'}**")
    lines.append("")

    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "smoke_report.md")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n== 汇总：{passed}/{len(RESULTS)} 通过，{failed} 失败，累计 {total_ms:.0f} ms")
    print(f"== 报告已写入 {out_file}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
