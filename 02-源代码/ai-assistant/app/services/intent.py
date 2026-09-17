"""意图识别与路由（两阶段）。

阶段一 **预筛**：关键词/正则 + 角色白名单，命中高确定性意图，完全不调模型。
阶段二 **模型**：未命中时调用 Dify `router` 应用（内部是 question-classifier 节点），
               返回结构化 JSON；`mock` 模式下用内置规则模拟。

路由结果还会过一次 **权限过滤**：角色不允许的 Agent 会被降级到该角色的默认 Agent，
避免访客通过话术越权触达员工/管理侧能力。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Set, Tuple

from .dify import APP_ROUTER, dify_client

logger = logging.getLogger(__name__)

# 角色 -> 可访问的 Agent 集合。
#
# 注意 `customer_service` 对所有角色开放：它装的是机构政策、校区地址、费用、
# 签证流程这类**公共信息**，不该按角色设卡。曾经的写法只给 visitor/admin，
# 结果员工或学员问「你们机构的服务流程是怎样的」会被判越权、降级到别的 Agent，
# 反而答不出来。
ROLE_AGENTS: Dict[str, Set[str]] = {
    "visitor": {"customer_service"},
    "student": {"customer_service", "student_helper", "mental_care"},
    "employee": {"customer_service", "enterprise_assistant", "student_helper"},
    "manager": {"customer_service", "enterprise_assistant", "reporter", "student_helper"},
    "admin": {"customer_service", "student_helper", "enterprise_assistant",
              "mental_care", "reporter", "screener"},
}

# 角色的兜底 Agent（越权或无法识别时用）
ROLE_DEFAULT_AGENT: Dict[str, str] = {
    "visitor": "customer_service",
    "student": "student_helper",
    "employee": "enterprise_assistant",
    "manager": "enterprise_assistant",
    "admin": "enterprise_assistant",
}

# 阶段一预筛规则：(关键词, agent, intent, 置信度)
PREFILTER_RULES: Tuple[Tuple[Tuple[str, ...], str, str, float], ...] = (
    (("我要请假", "请假申请", "请个假", "考务申请"), "student_helper", "leave_apply", 0.95),
    (("我要投诉", "投诉", "意见建议"), "student_helper", "after_sales", 0.93),
    (("活动报名", "我要报名", "报名参加"), "student_helper", "activity_enroll", 0.93),
    (("提交日报", "写日报", "口述日报"), "enterprise_assistant", "daily_report", 0.94),
    (("入职指引", "新人指引", "入职流程", "新人入职", "入职第一天",
      "入职要准备", "新员工入职"), "enterprise_assistant", "onboarding", 0.92),
    # 主动待办推送（需求：企业助手「主动待办推送」）。
    # 必须排在 onboarding 之后：「新人入职待办清单」两个规则都沾边，
    # 前者的关键词更长更具体，应该由 onboarding 赢。
    (("待办", "待处理事项", "有什么要处理", "有没有要审批", "有没有要跟进",
      "有没有待审批", "待办提醒", "主动提醒", "今天要做什么"),
     "enterprise_assistant", "todo_push", 0.93),
    # 心理预警汇总：数据敏感，只有管理层能看，越权由 handler 内部拒绝
    # （这里不能靠 ROLE_AGENTS 拦，因为 employee 对 enterprise_assistant 本来就放行）
    #
    # ⚠️ 关键词只收「状态询问」句式，**不能收裸的「心理预警」「预警情况」**：
    # 「查一下心理预警情况」是取数请求，应该走 NL2SQL 的受控查询模板（有 SQL 预览、
    # 有行数上限、按角色分范围）；一旦被这条规则截胡，就变成「给你一段汇总话术」，
    # 等于把一个已交付的取数能力吃掉了（tests/test_mock_agent.py 有断言锁着这件事）。
    (("有没有预警", "有没有心理预警", "预警清单", "高危学生", "预警触达",
      "预警处理进度", "预警要不要处理"),
     "enterprise_assistant", "alert_digest", 0.92),
    (("生成周报", "生成月报", "出个报告"), "reporter", "report_generate", 0.94),
    (("转人工", "找人工", "人工客服"), "customer_service", "handover", 0.99),
)


@dataclass
class IntentResult:
    agent: str
    intent: str
    confidence: float
    slots: Dict[str, Any] = field(default_factory=dict)
    route_source: str = "fallback"       # prefilter | dify | fallback
    action: str = "dispatch"             # dispatch | clarify | fallback
    permission_denied: bool = False
    original_agent: str = ""

    @property
    def needs_clarification(self) -> bool:
        return self.action == "clarify"


def _prefilter(message: str) -> IntentResult | None:
    for keywords, agent, intent, confidence in PREFILTER_RULES:
        if any(k in message for k in keywords):
            return IntentResult(agent=agent, intent=intent, confidence=confidence,
                                route_source="prefilter")
    return None


def _apply_permission(result: IntentResult, role: str) -> IntentResult:
    allowed = ROLE_AGENTS.get(role, ROLE_AGENTS["visitor"])
    if result.agent in allowed:
        return result
    fallback_agent = ROLE_DEFAULT_AGENT.get(role, "customer_service")
    logger.info("角色 %s 无权访问 Agent %s，降级到 %s", role, result.agent, fallback_agent)
    return IntentResult(
        agent=fallback_agent,
        intent=result.intent,
        confidence=min(result.confidence, 0.5),
        slots=result.slots,
        route_source=result.route_source,
        action="fallback",
        permission_denied=True,
        original_agent=result.agent,
    )


async def route(message: str, role: str, user: str) -> IntentResult:
    """返回最终意图；调用方据此分派 Dify 应用。"""
    hit = _prefilter(message)
    if hit is None:
        try:
            data = await dify_client.run_workflow(
                APP_ROUTER, {"query": message, "role": role}, user
            )
            outputs = data.get("outputs", {})
            hit = IntentResult(
                agent=outputs.get("agent", "customer_service"),
                intent=outputs.get("intent", "unknown"),
                confidence=float(outputs.get("confidence", 0.5)),
                slots=outputs.get("slots") or {},
                route_source="dify",
            )
        except Exception as exc:                       # noqa: BLE001
            logger.warning("意图路由失败，走兜底: %s", exc)
            hit = IntentResult(agent="customer_service", intent="unknown",
                               confidence=0.3, route_source="fallback")

    result = _apply_permission(hit, role)

    # 置信度分级（与文档 4.2 节一致）
    if not result.permission_denied:
        if result.confidence >= 0.75:
            result.action = "dispatch"
        elif result.confidence >= 0.5:
            result.action = "clarify"
        else:
            result.action = "fallback"
    return result
