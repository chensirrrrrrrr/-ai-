"""mock 模式下的本地 Agent 编排。

## 为什么要这一层

`DIFY_MODE=mock` 时 Dify 不在场。但统一对话入口依然要给出「像 Agent 一样」的答复，
而不是把用户原话丢给一张静态关键词表 —— 那样会出现很荒唐的结果：

    用户：帮我查一下最近的意向客户跟进情况     ← 系统里明明有这些数据
    AI  ：暂未查询到相关信息，建议咨询顾问获取准确答复。

这条问题的路由结果其实是 `enterprise_assistant / nl2sql_query`（置信度 0.92），
只是当时的 mock 应答层**完全无视路由结果**，所以数据类问题永远掉进死胡同。

本层按 intent 分派：能落库的落库、能查库的查库、知识类检索语料、
权限不够就明说「这不是你的角色」、都不命中就讲清能力边界并给可问的示例。

## 边界

这是**离线替代品**，不是「万能问答」。开放域自由问答需要真实模型：
配置 `DIFY_MODE=live` + `DIFY_APP_KEYS` 接上 Dify 后，本层不参与。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, List, Optional

from sqlalchemy.orm import Session

from ..core import AppError, Forbidden
from ..models import Activity, AfterSalesTicket, StudentRequest
from . import audit, nl2sql, onboarding, todo
from .dify import (APP_ENTERPRISE, MOCK_NO_RECALL_TEXT, DifyChatResult,
                   mock_kb_answer)

logger = logging.getLogger(__name__)

ROLE_LABELS = {
    "visitor": "访客",
    "student": "学员",
    "employee": "员工",
    "manager": "管理层",
    "admin": "管理员",
}

# 各 Agent 的职责，权限不够时用来解释「这件事归谁管」
AGENT_TITLES = {
    "customer_service": "客服问答",
    "student_helper": "学员服务",
    "enterprise_assistant": "内部经营与数据",
    "mental_care": "心理关怀",
    "reporter": "经营报告",
    "screener": "材料研判",
}

# 经营报告 / 日报的允许角色
REPORT_ROLES = {"manager", "admin"}
DAILY_REPORT_ROLES = {"employee", "manager", "admin"}
# 新人入职指引是内部文档，与日报同口径（学员 / 访客不可见）
ONBOARDING_ROLES = {"employee", "manager", "admin"}

MAX_PREVIEW_ROWS = 10
DUPLICATE_WINDOW_MINUTES = 10

LEAVE_DATE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
LEAVE_DAYS_RE = re.compile(r"(\d+)\s*天")
LEAVE_REASON_RE = re.compile(r"(?:原因|因为|事由)[：: ]?\s*([^\s，。；;,、]{1,20})")

CAN_DO_TEXT = (
    "我可以帮你：\n"
    "· 机构与政策：校区地址、申请材料、服务费用、签证流程、服务时间\n"
    "· 选校与专业：目的地对比、院校排名、转专业、均分与语言要求、奖学金\n"
    "· 学生事务：请假/考务申请、成绩查询、课程安排、活动报名、投诉与建议"
)


def _label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


def _conv(message: str) -> str:
    return f"conv-mock-{abs(hash(message)) % 100000:05d}"


def _reply(answer: str, *, conv: str, app: str = "",
           refs: Optional[List[dict]] = None,
           actions: Optional[List[str]] = None, **meta: Any) -> DifyChatResult:
    return DifyChatResult(
        answer=answer,
        conversation_id=conv,
        message_id="msg-mock",
        references=refs or [],
        suggest_actions=actions or [],
        metadata={"mode": "mock", "app": app, "orchestrated": True, **meta},
    )


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


# --------------------------------------------------------------------------- #
# 取数类：真的去查库
# --------------------------------------------------------------------------- #
def _handle_data(message: str, role: str, db: Session, *, conv: str, app: str) -> DifyChatResult:
    if role not in nl2sql.ROLE_SCOPE:
        return _reply(
            f"「{message}」属于内部经营数据，需要员工及以上角色才能查。\n"
            f"你当前的角色是「{_label(role)}」，看不到客户、线索、成绩这类数据。\n\n"
            f"{CAN_DO_TEXT}\n\n"
            "要看经营数据请用员工账号登录；也可以回复「转人工」，我让顾问帮你取。",
            conv=conv, app=app, permission_denied=True,
            actions=["转人工", "咨询机构信息"],
        )

    scope = nl2sql.ROLE_SCOPE[role]
    try:
        result = nl2sql.run_query(db, message, scope)
    except Forbidden as exc:
        usable = "、".join(t.title for t in nl2sql.applicable_templates(scope)) or "（无）"
        return _reply(
            f"{exc.message}\n\n你当前范围（{scope}）可查：{usable}",
            conv=conv, app=app, permission_denied=True,
        )
    except AppError as exc:
        usable = "、".join(t.title for t in nl2sql.applicable_templates(scope)) or "（无）"
        return _reply(
            f"{exc.message}\n\n你当前范围（{scope}）可查的模板：{usable}\n"
            "「查一下最近的意向客户跟进情况」这类句式最容易命中。",
            conv=conv, app=app, no_recall=True,
        )

    rows = result["rows"]
    title = result.get("template_title") or result["template_id"]
    lines = [result["answer_text"], ""]
    if rows:
        for i, row in enumerate(rows[:MAX_PREVIEW_ROWS], 1):
            cells = " · ".join(f"{col}：{_fmt(val)}"
                               for col, val in zip(result["columns"], row))
            lines.append(f"{i}. {cells}")
        rest = len(rows) - MAX_PREVIEW_ROWS
        if rest > 0:
            lines.append(f"…… 还有 {rest} 条，完整结果去「数据洞察」页看。")
    else:
        lines.append("条件没筛到数据，换个关键词再试（例如直接给客户姓名）。")
    lines += ["", f"查询模板：{title}（受控 SELECT，只读，有行数上限）",
              f"SQL：{result['sql_preview']}"]

    return _reply(
        "\n".join(lines), conv=conv, app=app,
        refs=[{"doc": "受控查询模板", "section": title, "version": "内置"}],
        actions=["在「数据洞察」页查看完整结果", "换个筛选条件",
                 "调用 /api/v1/nl2sql/query 获取数据结果"],
        template_id=result["template_id"], row_count=result["row_count"],
    )


# --------------------------------------------------------------------------- #
# 事项类：能落库的就落库
# --------------------------------------------------------------------------- #
def _handle_after_sales(message: str, role: str, db: Session, *,
                        subject: str, ref_id: Optional[int],
                        conv: str, app: str) -> DifyChatResult:
    since = datetime.now() - timedelta(minutes=DUPLICATE_WINDOW_MINUTES)
    dup = (db.query(AfterSalesTicket)
             .filter(AfterSalesTicket.content == message,
                     AfterSalesTicket.created_at >= since)
             .first())
    if dup is not None:
        return _reply(
            f"这条反馈刚刚已经开过工单了：工单 #{dup.id}（{dup.status}），"
            "售后专员会跟进，不用重复提交。\n"
            "如果你要补充信息，直接把补充内容发我，我会追加到同一件事上。",
            conv=conv, app=app, duplicated=True, ticket_id=dup.id,
        )

    summary = message[:60] + ("..." if len(message) > 60 else "")
    row = AfterSalesTicket(
        student_id=ref_id if role == "student" else None,
        content=message, summary=summary, category="投诉", status="OPEN",
    )
    db.add(row)
    db.flush()                                   # 拿到 id；commit 由调用方统一做
    audit.record(db, action="ticket.create", actor_id=subject, actor_role=role,
                 resource="after_sales_ticket", resource_id=row.id,
                 detail={"source": "chat", "category": "投诉"})

    # 注意：工单「列表」页只对员工及以上开放（/api/v1/tickets 是 staff_only），
    # 学员侧只有提交入口 —— 所以别对学员承诺一个打不开的页面。
    if role == "student":
        how = ("售后专员会在 1 个工作日内首次响应；"
               "想看进度或补充信息，直接在这里跟我说，我帮你带话给专员。")
        actions = ["补充信息", "转人工"]
    else:
        how = ("售后专员会在 1 个工作日内首次响应，"
               "进度到「学员中心 → 售后工单」里流转；要催办直接说一声。")
        actions = ["查看工单列表", "补充信息", "转人工"]

    return _reply(
        f"很抱歉给你带来不好的体验。已生成售后工单 #{row.id}，状态「待处理」。\n"
        f"工单摘要：{summary}\n" + how,
        conv=conv, app=app, ticket_id=row.id,
        actions=actions,
    )


def _handle_leave_apply(message: str, role: str, db: Session, *,
                        subject: str, ref_id: Optional[int],
                        conv: str, app: str) -> DifyChatResult:
    if role != "student" or not ref_id:
        return _reply(
            "请假 / 考务申请需要用学员本人账号提交。\n"
            "登录学员账号后，直接说「我要请假 9 月 15 日 2 天，原因看病」，我就能帮你起单，"
            "带教老师审批后结果会通过公众号推送。\n\n"
            "如果你是员工要帮学生代提交，请到「学员中心 → 我的申请」里选学生操作。",
            conv=conv, app=app, permission_denied=role != "student",
            actions=["转人工"],
        )

    m_date = LEAVE_DATE_RE.search(message)
    m_days = LEAVE_DAYS_RE.search(message)
    if not (m_date and m_days):
        missing = []
        if not m_date:
            missing.append("起始日期（例如「9 月 15 日」）")
        if not m_days:
            missing.append("请假天数（例如「2 天」）")
        return _reply(
            "可以，我帮你起请假单，还差：" + "、".join(missing) + "。\n"
            "补成一句话就行，例如「我要请假 9 月 15 日 2 天，原因看病」。",
            conv=conv, app=app, need_slots=True,
        )

    try:
        start = date(datetime.now().year, int(m_date.group(1)), int(m_date.group(2)))
    except ValueError:
        return _reply("日期没看懂，换个写法试试，例如「9 月 15 日」。",
                      conv=conv, app=app, need_slots=True)
    days = int(m_days.group(1))
    m_reason = LEAVE_REASON_RE.search(message)
    reason = m_reason.group(1) if m_reason else None

    key = f"chat-{subject}-{start.isoformat()}-{days}"
    exist = (db.query(StudentRequest)
               .filter(StudentRequest.idempotency_key == key)
               .first())
    if exist is not None:
        return _reply(
            f"这份请假申请已经提交过了：申请单 #{exist.id}（{exist.status}），不用重复提交。",
            conv=conv, app=app, duplicated=True, request_id=exist.id,
        )

    row = StudentRequest(
        student_id=ref_id, type="LEAVE", status="PENDING",
        content={"start_date": start.isoformat(), "days": days, "reason": reason},
        idempotency_key=key,
    )
    db.add(row)
    db.flush()
    audit.record(db, action="request.apply", actor_id=subject, actor_role=role,
                 resource="student_request", resource_id=row.id,
                 detail={"source": "chat", "type": "LEAVE"})

    return _reply(
        f"已提交请假申请 #{row.id}：{start.isoformat()} 起 {days} 天"
        + (f"，事由「{reason}」。" if reason else "。") + "\n"
        "当前状态「待审批」，带教老师在后台审批后会通过公众号推送结果；"
        "要撤回或改天数，跟我说一声。",
        conv=conv, app=app, request_id=row.id,
        actions=["查看我的申请", "修改申请"],
    )


# --------------------------------------------------------------------------- #
# 其余意图：给可执行的信息 + 别越权
# --------------------------------------------------------------------------- #
def _handle_report(message: str, role: str, *, conv: str, app: str) -> DifyChatResult:
    if role not in REPORT_ROLES:
        return _reply(
            f"经营报告属于管理层能力，你当前的角色是「{_label(role)}」，"
            "暂时打不开这一项。\n\n"
            "到「运营中心 → 报告」可以看到你有权限的日报与周报；"
            "需要更高权限请找管理员开通，或回复「转人工」。",
            conv=conv, app=app, permission_denied=True,
        )
    return _reply(
        "报告生成会走「取数 → 指标计算 → 叙述生成」三步，输出周报/月报草稿。\n"
        "在「运营中心 → 报告」里点生成即可，也可以告诉我报告范围"
        "（例如「上周的线索转化」），我按范围重跑一次。",
        conv=conv, app=app, actions=["生成周报", "查看历史报告"],
    )


def _handle_daily_report(message: str, role: str, *, conv: str, app: str) -> DifyChatResult:
    if role not in DAILY_REPORT_ROLES:
        return _reply(
            f"日报是员工侧能力，你当前的角色是「{_label(role)}」，提交不了。\n"
            "如果你确实需要提交，请找管理员确认账号权限。",
            conv=conv, app=app, permission_denied=True,
        )
    return _reply(
        "日报支持文字或语音口述。到「运营中心 → 日报」点语音录入，"
        "说清「考试类型 + 分数 + 日期」这类要点，我负责转写并抽成结构化字段，"
        "确认无误再提交。",
        conv=conv, app=app, actions=["去提交日报"],
    )


def _handle_onboarding(message: str, role: str, db: Session, *,
                       ref_id: Optional[int], conv: str, app: str) -> DifyChatResult:
    """新人入职指引：从版本化知识资产里召回，并附上「该找谁」的关键联系人。"""
    hit = onboarding.answer(message, db, ref_id)
    lines: List[str] = []

    if hit["matched"]:
        if hit.get("question"):
            lines.append(f"【{hit['question']}】")
        lines.append(hit["answer"])
        lines += ["",
                  f"（依据：《{onboarding.GUIDE_TITLE}》{onboarding.GUIDE_VERSION} · "
                  f"负责人：{hit['owner']} · 更新：{onboarding.GUIDE_UPDATED_AT}）"]
        source = [{"doc": onboarding.GUIDE_TITLE,
                   "section": hit.get("question") or hit.get("stage") or "入职指引",
                   "version": onboarding.GUIDE_VERSION}]
    else:
        g = onboarding.guide()
        lines.append("我手上的《新人入职指引》里没直接写这件事。指引覆盖这几个阶段：")
        for s in g["stages"]:
            lines.append(f"· {s['name']}（{s['window']}）：{s['goal']}")
        lines += ["",
                  "换个说法再问也可以，例如「报到要带什么」「日报怎么提交」"
                  "「我的带教老师是谁」；或者直接说阶段名（D0 / D1 / 第一周 / 第一个月 / 转正）。"]
        source = [{"doc": onboarding.GUIDE_TITLE, "section": "指引目录",
                   "version": onboarding.GUIDE_VERSION}]

    # 关键联系人：新人最需要的其实是「这件事找谁」
    contacts = onboarding.resolve_contacts(db, ref_id)
    person = {c["key"]: c for c in contacts["items"]}
    bits = []
    for key in ("manager", "buddy", "hr"):
        entry = person.get(key) or {}
        who = entry.get("contact")
        label = entry.get("label")
        if who:
            bits.append(f"{label}：{who['name']}"
                        f"（{who.get('title') or who.get('department') or '-'}）")
        elif entry.get("note"):
            bits.append(f"{label}：{entry['note']}")
    if bits:
        lines += ["", "对应联系人 —— " + "；".join(bits)]

    return _reply("\n".join(lines), conv=conv, app=app, refs=source,
                  actions=["查看入职清单", "我的带教老师是谁", "报到要带什么材料"],
                  onboarding_version=onboarding.GUIDE_VERSION)


def _handle_activities(db: Session, *, conv: str, app: str) -> DifyChatResult:
    rows = db.query(Activity).order_by(Activity.id.desc()).limit(5).all()
    if not rows:
        return _reply("目前还没有开放报名的活动，等活动上线我会在这里告诉你。",
                      conv=conv, app=app)
    lines = ["近期活动（按最新发布）："]
    for i, a in enumerate(rows, 1):
        cap = getattr(a, "capacity", None)
        enrolled = getattr(a, "enrolled_count", None)
        seat = f"，名额 {enrolled}/{cap}" if cap else ""
        lines.append(f"{i}. {getattr(a, 'title', '活动')}"
                     f"（{getattr(a, 'status', '-')}{seat}）")
    lines.append("")
    lines.append("告诉我要报的活动名称，我帮你锁定名额；满员会进候补队列。")
    return _reply("\n".join(lines), conv=conv, app=app,
                  refs=[{"doc": "运营活动", "section": "近期活动", "version": "2026-08"}],
                  actions=["报名活动"])


def _handle_score_query(message: str, role: str, *, conv: str, app: str) -> DifyChatResult:
    if role == "student":
        return _reply(
            "成绩查询在「学员中心 → 成绩」里，能看到每次模考的科目、得分与目标院校分数线对比。\n"
            "如果某一科明显偏低，我可以帮你把提分建议排个优先级。",
            conv=conv, app=app, actions=["查看我的成绩"],
        )
    return _reply(
        "要查学生成绩，用「查一下张三的成绩」这种句式最准（员工及以上角色）。\n"
        "如果要看整体分布，可以说「统计一下各科平均分」，我走受控查询模板返回结果。",
        conv=conv, app=app, actions=["查一下某个学生的成绩"],
    )


def _handle_handover(message: str, *, conv: str, app: str) -> DifyChatResult:
    return _reply(
        "已为你转接人工客服。工作时间内（9:00-21:00）顾问会在 5 分钟内接入；\n"
        "非工作时间可以留下手机号或微信，顾问会在次日 10:00 前联系你。",
        conv=conv, app=app,
        refs=[{"doc": "售后服务规范", "section": "人工转接", "version": "2026-08"}],
        actions=["留下联系方式"],
    )


def _handle_emotion(message: str, *, conv: str, app: str) -> DifyChatResult:
    return _reply(
        "听起来这段时间你挺不容易的，谢谢你愿意说出来。\n"
        "可以先做两件小事：把当下最难受的那件事写下来（不用管逻辑），"
        "再去喝点水、走两分钟。\n\n"
        "如果这种状态持续两周以上，或者出现伤害自己的念头，请立刻联系心理老师，"
        "回复「转人工」我帮你接入；紧急情况请拨打 12356 心理援助热线。",
        conv=conv, app=app, actions=["转人工", "预约心理老师"],
    )


def _permission_denied(message: str, intent, role: str, *, conv: str,
                       app: str) -> DifyChatResult:
    need = AGENT_TITLES.get(getattr(intent, "original_agent", "") or app, "该能力")
    return _reply(
        f"这件事我没法替你做：「{message}」属于「{need}」，"
        f"需要更高的角色权限，而你当前是「{_label(role)}」。\n\n"
        f"{CAN_DO_TEXT}\n\n"
        "如果觉得是账号权限配错了，回复「转人工」让顾问确认一下。",
        conv=conv, app=app, permission_denied=True,
    )


# --------------------------------------------------------------------------- #
# 寒暄与能力询问
#
# 「你好」这类输入不该被当成「答不出的问题」甩兜底话术 ——
# 第一句话就听到「我暂时没有可用的信息，不能凭空给你答复」，体验是灾难性的。
#
# ⚠️ 判定必须严格：只有**整句都是寒暄**才算寒暄。
# 「你好，请问雅思要考多少分」得照样去查知识库，
# 否则真实问题会被寒暄分支吞掉，那就从「答非所问」变成「不答」了。
# --------------------------------------------------------------------------- #
_GREETING_TOKENS = ("你好", "您好", "早上好", "中午好", "下午好", "晚上好",
                    "在吗", "在不在", "有人吗", "哈喽", "嗨", "hi", "hello",
                    "hey", "morning", "早")
_THANKS_TOKENS = ("谢谢", "多谢", "感谢", "谢了", "thanks", "thank", "辛苦")
_ACK_TOKENS = ("好的", "好嘞", "收到", "明白了", "了解", "可以", "是的", "行吧", "行",
               "好", "对", "ok", "okay", "嗯", "嗯嗯")
_BYE_TOKENS = ("再见", "拜拜", "bye", "回头聊", "先这样", "下次聊")
_TRAILING_FILLER = ("吗", "呀", "啊", "吧", "哈", "哦", "呐", "呢", "的了")
_SEPARATORS = " \t,，。.!！~～?？、:：;；"

# 组合寒暄（「好的谢谢」「你好，谢谢」）用并集兜底，
# 否则会漏到「答不出来」那条路上。
_PLEASANTRY_TOKENS = tuple(dict.fromkeys(
    _GREETING_TOKENS + _THANKS_TOKENS + _ACK_TOKENS + _BYE_TOKENS))

# 能力询问按短语整体匹配（这些说法本身就很明确，不会误伤）
_CAPABILITY_PHRASES = ("你能做什么", "你会做什么", "能做什么", "有什么功能",
                       "能帮我做什么", "能帮我干什么", "能干什么", "你会什么",
                       "你是谁", "你是什么", "介绍一下你", "自我介绍", "有什么能力",
                       "你可以帮我做什么", "帮我做什么")


def _consume_tokens(text: str, tokens: tuple) -> str:
    """把开头连续的寒暄词与标点逐个吃掉，返回剩余内容。"""
    rest = text.strip()
    ordered = sorted(tokens, key=len, reverse=True)   # 长词优先，避免「早上好」被「早」截断
    changed = True
    while rest and changed:
        changed = False
        lowered = rest.lower()
        for tok in ordered:
            if lowered.startswith(tok):
                rest = rest[len(tok):].lstrip(_SEPARATORS)
                changed = True
                break
    return rest


def _is_only(text: str, tokens: tuple) -> bool:
    """整句（允许重复、允许带标点）只由这些词组成。"""
    rest = _consume_tokens(text, tokens)
    if not rest:
        return True
    # 「你好吗」「谢谢哈」这类：剩下一个无实义的语气词也算
    return rest in _TRAILING_FILLER


def _strip_leading_pleasantry(text: str) -> str:
    """去掉开头的寒暄，留下真正要办的事。

    ⚠️ 这一步不是「好看」，是必须的：nl2sql 的参数抽取会在问句里找人名/关键词，
    而「好的」这种 2 字纯中文片段恰好会被当成客户名，拼进
    `WHERE l.name LIKE '%好的%'` —— 于是「好的，帮我查一下最近的意向客户跟进情况」
    会筛出 **0 条**，看起来像「库里没数据」，实际是问候语污染了参数。

    整句都是寒暄时返回原文（那种情况在 `_handle_smalltalk` 就返回了，走不到这里）。
    """
    return _consume_tokens(text, _PLEASANTRY_TOKENS) or text.strip()


def _handle_smalltalk(message: str, role: str, *, conv: str, app: str) -> Optional[DifyChatResult]:
    text = message.strip()
    if not text:
        return None

    if _is_only(text, _GREETING_TOKENS):
        return _reply(
            f"你好！我是留学机构的 AI 助手，当前以「{_label(role)}」身份为你服务。\n\n"
            "直接说你要办的事就行，我能在系统里查数据、起申请单、开售后工单。\n"
            "需要人工的话回复「转人工」。",
            conv=conv, app=app, actions=["转人工", "你能做什么"],
        )

    if any(phrase in text for phrase in _CAPABILITY_PHRASES):
        return _reply(
            "我能做这些事：\n\n" + CAN_DO_TEXT + "\n\n"
            "· 经营数据（员工及以上）：客户跟进记录、线索状态分布、待审批申请、工单情况\n\n"
            "举例：「帮我查一下最近的意向客户跟进情况」「我要请假 9 月 15 日 2 天」"
            "「我要投诉排课时间」。\n"
            "越出上面范围的问题（比如通用知识问答）我答不了，会直说，"
            "你可以回复「转人工」找顾问。",
            conv=conv, app=app, actions=["转人工", "我们的服务流程是什么"],
        )

    if _is_only(text, _THANKS_TOKENS):
        return _reply("不客气。还有别的要办的吗？比如查数据、起请假单、开售后工单都可以。",
                      conv=conv, app=app, actions=["你能做什么", "转人工"])

    if _is_only(text, _ACK_TOKENS):
        return _reply("好的，随时叫我。要查数据、办申请或转人工，直接说就行。",
                      conv=conv, app=app)

    if _is_only(text, _BYE_TOKENS):
        return _reply("再见！有需要随时回来找我。",
                      conv=conv, app=app)

    # 组合寒暄兜底（「好的谢谢」「你好，谢谢」）：
    # 只要整句能由寒暄词吃完，就不该被当成「答不出的问题」。
    if _is_only(text, _PLEASANTRY_TOKENS):
        return _reply(
            "不客气～有需要随时说：查经营数据、起请假/考务申请单、开售后工单，"
            "或者回复「转人工」找顾问。",
            conv=conv, app=app, actions=["你能做什么", "转人工"],
        )

    return None


def _handle_todo(message: str, role: str, db: Session, *,
                 subject: str, ref_id: Optional[int],
                 conv: str, app: str) -> DifyChatResult:
    """主动待办推送：**先「问」再「答」**，并把这一轮提醒落库留痕。

    需求原文的句式是「当系统检测到有待处理事项时，主动询问员工『有没有投诉反馈需要跟进？』，
    并直接反馈查询结果」——所以这里不能只给一个数字，要按类别把「问句 + 明细 + 处理入口」
    一起给出。`todo.digest_text()` 与前端横幅共用同一份口径。

    ⚠️ 副作用：这轮会把待办写进 `todo_push`（同一时间窗内幂等），
    管理层还会顺带触发「未触达心理预警」的自动触达 —— 这正是需求要的「立即触发预警」。
    """
    if role not in todo.STAFF_ROLES:
        return _reply(
            f"待办提醒是给员工用的功能，你当前是「{_label(role)}」。\n\n"
            f"{CAN_DO_TEXT}\n\n"
            "如果你要查自己的申请进度，直接说「我的请假申请到哪一步了」。",
            conv=conv, app=app, permission_denied=True,
        )

    todos = todo.collect(db, role)
    result = todo.push_due(db, subject=subject or f"role:{role}", role=role, ref_id=ref_id)
    pushed = [r for r in result["records"] if not r["duplicated"]]
    delivered = result["delivered_alerts"]
    db.flush()                                   # commit 由调用方统一做

    lines = [todo.digest_text(todos)]
    if pushed:
        lines += ["", f"（本轮已登记提醒："
                      + "、".join(f"{CATEGORY_LABEL[r['category']]}×{r['count']}" for r in pushed)
                      + "）"]
    else:
        lines += ["", "（这些提醒刚才已经推过一轮，就不再重复打扰你了）"]
    if delivered:
        lines += ["", f"⚠️ 顺带把 {len(delivered)} 条未触达的心理预警标记为已触达，"
                      f"处理人："
                      + "、".join(sorted({d["notified_to_name"] or "未指定" for d in delivered}))]

    return _reply(
        "\n".join(lines), conv=conv, app=app,
        refs=[{"doc": "待办规则", "section": "主动待办推送", "version": "内置"}],
        actions=["查看待办清单", "去运营中心处理", "设置提醒频率"],
        todo_count=sum(t["count"] for t in todos),
        todo_categories=[t["category"] for t in todos],
        pushed=len(pushed), alerts_delivered=len(delivered),
    )


CATEGORY_LABEL = {
    "approval": "待审批申请",
    "ticket": "待跟进工单",
    "followup": "到期客户跟进",
    "screening_review": "待复核研判",
    "mental_alert": "待触达心理预警",
}


def _handle_alert_digest(message: str, role: str, db: Session, *,
                         conv: str, app: str) -> DifyChatResult:
    """心理预警汇总 + 干预建议（REQ：辅助老师通过企业助手快速介入干预）。

    心理数据是敏感数据（`/api/v1/alerts` 也是 manager_only），
    所以这里必须自己卡一道权限 —— 意图路由层面 employee 对 enterprise_assistant
    本来是放行的，不能指望路由把这一层拦住。
    """
    if role not in todo.MANAGER_ROLES:
        return _reply(
            f"心理预警涉及学生敏感信息，只有管理层（{list(todo.MANAGER_ROLES)}）能看，"
            f"你当前是「{_label(role)}」。\n\n"
            "如果你注意到学生状态异常，正确做法是：直接把情况告诉带教老师或管理层，"
            "不要在学生群里讨论。回复「转人工」也可以帮你转达。",
            conv=conv, app=app, permission_denied=True, actions=["转人工"],
        )

    digest = todo.alert_digest(db)
    if not digest["total"]:
        return _reply("当前没有未关闭的心理预警，学生心理状态看起来是平稳的。",
                      conv=conv, app=app, alert_total=0)

    lines = [f"当前有 **{digest['total']}** 条未关闭的心理预警，"
             f"其中高危 **{digest['high_risk']}** 条、未触达 **{digest['undelivered']}** 条：", ""]
    for item in digest["items"][:5]:
        flag = "🔴 HIGH" if item["risk_level"] == "HIGH" else item["risk_level"]
        state = "已触达" if item["notified"] else "未触达"
        lines.append(f"· #{item['id']} {item['student_name']}（{flag} · {state}）")
        lines.append(f"    原因：{item['reason'][:70]}")
    lines += ["", "建议动作（按顺序）：",
              "1. 24 小时内单独联系学生，先听不评判，不在群里提及",
              "2. 3 个工作日内回填跟进记录；风险未降级就保持 FOLLOWING",
              "3. 出现自伤/极端言语立即升级：心理老师 + 家长 + 管理层同步"]
    if digest["undelivered"]:
        lines += ["", f"还有 {digest['undelivered']} 条没触达处理人，"
                      "我可以现在就替你触达（会记录触达对象与干预建议）。"]

    return _reply(
        "\n".join(lines), conv=conv, app=app,
        refs=[{"doc": "心理关怀处置规范", "section": "预警分级与介入", "version": "2026-08"}],
        actions=["立即触达未提醒预警", "查看预警详情", "更新跟进状态"],
        alert_total=digest["total"], alert_undelivered=digest["undelivered"],
    )


# --------------------------------------------------------------------------- #
# 总入口
# --------------------------------------------------------------------------- #
def respond(intent, message: str, role: str, db: Session, *,
            subject: str = "", ref_id: Optional[int] = None) -> DifyChatResult:
    """mock 模式下的对话编排。返回结构与会话链路里的 DifyChatResult 一致。"""
    app = intent.agent
    conv = _conv(message)
    kind = intent.intent

    # 寒暄 / 能力询问放在最前面：这类输入压根不该走「问题答不出」的兜底。
    # 对非寒暄内容本函数返回 None，不影响下面的正常分派。
    smalltalk = _handle_smalltalk(message, role, conv=conv, app=app)
    if smalltalk is not None:
        return smalltalk

    # 去掉开头的寒暄再解析（「好的，帮我查一下…」不能把「好的」当成客户名）
    clean = _strip_leading_pleasantry(message)

    if kind == "after_sales":
        return _handle_after_sales(message, role, db, subject=subject, ref_id=ref_id,
                                   conv=conv, app=app)
    if kind == "leave_apply":
        return _handle_leave_apply(clean, role, db, subject=subject, ref_id=ref_id,
                                   conv=conv, app=app)
    # 入职指引必须在 enterprise_assistant 兜底之前判断：
    # 下面那条 `app == APP_ENTERPRISE` 会把 enterprise 侧的所有输入都吞成取数查询。
    if kind == "onboarding":
        if role not in ONBOARDING_ROLES:
            return _reply(
                f"《新人入职指引》是公司内部文档，只有员工及以上角色能看，"
                f"你当前是「{_label(role)}」。\n"
                "如果你是刚入职的同事，请用公司分配的账号登录；"
                "如果是外部咨询，回复「转人工」我帮你接顾问。",
                conv=conv, app=app, permission_denied=True, actions=["转人工"],
            )
        return _handle_onboarding(message, role, db, ref_id=ref_id, conv=conv, app=app)
    # 这两条也必须排在 enterprise_assistant 兜底之前（理由同上）。
    if kind == "todo_push":
        return _handle_todo(message, role, db, subject=subject, ref_id=ref_id,
                            conv=conv, app=app)
    if kind == "alert_digest":
        return _handle_alert_digest(message, role, db, conv=conv, app=app)
    if kind == "nl2sql_query" or app == APP_ENTERPRISE:
        return _handle_data(clean, role, db, conv=conv, app=app)
    if kind == "report_generate":
        return _handle_report(message, role, conv=conv, app=app)
    if kind == "daily_report":
        return _handle_daily_report(message, role, conv=conv, app=app)
    if kind == "activity_enroll":
        return _handle_activities(db, conv=conv, app=app)
    if kind == "score_query":
        return _handle_score_query(message, role, conv=conv, app=app)
    if kind == "handover":
        return _handle_handover(message, conv=conv, app=app)
    if kind == "emotion_support":
        return _handle_emotion(message, conv=conv, app=app)

    # 知识类：语料命中就答（不管角色 —— 机构政策是公共信息，不该被权限拦）
    hit = mock_kb_answer(app, message)
    if hit is not None:
        return hit

    if intent.permission_denied:
        return _permission_denied(message, intent, role, conv=conv, app=app)

    return _reply(MOCK_NO_RECALL_TEXT, conv=conv, app=app, no_recall=True)
