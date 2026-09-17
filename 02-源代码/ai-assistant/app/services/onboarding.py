"""新人入职指引（M3-07）：内容资产 + 查询组装。

## 为什么单独成层

入职指引是**要持续维护的知识资产**，不是写死在接口里的文案。所以：

- 内容以版本化常量维护（`GUIDE_VERSION` / `GUIDE_UPDATED_AT` / `GUIDE_TAGS`），
  `scripts/seed.py` 会把它同步登记进 `knowledge_doc`（`category="NEO"`），
  于是它既能被后台检索、改版、下线，也能进 Dify 数据集被对话召回；
- 接口层（`app/api/v1/org.py`）只做查询与组装，一句文案都不写；
- 关键联系人**从 `employee` 表现算**，不写死姓名 —— 组织变动后不用改代码。

## 内容维护约定

改内容时请同时改 `GUIDE_VERSION`（例：`V2026.09` → `V2026.10`），
否则 `knowledge_doc` 的版本号会与正文脱节，检索时会给出过期引用。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import Employee

# --------------------------------------------------------------------------- #
# 元信息（登记进 knowledge_doc 用；改正文务必同步改版本号）
# --------------------------------------------------------------------------- #
GUIDE_TITLE = "新人入职指引"
GUIDE_CATEGORY = "NEO"                  # knowledge_doc.category
GUIDE_VERSION = "V2026.09"
GUIDE_UPDATED_AT = "2026-09-15"
GUIDE_OWNER = "人力行政部"
GUIDE_TAGS = ["新人入职", "SOP", "报到", "账号与权限", "试用期"]

# 适用范围：写清楚「给谁看、什么时候看」，避免被当成公司通用制度
GUIDE_SCOPE = "全体新入职员工（含顾问、带教老师、职能岗）；入职前后 90 天内适用。"


# --------------------------------------------------------------------------- #
# 阶段化步骤
# --------------------------------------------------------------------------- #
STAGES: List[Dict[str, Any]] = [
    {
        "key": "D0",
        "name": "报到前",
        "window": "入职前 1—3 天",
        "goal": "把材料与设备准备齐，避免报到当天跑第二趟。",
        "items": [
            {"key": "D0-01", "title": "提交入职材料",
             "detail": "身份证复印件、学历/学位证明、离职证明（如适用）、银行卡、一寸照片 2 张。"
                       "原件核验后当场归还，扫描件由人力行政部归档。",
             "owner": "人力行政部", "channel": "邮件至 hr@example.com"},
            {"key": "D0-02", "title": "签署劳动合同与保密协议",
             "detail": "合同一式两份，试用期与转正后薪酬以合同为准；"
                       "保密协议含学生信息与客户资料条款，签完请自留一份。",
             "owner": "人力行政部", "channel": "现场签署"},
            {"key": "D0-03", "title": "确认办公设备与工位",
             "detail": "笔记本、工牌、门禁卡在报到当天领取；"
                       "如需外勤设备（录音笔等）请在报到前一天告知直属上级。",
             "owner": "直属上级", "channel": "报到当天现场领取"},
            {"key": "D0-04", "title": "提前加好内部沟通群",
             "detail": "加入部门群与全员群，群内备注格式「部门-姓名」。"
                       "业务通知以群公告为准，不要只依赖私聊。",
             "owner": "直属上级", "channel": "由直属上级拉群"},
        ],
    },
    {
        "key": "D1",
        "name": "入职第一天",
        "window": "报到当日",
        "goal": "账号能登、系统能进、知道找谁问。",
        "items": [
            {"key": "D1-01", "title": "领取并激活系统账号",
             "detail": "账号由系统管理员统一开通，初始密码首次登录后必须修改。"
                       "登录地址与账号密码通过企业微信/邮件单独发送，不要转发给他人。",
             "owner": "系统管理员", "channel": "对接 IT / 系统管理员"},
            {"key": "D1-02", "title": "熟悉 AI 智能助手系统",
             "detail": "统一对话入口可问公司信息、政策、SOP；"
                       "「组织与指南」页能查组织架构、员工花名册与本入职指引；"
                       "写操作（建客户、起申请单、开工单）都会留审计日志，按规范操作。",
             "owner": "直属上级", "channel": "系统「对话助手」「组织与指南」"},
            {"key": "D1-03", "title": "明确汇报关系与岗位职责",
             "detail": "确认直属上级、部门负责人、带教老师；"
                       "在「组织与指南」的组织架构里能直接看到自己的汇报线。",
             "owner": "直属上级", "channel": "组织架构页 / 入职面谈"},
            {"key": "D1-04", "title": "提交第一份日报",
             "detail": "日报支持文字或语音口述，当天提交；同一人同一天重复提交会覆盖而非新增记录。"
                       "格式：今天做了什么 + 遇到的问题 + 明天计划。",
             "owner": "本人", "channel": "系统「运营中心 → 日报」"},
            {"key": "D1-05", "title": "信息与数据安全告知",
             "detail": "学生身份证、家庭信息、心理数据属敏感信息，仅限授权岗位查看，"
                       "严禁外发到私人设备或第三方平台；违规按保密协议处理。",
             "owner": "人力行政部", "channel": "入职面谈签收"},
        ],
    },
    {
        "key": "W1",
        "name": "第一周",
        "window": "入职第 1 周",
        "goal": "把业务流程跑通一遍，敢独立接第一件事。",
        "items": [
            {"key": "W1-01", "title": "业务与产品线入门",
             "detail": "了解公司主营的申请产品（如英国硕士直申、澳洲八大保录）的"
                       "目标客群、申请条件、服务周期与费用区间。",
             "owner": "带教老师", "channel": "内部培训 + 知识库"},
            {"key": "W1-02", "title": "跟岗一次完整业务闭环",
             "detail": "顾问岗：跟进一条意向客户 → 提交材料研判 → 出选校方案；"
                       "教务岗：学生请假审批 → 工单处理 → 成绩录入。以旁观为主。",
             "owner": "带教老师", "channel": "现场跟岗"},
            {"key": "W1-03", "title": "熟悉常用系统操作",
             "detail": "客户录入与查重（手机号重复会直接拦下）、跟进记录（支持幂等键防重复）、"
                       "材料研判、工单流转、请假审批。",
             "owner": "带教老师", "channel": "系统各业务视图"},
            {"key": "W1-04", "title": "确认试用期考核目标",
             "detail": "与直属上级确认 3 个月的量化目标与过程要求，书面留痕；"
                       "中途调整目标需重新确认。",
             "owner": "直属上级", "channel": "试用期面谈"},
        ],
    },
    {
        "key": "M1",
        "name": "第一个月",
        "window": "入职 30 天内",
        "goal": "独立承担岗位职责，流程上不再需要人盯。",
        "items": [
            {"key": "M1-01", "title": "独立完成本职业务",
             "detail": "顾问岗独立跟单并产出方案；教务岗独立处理审批与工单；"
                       "职能岗独立交付本月常规事务。",
             "owner": "本人", "channel": "日常业务"},
            {"key": "M1-02", "title": "只用知识库口径对外沟通",
             "detail": "价格、退费、承诺类话术必须走知识库口径，不做超出政策的承诺；"
                       "政策类信息注意时效，过期内容会被标注。",
             "owner": "本人", "channel": "知识库 / 对话助手"},
            {"key": "M1-03", "title": "完成一次月度复盘",
             "detail": "写清：目标完成度、遇到的问题、改进动作、需要的支持。"
                       "复盘沉进知识库可被团队复用。",
             "owner": "直属上级", "channel": "月度复盘会"},
        ],
    },
    {
        "key": "M3",
        "name": "转正前后",
        "window": "入职 90 天前后",
        "goal": "完成转正评估，把经验沉淀回知识库。",
        "items": [
            {"key": "M3-01", "title": "提交转正申请与自评",
             "detail": "按考核目标逐项自评，附过程材料（客户记录、工单、报表）。",
             "owner": "本人", "channel": "提交直属上级 → 人力行政部"},
            {"key": "M3-02", "title": "转正答辩 / 面谈",
             "detail": "由直属上级与部门负责人共同评估；"
                       "评估维度：业务结果、流程合规、协作与主动性。",
             "owner": "直属上级", "channel": "转正面谈"},
            {"key": "M3-03", "title": "沉淀一份可复用材料",
             "detail": "把试用期踩过的坑、总结出的方法整理成 SOP / FAQ / 案例，"
                       "提交给知识库维护人归档。",
             "owner": "本人", "channel": "知识库（找知识库维护人归档）"},
        ],
    },
]

# 显式声明未覆盖的范围 —— 避免新人对「指引没写」产生误解
GUIDE_LIMITS = [
    "薪酬、社保公积金、个税等口径以劳动合同与人力行政部解释为准，本指引不重复规定。",
    "各岗位的详细业务流程见对应 SOP 文档，本指引只覆盖「入职前后要做什么」。",
    "超过 90 天后的日常问题，请走部门内部流程或直接问 AI 助手 / 直属上级。",
]


# --------------------------------------------------------------------------- #
# 常见问题
# --------------------------------------------------------------------------- #
FAQ: List[Dict[str, Any]] = [
    {"id": "F-01", "q": "报到当天要带什么？",
     "a": "身份证原件、学历学位证原件、离职证明（如适用）、银行卡、一寸照片 2 张。"
          "原件现场核验后归还，扫描件交人力行政部归档。",
     "tags": ["报到", "材料"], "owner": "人力行政部"},
    {"id": "F-02", "q": "系统账号什么时候开通？密码忘了怎么办？",
     "a": "报到当天由系统管理员开通，账号密码单独发送给你本人。"
          "首次登录必须改密码；忘记密码请联系系统管理员重置，不要找人代登。",
     "tags": ["账号", "权限", "IT"], "owner": "系统管理员"},
    {"id": "F-03", "q": "日报必须每天写吗？可以语音吗？",
     "a": "在职期间每个工作日提交。支持文字或语音口述，语音会自动转写并抽成结构化字段，"
          "确认无误再提交。同一人同一天重复提交是覆盖，不会产生两条记录。",
     "tags": ["日报", "制度"], "owner": "直属上级"},
    {"id": "F-04", "q": "请假怎么申请？多久能批？",
     "a": "学员端在「学员中心 → 我的申请」提交，也可以在对话里直接说"
          "「我要请假 9 月 15 日 2 天，原因看病」，系统会起单。"
          "带教老师审批后结果推送；已审批的单子不能重复审批。"
          "员工本人请假按公司考勤制度走，不走学员请假流程。",
     "tags": ["请假", "审批"], "owner": "带教老师 / 直属上级"},
    {"id": "F-05", "q": "我的汇报关系在哪里看？",
     "a": "「组织与指南」页的组织架构树按汇报线展开，能看到自己的直属上级、"
          "同部门同事和部门负责人；员工花名册支持按部门和姓名检索。",
     "tags": ["组织架构", "汇报关系"], "owner": "人力行政部"},
    {"id": "F-06", "q": "带教老师是谁？怎么安排？",
     "a": "由部门负责人指定同岗位的资深同事担任带教老师，一般在你所在部门内。"
          "带教内容包括业务入门、跟岗、系统操作与试用期目标对齐。",
     "tags": ["带教", "培训"], "owner": "部门负责人"},
    {"id": "F-07", "q": "客户和学生的资料能外发吗？",
     "a": "不能。学生身份证、家庭信息、心理数据属敏感信息，仅限授权岗位在系统内查看，"
          "严禁导出到私人设备或通过个人微信/网盘发送。违规按保密协议处理。",
     "tags": ["数据安全", "合规", "保密"],
     "owner": "人力行政部 / 系统管理员"},
    {"id": "F-08", "q": "对外报价或承诺可以自己定吗？",
     "a": "不可以。价格、退费、录取承诺类话术必须使用知识库里的标准口径，"
          "不做超出政策的承诺。拿不准就先问直属上级，或让 AI 助手检索知识库。",
     "tags": ["合规", "话术", "知识库"], "owner": "直属上级"},
    {"id": "F-09", "q": "试用期考核看什么？",
     "a": "看三项：业务结果（目标完成度）、流程合规（系统操作与留痕是否规范）、"
          "协作与主动性。第一个月内会与直属上级书面确认目标。",
     "tags": ["试用期", "考核", "转正"], "owner": "直属上级"},
    {"id": "F-10", "q": "工作中遇到系统问题或程序 bug 找谁？",
     "a": "先看「系统状态」页确认后端与依赖是否正常；确认是系统问题就找系统管理员，"
          "并提供操作时间与页面提示，方便按审计日志定位。",
     "tags": ["IT", "系统", "报障"], "owner": "系统管理员"},
    {"id": "F-11", "q": "公司有哪些政策/流程文档可以自己看？",
     "a": "知识库里按分类归档：公司信息与服务政策、签证材料清单、岗位 SOP、常见问题。"
          "可以直接在对话里问 AI 助手，回答会标注引用出处。",
     "tags": ["知识库", "政策"], "owner": "知识库维护人"},
    {"id": "F-12", "q": "转正需要准备什么？",
     "a": "提交转正申请与自评（按考核目标逐项写，附过程材料），"
          "由直属上级与部门负责人共同评估。同时建议沉淀一份可复用材料进知识库。",
     "tags": ["转正", "评估"], "owner": "人力行政部"},
]


# --------------------------------------------------------------------------- #
# 关键联系人（声明式规则，姓名与部门**从 employee 表现算**，不写死）
# --------------------------------------------------------------------------- #
CONTACT_RULES: List[Dict[str, Any]] = [
    {"key": "manager", "label": "直属上级", "order": 1,
     "desc": "日常任务分派、目标对齐、请假与异常第一责任人。"},
    {"key": "dept_head", "label": "部门负责人", "order": 2,
     "desc": "跨部门协调、资源支持、转正评估。"},
    {"key": "buddy", "label": "带教老师", "order": 3,
     "desc": "业务入门、跟岗、系统操作答疑（同部门同岗位资深同事）。"},
    {"key": "hr", "label": "人力行政对接", "order": 4,
     "desc": "合同、材料、社保公积金、考勤与转正流程。"},
    {"key": "it", "label": "系统管理员", "order": 5,
     "desc": "账号开通与重置、权限调整、系统报障。"},
]

# 从 title / department 里认出职能对接人的关键词
_HR_KEYS = ("人力", "人事", "行政", "HR")
_IT_KEYS = ("系统管理", "运维", "信息技术", "IT")


def _brief(emp: Optional[Employee]) -> Optional[Dict[str, Any]]:
    if emp is None:
        return None
    return {"id": emp.id, "emp_no": emp.emp_no, "name": emp.name,
            "department": emp.department, "title": emp.title,
            "phone": emp.phone, "email": emp.email, "biz_role": emp.biz_role}


def _match_func_role(employees: List[Employee], keys: tuple) -> Optional[Employee]:
    for emp in employees:
        hay = f"{emp.title or ''}{emp.department or ''}"
        if any(k.lower() in hay.lower() for k in keys):
            return emp
    return None


def resolve_contacts(db: Session, employee_id: Optional[int] = None) -> Dict[str, Any]:
    """按规则解析关键联系人。

    没有配置的岗位**如实返回 None 并给出说明**，不编造姓名 ——
    新人拿到一个假名字比拿到「暂未配置」更糟。
    """
    employees = (db.query(Employee)
                 .filter(Employee.status == "ACTIVE")
                 .order_by(Employee.id).all())
    by_id = {e.id: e for e in employees}

    resolved: Dict[str, Optional[Dict[str, Any]]] = {}
    notes: Dict[str, str] = {}

    target = by_id.get(employee_id) if employee_id else None
    if employee_id and target is None:
        notes["_target"] = f"员工 {employee_id} 不存在或已停用，以下按公司级默认给出。"

    manager = by_id.get(target.manager_id) if (target and target.manager_id) else None
    if target and manager is None:
        notes["manager"] = "系统里还没有登记该员工的直属上级，请让人力行政部补全汇报关系。"
    resolved["manager"] = _brief(manager)

    if target:
        dept = target.department
        mate = next((e for e in employees
                     if e.department == dept and e.id != target.id
                     and e.biz_role == "teacher"), None)
        resolved["buddy"] = _brief(mate)
        if mate is None:
            notes["buddy"] = "本部门暂未登记带教老师（biz_role=teacher），可先由直属上级代带。"
    else:
        # 公司级默认：找 biz_role=teacher 的第一位
        resolved["buddy"] = _brief(next((e for e in employees
                                         if e.biz_role == "teacher"), None))

    if target:
        dept = target.department
        head = next((e for e in employees
                     if e.department == dept and e.biz_role == "manager"), None)
    else:
        head = next((e for e in employees if e.biz_role == "manager"), None)
    resolved["dept_head"] = _brief(head)
    if head is None:
        notes["dept_head"] = "未找到该部门的负责人记录（biz_role=manager）。"

    hr = _match_func_role(employees, _HR_KEYS)
    resolved["hr"] = _brief(hr)
    if hr is None:
        notes["hr"] = "暂未配置人力行政对接人，入职材料与合同事项请先找直属上级转达。"

    it = _match_func_role(employees, _IT_KEYS)
    resolved["it"] = _brief(it)
    if it is None:
        notes["it"] = "暂未配置系统管理员，账号与权限问题请找直属上级，由其转交运维。"

    items = []
    for rule in sorted(CONTACT_RULES, key=lambda r: r["order"]):
        entry = dict(rule)
        entry["contact"] = resolved.get(rule["key"])
        entry["note"] = notes.get(rule["key"], "")
        items.append(entry)

    return {"items": items, "notes": notes,
            "scope": f"scope={target.name}（{target.department}）" if target else "公司级默认"}


# --------------------------------------------------------------------------- #
# 查询组装
# --------------------------------------------------------------------------- #
_PUNCT = " \t\r\n，。！？、；：（）【】《》“”‘’…—·,.!?;:()[]{}<>\"'-_/\\|"
_FAQ_THRESHOLD = 0.30
# 泛问时的兜底关键词（命中就返回阶段目录，而不是「没查到」）
_GENERIC_KEYS = ("入职", "新人", "指引", "报到", "带教", "试用期", "转正", "新员工")


def _normalize(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch not in _PUNCT)


def _bigrams(text: str) -> set:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _score(kw_norm: str, kw_grams: set, text: Optional[str]) -> float:
    """归一化子串命中算 1.0，否则用二元组 Dice 系数（0~1）。"""
    norm = _normalize(text or "")
    if not norm:
        return 0.0
    if kw_norm and kw_norm in norm:
        return 1.0
    grams = _bigrams(norm)
    if not grams or not kw_grams:
        return 0.0
    return 2 * len(kw_grams & grams) / (len(kw_grams) + len(grams))


def guide() -> Dict[str, Any]:
    """返回完整指引（前端一次拿全）。"""
    return {
        "title": GUIDE_TITLE,
        "category": GUIDE_CATEGORY,
        "version": GUIDE_VERSION,
        "updated_at": GUIDE_UPDATED_AT,
        "owner": GUIDE_OWNER,
        "tags": GUIDE_TAGS,
        "scope": GUIDE_SCOPE,
        "limits": GUIDE_LIMITS,
        "stages": STAGES,
        "faq": FAQ,
        "stats": {"stage_count": len(STAGES),
                  "item_count": sum(len(s["items"]) for s in STAGES),
                  "faq_count": len(FAQ)},
    }


def checklist(stage: Optional[str] = None) -> Dict[str, Any]:
    """按阶段返回待办清单；不传则返回全部阶段。"""
    if stage:
        wanted = stage.strip().upper()
        stages = [s for s in STAGES if s["key"].upper() == wanted]
    else:
        stages = STAGES
    return {
        "version": GUIDE_VERSION,
        "stages": [{"key": s["key"], "name": s["name"], "window": s["window"],
                    "goal": s["goal"],
                    "items": [{"key": i["key"], "title": i["title"],
                               "owner": i["owner"], "channel": i["channel"]}
                              for i in s["items"]]}
                   for s in stages],
        "total": sum(len(s["items"]) for s in stages),
    }


def search_faq(keyword: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    """新人常见问题检索。

    匹配策略（由强到弱）：
    1. 归一化后子串命中 → 直接算满分（「日报」这类关键词走这条）；
    2. 否则算 **字符二元组 Dice 相似度** —— 员工是整句提问的
       （「入职指引：报到要带什么？」），子串匹配必然落空，需要模糊匹配兜住。

    低于 `_FAQ_THRESHOLD` 一律不返回，宁可不答也别答错。
    """
    kw = (keyword or "").strip()
    if not kw:
        items = [{**f, "score": 0.0} for f in FAQ]
        return {"keyword": "", "total": len(items), "items": items[:max(1, limit)]}

    kw_norm = _normalize(kw)
    kw_grams = _bigrams(kw_norm)
    scored = []
    for f in FAQ:
        score = max(_score(kw_norm, kw_grams, f["q"]),
                    _score(kw_norm, kw_grams, f["a"]) * 0.9,   # 正文命中略降权
                    max((_score(kw_norm, kw_grams, t) for t in f["tags"]), default=0.0))
        if score >= _FAQ_THRESHOLD:
            scored.append({**f, "score": round(score, 3)})

    scored.sort(key=lambda x: (-x["score"], x["id"]))
    return {"keyword": kw, "total": len(scored), "items": scored[:max(1, limit)]}


def answer(question: str, db: Session, employee_id: Optional[int] = None) -> Dict[str, Any]:
    """对话侧用：从入职指引里挑出最相关的一条回答。"""
    hit = search_faq(question)
    if hit["total"]:
        best = hit["items"][0]
        return {"matched": True, "source": "FAQ", "faq_id": best["id"],
                "answer": best["a"], "question": best["q"],
                "owner": best["owner"], "tags": best["tags"],
                "score": best["score"]}

    text = question or ""
    for s in STAGES:
        if s["name"] in text or s["key"].lower() in text.lower():
            lines = [f"{s['name']}（{s['window']}）要做的 {len(s['items'])} 件事："]
            lines += [f"{i + 1}. {it['title']} —— {it['detail']}"
                      for i, it in enumerate(s["items"])]
            return {"matched": True, "source": "STAGE", "stage": s["key"],
                    "answer": "\n".join(lines), "owner": s["items"][0]["owner"],
                    "tags": GUIDE_TAGS}

    # 泛问「入职指引」：给阶段目录，比回一句「没查到」有用得多
    if any(k in text for k in _GENERIC_KEYS):
        lines = [f"《{GUIDE_TITLE}》{GUIDE_VERSION}"
                 f"（责任人：{GUIDE_OWNER}，更新 {GUIDE_UPDATED_AT}）共 {len(STAGES)} 个阶段："]
        lines += [f"· {s['name']}（{s['window']}）：{s['goal']}" for s in STAGES]
        lines += ["",
                  "要清单就说阶段名，例如「D1 要做哪些事」；"
                  "要找人就说「我的带教老师是谁」；也可以直接问具体问题（如「报到要带什么」）。"]
        return {"matched": True, "source": "GUIDE", "answer": "\n".join(lines),
                "owner": GUIDE_OWNER, "tags": GUIDE_TAGS, "stage": None}

    return {"matched": False, "source": None, "answer": "",
            "owner": GUIDE_OWNER, "tags": GUIDE_TAGS,
            "hint": "指引里没写这件事，建议先看 guide 的 limits 部分，或直接问直属上级。"}
