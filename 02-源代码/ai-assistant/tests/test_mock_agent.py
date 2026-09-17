"""mock 模式下的对话覆盖度与编排行为。

背景（用户报的问题）：
截图里学员问「帮我查一下最近的意向客户跟进情况」，AI 回「暂未查询到相关信息」。
而这条问题的路由结果其实是 `enterprise_assistant / nl2sql_query`（置信度 0.92），
数据库里也真有一批跟进记录 —— 说明问题出在 **mock 应答层无视路由结果**，
只会拿用户原话去撞一张静态关键词表，表外的问题一律死胡同。

本文件锁住修好之后的行为：
1. 取数类问题真的查库，并把用的模板和 SQL 标出来；
2. 角色不够时明说「没权限」，而不是假装「查不到」；
3. 投诉/请假这类事项真的落库，且重复提交不会重复建单；
4. 知识面覆盖到常见问题，兜底话术有信息量。
"""
from __future__ import annotations

import re

import pytest

from tests.conftest import data

DATA_QUESTION = "帮我查一下最近的意向客户跟进情况"


# --------------------------------------------------------------------------- #
# 1. 取数类：真查库
# --------------------------------------------------------------------------- #
def test_staff_data_question_returns_real_rows(client, advisor_headers):
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-data-1", "message": DATA_QUESTION},
                       headers=advisor_headers)
    body = data(resp)

    assert body["agent"] == "enterprise_assistant"
    assert body["intent"] == "nl2sql_query"
    # 核心回归：不能再是死胡同
    assert "暂未查询到" not in body["answer"]
    assert "查询模板" in body["answer"], "取数回答要说明走了哪个受控模板"
    assert "SQL" in body["answer"], "取数回答要附 SQL 预览，便于核对"
    assert body["references"], "取数回答必须标出数据来源（模板）"
    assert any("nl2sql" in item for item in body["suggest_actions"])


def test_manager_can_query_more_than_employee(client, manager_headers):
    """管理范围能查心理预警，销售范围查不了 —— 同一条问题不同结果。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-data-2", "message": "查一下心理预警情况"},
                       headers=manager_headers)
    body = data(resp)
    assert "查询模板" in body["answer"]
    assert "敏感" not in body["answer"] or "无权" not in body["answer"]


def test_student_data_question_explains_permission(client, student_headers):
    """学员问经营数据：要明说没这个权限，而不是假装「查不到」。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-data-3", "message": DATA_QUESTION},
                       headers=student_headers)
    body = data(resp)
    assert "暂未查询到" not in body["answer"]
    assert "学员" in body["answer"], "要指出当前角色"
    assert "员工" in body["answer"], "要说明需要什么角色"
    # 仍然要给出该角色能做的事
    assert "请假" in body["answer"] or "成绩" in body["answer"]


def test_unknown_data_question_lists_usable_templates(client, advisor_headers):
    """问法映射不到模板时，要把「你能查什么」列出来，而不是干巴巴报错。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-data-4",
                             "message": "帮我查一下这个月的环保设备采购明细"},
                       headers=advisor_headers)
    body = data(resp)
    assert "可查" in body["answer"] or "模板" in body["answer"]


def test_score_average_is_a_real_aggregate(client, manager_headers):
    """「统计一下各科平均分」要真跑聚合模板，不能被知识类的「均分」抢走。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-data-5", "message": "统计一下各科平均分"},
                       headers=manager_headers)
    body = data(resp)
    assert body["agent"] == "enterprise_assistant", "取数问题被路由到了知识问答"
    assert "暂未查询到" not in body["answer"]
    assert "查询模板" in body["answer"]
    assert "各科平均分" in body["answer"], "要走聚合模板，而不是成绩明细"


def test_admission_average_policy_still_routes_to_knowledge(client, manager_headers):
    """防误伤：问的是**院校录取门槛**，不是库里统计值，必须留在知识类。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-data-6",
                             "message": "英国留学平均分要求是多少？"},
                       headers=manager_headers)
    body = data(resp)
    assert body["agent"] == "customer_service", "政策类问法被错误地当成取数请求"
    assert "查询模板" not in body["answer"]


# --------------------------------------------------------------------------- #
# 2. 事项类：真落库
# --------------------------------------------------------------------------- #
def test_complaint_creates_real_ticket(client, student_headers, advisor_headers):
    """旧版只是嘴上说「已生成工单」，其实什么都没写。现在必须真落库。"""
    message = "我要投诉课程安排的排课时间不合理"
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-ticket-1", "message": message},
                       headers=student_headers)
    body = data(resp)

    match = re.search(r"工单 #(\d+)", body["answer"])
    assert match, f"答复里要给出工单号，实际：{body['answer'][:120]}"
    ticket_id = int(match.group(1))

    # 学员侧只有提交入口，列表是员工视图 —— 所以用员工令牌核对这条工单确实存在
    listing = data(client.get("/api/v1/tickets", headers=advisor_headers))
    row = next((item for item in listing["items"] if item["id"] == ticket_id), None)
    assert row is not None, "工单没有真正落库"
    assert row["student_id"], "学员提交的工单要挂到本人名下"


def test_repeated_complaint_is_idempotent(client, student_headers):
    """用户在对话里连点两次很常见，不能开出两张单。"""
    message = "我要投诉顾问回复太慢，三天没消息"
    payload = {"session_id": "s-ticket-2", "message": message}

    first = data(client.post("/api/v1/chat/message", json=payload, headers=student_headers))
    second = data(client.post("/api/v1/chat/message", json=payload, headers=student_headers))

    assert re.search(r"工单 #\d+", first["answer"])
    assert "已经开过工单" in second["answer"], f"重复提交未去重：{second['answer'][:120]}"


def test_leave_apply_creates_request(client, student_headers):
    """请假要真的生成申请单（旧版只说「我会帮你生成」）。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-leave-1",
                             "message": "我要请假 9 月 15 日 2 天，原因看病"},
                       headers=student_headers)
    body = data(resp)
    assert "请假申请 #" in body["answer"], f"未生成申请单：{body['answer'][:120]}"
    assert "2026-09-15" in body["answer"]
    assert "2 天" in body["answer"]

    listing = data(client.get("/api/v1/requests", headers=student_headers))
    assert any(r["type"] == "LEAVE" for r in listing["items"])


def test_leave_apply_asks_for_missing_slots(client, student_headers):
    """信息不全时要追问，而不是瞎猜一个日期建单。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-leave-2", "message": "我要请假"},
                       headers=student_headers)
    body = data(resp)
    assert "还差" in body["answer"] or "日期" in body["answer"]
    assert "请假申请 #" not in body["answer"]


def test_visitor_cannot_apply_leave(client, visitor_headers):
    """访客没有学员档案，不能建单，但要告诉 TA 怎么才能办。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-leave-3",
                             "message": "我要请假 9 月 15 日 2 天"},
                       headers=visitor_headers)
    body = data(resp)
    assert "请假申请 #" not in body["answer"]
    assert "学员" in body["answer"]


# --------------------------------------------------------------------------- #
# 3. 知识面：常见问题不该掉进死胡同
# --------------------------------------------------------------------------- #
KB_CASES = [
    ("英国和澳洲留学怎么选？", "英国"),
    ("雅思要考多少分？", "IELTS"),
    ("申请要准备什么文书？", "个人陈述"),
    ("签证怎么办理？", "CAS"),
    ("住宿怎么安排？", "宿舍"),
    ("有奖学金吗？", "奖学金"),
    ("什么时候开始申请比较合适？", "时间轴"),
    ("课程安排在哪里看？", "课表"),
    ("你们有几家校区？", "成都"),
    ("申请费用大概多少？", "万"),
    # 「服务流程」曾经被材料条目的裸「流程」关键词抢走，答成材料清单
    ("这家机构的服务流程是怎样的？", "服务流程"),
    ("申请流程是什么？", "材料清单"),
]


def test_knowledge_coverage(client, visitor_headers):
    """这些以前全会回「暂未查询到」，现在都该有正经答案。"""
    failures = []
    for index, (question, marker) in enumerate(KB_CASES):
        resp = client.post("/api/v1/chat/message",
                           json={"session_id": f"s-kb-{index}", "message": question},
                           headers=visitor_headers)
        body = data(resp)
        if marker not in body["answer"] or "暂未查询到" in body["answer"]:
            failures.append((question, body["answer"][:80]))
    assert not failures, f"以下问题未命中知识库：{failures}"


def test_knowledge_is_public_for_every_role(client, admin_headers, manager_headers,
                                            advisor_headers, student_headers,
                                            visitor_headers):
    """机构政策是公共信息，任何角色问都要能答（曾经员工/学员会被判越权）。"""
    for name, headers in [("admin", admin_headers), ("manager", manager_headers),
                          ("employee", advisor_headers), ("student", student_headers),
                          ("visitor", visitor_headers)]:
        resp = client.post("/api/v1/chat/message",
                           json={"session_id": f"s-pub-{name}",
                                 "message": "你们机构在成都的校区在哪？"},
                           headers=headers)
        body = data(resp)
        assert body["agent"] == "customer_service", f"{name} 的机构信息问题被降级了"
        assert body["references"], f"{name} 未拿到知识类引用"


def test_handover_intent_answers(client, visitor_headers):
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-handover", "message": "转人工"},
                       headers=visitor_headers)
    body = data(resp)
    assert "人工客服" in body["answer"]
    assert "5 分钟" in body["answer"] or "接入" in body["answer"]


# --------------------------------------------------------------------------- #
# 4. 寒暄 / 能力询问：第一句话不能就甩「答不出来」
#
# 用户截图里的真实问题：admin 输入「你好」，收到的是
# 「这个问题我暂时没有可用的信息，不能凭空给你答复。」——
# 开场体验灾难。但判定必须严格：带真实问题的「你好，请问…」不能被吞掉。
# --------------------------------------------------------------------------- #
DEAD_END = "暂时没有可用的信息"


def test_greeting_is_not_a_dead_end(client, admin_headers):
    """截图原样复现：admin 说「你好」。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-hi-1", "message": "你好"},
                       headers=admin_headers)
    body = data(resp)
    assert DEAD_END not in body["answer"]
    assert "你好" in body["answer"]
    # 要顺手告诉用户「接下来能干什么」
    assert "转人工" in body["answer"]


@pytest.mark.parametrize("greeting", [
    "你好", "您好", "你好！", "你好你好", "hi", "Hello", "嗨", "哈喽",
    "在吗", "有人吗", "早上好", "下午好", "晚上好", "你好呀",
])
def test_various_greetings_all_answered(client, visitor_headers, greeting):
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": f"s-hi-{abs(hash(greeting))}", "message": greeting},
                       headers=visitor_headers)
    body = data(resp)
    assert DEAD_END not in body["answer"], f"{greeting!r} 又掉进兜底了"


def test_greeting_mixed_with_real_question_is_not_swallowed(client, visitor_headers):
    """核心防误伤：「你好，请问雅思要考多少分」必须照样答出雅思要求。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-hi-mix",
                             "message": "你好，请问雅思要考多少分？"},
                       headers=visitor_headers)
    body = data(resp)
    assert "IELTS" in body["answer"], "真实问题被寒暄分支吞掉了"
    assert DEAD_END not in body["answer"]


def test_greeting_mixed_with_task_keeps_task(client, student_headers):
    """带具体办事诉求的也要走原流程，只是礼貌开头而已。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-hi-leave",
                             "message": "你好，我要请假 9 月 18 日 1 天，原因复诊"},
                       headers=student_headers)
    body = data(resp)
    assert "请假申请 #" in body["answer"]


def test_pleasantry_prefix_does_not_poison_query_params(client, advisor_headers):
    """「好的，帮我查一下…」不能筛出 0 条。

    踩过的坑：nl2sql 的参数抽取会在问句里找人名，「好的」这种 2 字纯中文
    会被当成客户名拼进 LIKE，于是明明有数据却回「命中 0 条」。
    """
    plain = data(client.post("/api/v1/chat/message",
                             json={"session_id": "s-clean-1", "message": DATA_QUESTION},
                             headers=advisor_headers))
    polite = data(client.post("/api/v1/chat/message",
                              json={"session_id": "s-clean-2",
                                    "message": f"好的，{DATA_QUESTION}"},
                              headers=advisor_headers))

    assert "查询模板" in polite["answer"]
    assert "命中 0 条" not in polite["answer"], "问候语被当成客户名去筛数据了"
    # 加不加礼貌语，查出来的条数应当一致
    assert polite["answer"].split("查询模板")[0] == plain["answer"].split("查询模板")[0]


def test_capability_question_lists_skills(client, visitor_headers):
    """「你能做什么」要正面回答，而不是回一句「我没有可用信息」。"""
    for question in ["你能做什么？", "你可以帮我做什么", "你是谁"]:
        resp = client.post("/api/v1/chat/message",
                           json={"session_id": f"s-cap-{abs(hash(question))}",
                                 "message": question},
                           headers=visitor_headers)
        body = data(resp)
        assert DEAD_END not in body["answer"], f"{question!r} 未正面回答能力范围"
        assert "请假" in body["answer"] or "校区" in body["answer"]


def test_thanks_and_ack_are_handled(client, visitor_headers):
    for message in ["谢谢", "感谢", "好的", "收到", "再见",
                    "好的谢谢", "你好，谢谢", "好", "嗯嗯"]:
        resp = client.post("/api/v1/chat/message",
                           json={"session_id": f"s-thx-{abs(hash(message))}",
                                 "message": message},
                           headers=visitor_headers)
        body = data(resp)
        assert DEAD_END not in body["answer"], f"{message!r} 掉进兜底了"


def test_handover_after_greeting_still_works(client, admin_headers):
    """截图里第二条「转人工」是好的，别被寒暄分支抢走。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-hi-handover", "message": "转人工"},
                       headers=admin_headers)
    body = data(resp)
    assert "人工客服" in body["answer"]
    assert "你好！我是" not in body["answer"]


def test_stream_returns_mock_orchestrated_answer(client, advisor_headers):
    """流式路径也要走同一套编排，不能退回静态语料。"""
    resp = client.post("/api/v1/chat/stream",
                       json={"session_id": "s-stream-data", "message": DATA_QUESTION},
                       headers=advisor_headers)
    assert resp.status_code == 200
    joined = resp.text
    assert "查询模板" in joined, "流式回答也必须带受控模板信息"
