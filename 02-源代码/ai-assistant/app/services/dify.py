"""Dify 客户端：统一的对话 / 工作流 / 流式 / 语音转写入口。

设计要点
--------
1. **应用隔离**：每个业务 Agent 对应一个 Dify 应用，各用各的 API Key，
   便于单独灰度、单独限流、单独降级。
2. **mock 模式**：`DIFY_MODE=mock` 时完全不联网，用内置确定性应答，
   让本地开发与单元测试不依赖 Dify 服务。
3. **降级**：`live` 模式下 Dify 不可用（超时/5xx/网络错误）时，
   自动回落到 mock 应答并标记 `degraded=True`，保证「不出现无响应」。
"""
from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx

from ..config import settings
from ..core import UpstreamError

logger = logging.getLogger(__name__)

# Dify 应用名（与 6 个 Agent 对应）
APP_ROUTER = "router"
APP_CUSTOMER_SERVICE = "customer_service"
APP_STUDENT_HELPER = "student_helper"
APP_ENTERPRISE = "enterprise_assistant"
APP_MENTAL_CARE = "mental_care"
APP_SCREENER = "screener"
APP_REPORTER = "reporter"


# --------------------------------------------------------------------------- #
# 会话 ID 不属于本应用
# --------------------------------------------------------------------------- #
# Dify 的 `conversation_id` 是**按应用隔离**的，而前端只存一个全局会话 ID：
# 换一类问题（客服 → 学员助手）时会把上一个应用的会话递进来，Dify 有两种回法：
#
#   404 {"code":"not_found","message":"Conversation Not Exists. ..."}
#   400 {"errors":{"conversation_id":"... is not a valid uuid."}}
#
# 这类错误**不能走降级**。降级会把一段与问题无关的 mock 套话返回给用户，
# 而 `degraded=True` 在 UI 上只是个小标记，很容易被当成「已经接上 Dify 了」。
# 换个应用本来就不存在共同上下文，正确处理是**丢掉这个会话 ID 重开一次**。
_CONVERSATION_MISSING_HINTS = ("conversation not exists", "not a valid uuid")


def _conversation_missing(body: Any) -> bool:
    """响应体是不是在说「这个 conversation_id 在本应用里不存在」。"""
    try:
        text = json.dumps(body, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        text = str(body).lower()
    return any(hint in text for hint in _CONVERSATION_MISSING_HINTS)


def _json_or_none(resp: httpx.Response) -> Any:
    """拿响应体 JSON；不是 JSON 就回 None（判定用，不抛）。"""
    try:
        return resp.json()
    except Exception:                      # noqa: BLE001 - 非 JSON 一律当「不是」
        return None


def _conversation_is_stale(resp: httpx.Response) -> bool:
    """响应是不是「会话 ID 不属于本应用」（要求响应体已读出）。"""
    if resp.status_code not in (400, 404):
        return False
    return _conversation_missing(_json_or_none(resp))


def _drop_conversation(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in payload.items() if k != "conversation_id"}


@dataclass
class DifyChatResult:
    answer: str
    conversation_id: str = ""
    message_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    references: List[Dict[str, Any]] = field(default_factory=list)
    suggest_actions: List[str] = field(default_factory=list)
    degraded: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# mock 应答语料
#
# `DIFY_MODE=mock` 时完全不联网，用内置确定性应答，让本地开发与单元测试
# 不依赖 Dify 服务。命中规则见 `_match_kb()`：**最长关键词优先**。
#
# 这层的定位要说清楚：它是「离线语料库」，不是「万能问答」。
# 真正需要按路由分派、需要真查库的问题走 `services.mock_agent`；
# 开放域自由问答只有接上真实 Dify（DIFY_MODE=live）才有。
# --------------------------------------------------------------------------- #
_MOCK_KB: List[Tuple[Tuple[str, ...], str, List[Dict[str, Any]], List[str]]] = [
    # ---------- 申请与材料 ----------
    (
        ("材料", "准备什么", "需要什么", "申请流程"),
        "本科/硕士申请的核心材料清单：\n"
        "1）学历证明与完整成绩单（中英文对照、学校盖章）；\n"
        "2）语言成绩（IELTS / TOEFL / PTE 等）；\n"
        "3）个人陈述与推荐信（硕士按院校要求 1-2 封）；\n"
        "4）护照首页、资金证明与在读/毕业证明。\n"
        "不同国家与院校要求略有差异，建议先做一次免费背景评估再定清单。",
        [{"doc": "申请服务手册", "section": "材料清单", "version": "2026-08"}],
        ["预约背景评估", "查看院校要求"],
    ),
    (
        # ⚠️ 这里不能再挂裸「流程」：它会和「申请流程」抢词，
        # 也会把「你们的服务流程是什么」答成材料清单。只在具体组合上匹配。
        ("服务流程", "服务内容", "服务范围", "办理流程", "服务包含", "怎么合作"),
        "整体服务流程分六步：\n"
        "1）免费咨询：确认目标国家/专业与预算，约 30 分钟；\n"
        "2）背景评估：出「冲/稳/保」三档选校清单；\n"
        "3）签约与规划：定服务项目，拿到个人时间轴；\n"
        "4）材料与文书：顾问梳理素材 → 文书两轮批注定稿；\n"
        "5）递交与跟进：递申请、补充材料、跟 offer；\n"
        "6）签证与行前：换 CAS/I-20、办签证、订住宿、行前指导。\n"
        "每一步都会在系统里留节点，进度可以在「学员中心」看到。",
        [{"doc": "申请服务手册", "section": "服务流程", "version": "2026-08"}],
        ["预约免费咨询", "查看服务项目"],
    ),
    (
        ("文书", "个人陈述", "推荐信", "ps", "简历", "cv"),
        "文书材料一般包含：个人陈述（PS）、简历（CV）、1-2 封推荐信，"
        "部分专业还要 writing sample 或作品集。\n"
        "我们的流程是：顾问梳理素材 → 学生出初稿 → 文书老师逐段批注 → 定稿并做查重。"
        "每份文书至少两轮修改，改到满意为止。",
        [{"doc": "申请服务手册", "section": "文书服务", "version": "2026-08"}],
        ["预约文书老师", "查看文书样例"],
    ),
    (
        ("签证", "cas", "i-20", "存款证明", "面签", "递签"),
        "签证流程：拿到无条件 offer 后换 CAS / I-20 → 准备存款证明（一般需存满 28 天）"
        "→ 填表缴费 → 预约面签或递签。\n"
        "我们提供签证材料清单核对与面签模拟，递签后一般 2-4 周出结果。",
        [{"doc": "签证服务手册", "section": "办理流程", "version": "2026-07"}],
        ["核对签证材料", "预约面签模拟"],
    ),
    (
        ("背景评估", "评估", "我能申", "能申请", "条件够"),
        "背景评估看四项：院校背景、均分/GPA、语言成绩、软性经历（实习/科研/竞赛）。\n"
        "你把这几项发我，或者预约一次 30 分钟的免费评估，顾问会给出「冲/稳/保」三档选校建议。",
        [{"doc": "申请服务手册", "section": "背景评估", "version": "2026-08"}],
        ["预约背景评估", "上传成绩单"],
    ),
    # ---------- 机构信息 ----------
    (
        ("校区", "地址", "在哪", "怎么走", "位置", "门店"),
        "我们在成都设有两个服务中心：\n"
        "1）武侯区领事馆路服务中心（周一至周日 9:00-18:00）；\n"
        "2）高新区天府三街申请中心（周一至周五 9:30-18:30）。\n"
        "建议提前 1 天预约到访，顾问会准备院校资料与申请时间轴。",
        [{"doc": "公司信息", "section": "校区分布", "version": "2026-08"}],
        ["预约到访", "查看全部校区"],
    ),
    (
        ("工作时间", "营业时间", "几点", "电话", "联系方式", "怎么联系", "微信", "公众号"),
        "客服在线时间：周一至周日 9:00-21:00（节假日不休）。\n"
        "咨询热线 400-000-0000；也可以直接在这里留言，顾问会在 30 分钟内回复。"
        "非工作时间留言会在次日 10:00 前统一回复。",
        [{"doc": "公司信息", "section": "服务时间", "version": "2026-08"}],
        ["留言给顾问", "转人工"],
    ),
    (
        ("费用", "多少钱", "学费", "价格", "收费", "退费", "退款", "定金", "押金"),
        "英澳方向的申请服务费按项目分档，本科/硕士标准申请服务费区间为 "
        "1.2 万 ~ 3.8 万元，具体以签约项目为准；第三方费用（学校申请费、"
        "签证费、翻译公证费）据实另计。退费规则见合同附件二。",
        [{"doc": "服务与价格", "section": "收费标准", "version": "2026-07"}],
        ["索取报价单", "预约顾问"],
    ),
    (
        ("合同", "保录取", "保证录取", "包过", "承诺"),
        "我们提供的是申请服务，不承诺录取结果 —— 任何「保录取」都是不合规宣传。\n"
        "合同里会写清服务范围、交付节点与退费条款；如果对条款有疑问，"
        "签约前可以让顾问逐条解释，也支持带上家长一起过一遍。",
        [{"doc": "服务与价格", "section": "合同说明", "version": "2026-07"}],
        ["查看合同样例", "预约顾问"],
    ),
    # ---------- 选校与专业 ----------
    (
        ("目的地", "去哪", "哪个国家", "选国家", "英澳", "澳洲", "英国", "美国", "加拿大",
         "中国香港", "香港", "新加坡", "日本"),
        "主流目的地对比（按申请难度与预算）：\n"
        "· 英国：1 年制硕士，看重均分与院校背景，G5 对文书要求高；\n"
        "· 澳洲：八大名校接受度宽，可用语言班衔接，移民专业有额外加分；\n"
        "· 中国香港 / 新加坡：离家近、性价比高，商科与计算机竞争激烈；\n"
        "· 美加：看重综合背景，周期长（要 GRE/GMAT 与多轮文书）。\n"
        "预算从 20 万到 60 万不等，建议先说预算和目标，我再给具体建议。",
        [{"doc": "院校库", "section": "目的地对比", "version": "2026-08"}],
        ["看目的地对比明细", "预约选校"],
    ),
    (
        ("院校", "排名", "选校", "学校", "qs", "八大", "g5", "藤校"),
        "选校我们按「冲 / 稳 / 保」三档来配，一般各 2-3 所：\n"
        "· 冲：高于当前背景 1 档，靠文书和软背景补；\n"
        "· 稳：与背景匹配，录取概率 6 成以上；\n"
        "· 保：明显低于背景，用来兜底。\n"
        "排名只是个维度，还要看专业实力、地理位置与就业资源。你把均分和目标专业发我，我出初版清单。",
        [{"doc": "院校库", "section": "选校策略", "version": "2026-08"}],
        ["生成选校清单", "查看院校库"],
    ),
    (
        ("专业", "转专业", "跨专业", "换专业", "商科", "计算机", "cs", "金融"),
        "转专业可行性主要看两点：先修课和目标专业的背景偏好。\n"
        "· 商科转 CS / 数据类：通常要补编程与数学先修，部分项目接受 online 先修课；\n"
        "· 理工科转商科：一般最友好，补一门会计/统计即可；\n"
        "· 文科转工科：难度最高，建议走「先读衔接课程」的路径。\n"
        "把本科课程表发我，我可以帮你逐项比对先修课要求。",
        [{"doc": "院校库", "section": "转专业指南", "version": "2026-08"}],
        ["比对先修课", "预约选校"],
    ),
    (
        ("本科", "硕士", "研究生", "博士", "phd", "预科", "专升本", "diploma"),
        "学历层次对应关系：\n"
        "· 本科直申：需高考成绩或 A-Level / IB / AP；\n"
        "· 本科预科：高二/高三可读，完成后升大一；\n"
        "· 硕士：主流选择，1-2 年，看均分与文书；\n"
        "· 硕士预科：均分不够或跨专业时的过渡路径；\n"
        "· 博士：需导师套磁与研究计划（RP），周期 3-4 年。",
        [{"doc": "院校库", "section": "学历层次", "version": "2026-08"}],
        ["看我适合的层次", "预约顾问"],
    ),
    # ---------- 成绩与语言 ----------
    (
        ("gpa", "均分", "绩点", "成绩要求", "分数线"),
        "常见门槛（供参考，具体看院校与专业）：\n"
        "· 英国：均分 80+ 可申 QS 前 100，85+ 冲 G5 更稳；\n"
        "· 澳洲：八大一般 75-80，双非院校要求会高 5 分左右；\n"
        "· 中国香港：211/985 均分 80+，双非建议 85+；\n"
        "· 美国：GPA 3.5/4.0 以上才有竞争力。\n"
        "均分偏低的可以用专业课成绩单、排名证明或补考重修来补救。",
        [{"doc": "院校库", "section": "均分要求", "version": "2026-08"}],
        ["评估我的均分", "预约顾问"],
    ),
    (
        ("雅思", "托福", "语言", "ielts", "toefl", "pte", "多邻国", "语言班", "刷分"),
        "语言要求与补救路径：\n"
        "· 英澳：IELTS 6.5(6.0) 是多数专业门槛，7.0 可冲顶尖项目；\n"
        "· 语言班：差 0.5-1.0 分可申请，读完内部测试即可入学，不用再考雅思；\n"
        "· 美加：TOEFL 90+ / IELTS 7.0+ 比较稳，部分项目接受 Duolingo。\n"
        "语言成绩可以后补，先拿有条件 offer 更划算。",
        [{"doc": "申请服务手册", "section": "语言要求", "version": "2026-08"}],
        ["查看语言班方案", "预约语言规划"],
    ),
    (
        ("奖学金", "助学金", "减免", "资助"),
        "奖学金分三类：\n"
        "1）院校自动评审的入学奖学金，靠均分与文书，无需另交材料；\n"
        "2）需要单独申请的 merit-based 项目，通常有截止日期；\n"
        "3）国家留学基金委（CSC）等外部资助，走公派流程。\n"
        "我们会按你的背景筛一遍可申项目，并同步每条的截止时间。",
        [{"doc": "院校库", "section": "奖学金", "version": "2026-08"}],
        ["筛可申奖学金", "预约顾问"],
    ),
    (
        ("开学", "入学", "申请季", "截止", "什么时候开始", "几月"),
        "常见时间轴（以 9 月入学为例）：\n"
        "· 前一年 6-9 月：定校定专业、准备语言；\n"
        "· 10-12 月：递第一批申请（英国滚动录取，越早越好）；\n"
        "· 次年 1-3 月：拿 offer、补语言、准备资金证明；\n"
        "· 次年 4-7 月：换 CAS、办签证、订住宿；\n"
        "· 次年 8-9 月：行前与入学。\n"
        "澳洲是 2 月/7 月两季入学，节奏可以往后挪半年。",
        [{"doc": "申请服务手册", "section": "时间轴", "version": "2026-08"}],
        ["生成我的时间轴", "预约顾问"],
    ),
    # ---------- 学生事务 ----------
    (
        ("请假", "考务", "申请单"),
        "学生行政服务支持在线提交请假/考务申请，提交后由带教老师在管理后台审批，"
        "审批结果会通过公众号推送。你可以直接在对话里说「我要请假 9 月 15 日 2 天，"
        "原因看病」，我会帮你生成申请单。",
        [{"doc": "学生服务手册", "section": "行政服务", "version": "2026-08"}],
        ["提交请假申请", "查看我的申请"],
    ),
    (
        ("成绩", "分数", "考试", "模考"),
        "成绩查询支持按考试名称与科目筛选，我会在你补充学生编号后返回明细，"
        "并给出与目标院校往年分数线的对比。",
        [{"doc": "学生服务手册", "section": "成绩管理", "version": "2026-08"}],
        ["查询成绩"],
    ),
    (
        ("课程安排", "课表", "上课时间", "排课", "课时"),
        "课程安排由教研组按周排定，课表会在每周日 20:00 前同步到「学员中心」。\n"
        "如需调整（换班、顺延、补课），提前 24 小时提出即可，教务会协调同进度的班次。\n"
        "临近考试周的晚自习教室需要单独预约。",
        [{"doc": "学生服务手册", "section": "课程安排", "version": "2026-08"}],
        ["查看我的课表", "申请调课"],
    ),
    (
        ("师资", "老师", "试听", "教师"),
        "授课老师均为全职教研岗，语言类老师要求 7.5 分或同等成绩 + 3 年以上教龄，"
        "文书老师按目标专业方向匹配。\n"
        "报名前可以免费试听一节，试听后再决定是否入班，试听不绑定签约。",
        [{"doc": "学生服务手册", "section": "师资", "version": "2026-08"}],
        ["预约试听", "查看老师简介"],
    ),
    (
        ("活动", "报名", "讲座", "分享会"),
        "近期开放报名的活动有：澳洲八大申请分享会、雅思口语模考营。"
        "回复活动名称即可报名，名额有限会按报名顺序锁定。",
        [{"doc": "运营活动", "section": "近期活动", "version": "2026-08"}],
        ["查看活动列表", "报名活动"],
    ),
    (
        ("住宿", "租房", "宿舍", "公寓", "homestay", "寄宿"),
        "住宿三种选择：\n"
        "· 校内宿舍：最省心，需在拿 offer 后尽早申请，常要抢；\n"
        "· 校外公寓：性价比高，一般 44 周起租，注意押金与担保人要求；\n"
        "· 寄宿家庭：适合低龄学生，含餐，语言环境最好。\n"
        "我们提供区域安全性与通勤时间对比，签约前会帮你核一遍合同条款。",
        [{"doc": "行前服务手册", "section": "住宿", "version": "2026-07"}],
        ["看住宿方案", "预约行前指导"],
    ),
    (
        ("实习", "就业", "找工作", "求职", "opt", "工签", "留下"),
        "关于毕业后的路径：\n"
        "· 英国：毕业生签证（Graduate Route）本科/硕士 2 年，无需雇主担保；\n"
        "· 澳洲：485 临时毕业生签证，本科 2 年、硕士 3 年，偏远地区可延长；\n"
        "· 美国：OPT 12 个月，STEM 专业可延长 24 个月。\n"
        "在校期间建议至少积累 1 段当地实习，我们提供简历精修与模拟面试。",
        [{"doc": "行前服务手册", "section": "就业与签证", "version": "2026-07"}],
        ["预约简历精修", "看就业数据"],
    ),
    # ---------- 售后 ----------
    (
        ("投诉", "建议", "不满意", "反馈", "意见"),
        "很抱歉给你带来不好的体验。已收到你的反馈，我会生成售后工单交由专人跟进，"
        "处理进度可在「我的工单」里查看，一般 1 个工作日内首次响应。",
        [{"doc": "售后服务规范", "section": "投诉与建议", "version": "2026-08"}],
        ["查看我的工单"],
    ),
    (
        ("转人工", "人工客服", "找人工", "转接"),
        "已为你转接人工客服。工作时间内（9:00-21:00）顾问会在 5 分钟内接入；"
        "非工作时间可先留下联系方式，顾问次日 10:00 前回电。",
        [{"doc": "售后服务规范", "section": "人工转接", "version": "2026-08"}],
        ["留下联系方式"],
    ),
]

# 语料完全没命中时的兜底话术。
# 关键是**别装作答出来了，也别甩一句「暂未查询到」就走**——
# 用户看不出下一步该怎么办。所以这里明确说明能力边界 + 给出可问的问题。
MOCK_NO_RECALL_TEXT = (
    "这个问题我暂时没有可用的信息，不能凭空给你答复。\n\n"
    "我目前能处理这几类：\n"
    "· 机构与政策：校区地址、申请材料、服务费用、签证流程、服务时间\n"
    "· 选校与专业：国家对比、院校排名、转专业、均分与语言要求、奖学金、时间轴\n"
    "· 学生事务：请假/考务申请、成绩查询、课程安排、活动报名、投诉与建议\n"
    "· 经营数据（员工及以上）：客户跟进记录、线索状态分布、待审批申请、工单情况\n\n"
    "换个说法再问一次，或者回复「转人工」由顾问接手。"
)


def _match_kb(query: str):
    """在语料里找最具体的一条；没命中返回 None。

    按 **最长关键词优先** 打分，而不是按命中个数：
    「查一下最近的意向客户跟进情况」同时命中「客户」「意向」两个短词，
    若不看长度会压过更具体的「跟进情况」。
    """
    best = None
    best_score = (0, 0)
    lowered = query.lower()
    for entry in _MOCK_KB:
        matched = [kw for kw in entry[0] if kw in query or kw in lowered]
        if not matched:
            continue
        score = (max(len(k) for k in matched), len(matched))
        if score > best_score:
            best, best_score = entry, score
    return best


def mock_kb_answer(app: str, query: str) -> Optional[DifyChatResult]:
    """命中内置语料就返回结果；未命中返回 None（兜底话术由调用方决定）。"""
    entry = _match_kb(query)
    if entry is None:
        return None
    _keywords, answer, refs, actions = entry
    return DifyChatResult(
        answer=answer,
        conversation_id=f"conv-mock-{abs(hash(query)) % 100000:05d}",
        message_id="msg-mock",
        references=refs,
        suggest_actions=list(actions),
        metadata={"mode": "mock", "app": app, "kb_hit": True},
    )


def _mock_chat_answer(app: str, query: str) -> DifyChatResult:
    """mock 的低层兜底：语料命中就用语料，否则给可解释的兜底话术。

    ⚠️ 这层**不认识路由结果**（不知道 intent、也拿不到数据库）。
    统一对话入口在 mock 模式下走 `services.mock_agent`——它会先看 intent，
    该查库的就去查库。本函数只在「live 调 Dify 失败降级」时被用到。
    """
    hit = mock_kb_answer(app, query)
    if hit is not None:
        return hit
    return DifyChatResult(
        answer=MOCK_NO_RECALL_TEXT,
        conversation_id=f"conv-mock-{abs(hash(query)) % 100000:05d}",
        message_id="msg-mock",
        metadata={"mode": "mock", "app": app, "no_recall": True},
    )


class DifyClient:
    """Dify API 客户端（异步）。全部方法在 mock 模式下不发起任何网络请求。"""

    def __init__(self, mode: Optional[str] = None,
                 base_url: Optional[str] = None,
                 timeout: Optional[float] = None,
                 transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.mode = (mode or settings.dify_mode).lower()
        self.base_url = (base_url or settings.dify_base_url).rstrip("/")
        self.timeout = timeout or settings.dify_timeout
        self._transport = transport

    # ------------------------------------------------------------------ #
    # 内部：构造请求
    # ------------------------------------------------------------------ #
    def _client(self, api_key: str) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        # trust_env 按**本客户端的目标地址**判断（见 settings.resolve_proxy_trust）：
        # Dify 在内网/本机时要绕开系统代理，否则发往 127.0.0.1 的请求会被代理接走
        # （实测返回 502），再被降级逻辑吞掉。注意不能用 settings.dify_base_url 判断，
        # 因为 base_url 可能被构造时覆盖成别的目标。
        return httpx.AsyncClient(base_url=self.base_url, headers=headers,
                                 timeout=self.timeout, transport=self._transport,
                                 trust_env=settings.resolve_proxy_trust(self.base_url))

    def _key(self, app: str) -> str:
        key = settings.dify_key(app)
        if not key:
            missing = "、".join(settings.dify_missing_keys) or app
            raise UpstreamError(
                f"Dify 应用 [{app}] 未配置 API Key（当前还缺：{missing}）。"
                '请在 .env 里补 DIFY_APP_KEYS，支持两种写法：\n'
                '  DIFY_APP_KEYS={"router":"app-xxx","customer_service":"app-yyy"}\n'
                "  DIFY_APP_KEYS=router=app-xxx,customer_service=app-yyy\n"
                "配好后用 `python scripts/check_dify.py` 一键校验连通性。"
            )
        return key

    # ------------------------------------------------------------------ #
    # 对话（blocking）
    # ------------------------------------------------------------------ #
    async def chat(self, app: str, query: str, user: str,
                   conversation_id: Optional[str] = None,
                   inputs: Optional[Dict[str, Any]] = None) -> DifyChatResult:
        if self.mode != "live":
            return _mock_chat_answer(app, query)

        payload: Dict[str, Any] = {
            "inputs": inputs or {},
            "query": query,
            "response_mode": "blocking",
            "user": user,
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id

        try:
            async with self._client(self._key(app)) as client:
                resp = await client.post("/chat-messages", json=payload)
                if conversation_id and _conversation_is_stale(resp):
                    # 会话属于别的应用：丢掉它重开一次，**不要降级**
                    logger.info("Dify 会话 %s 不属于应用 %s，改为新建会话重试",
                                conversation_id, app)
                    return await self.chat(app, query, user, conversation_id=None,
                                           inputs=inputs)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:                      # noqa: BLE001 - 统一降级
            # 降级是兜底，但**必须让人看得见**：否则 live 模式配错了 Key / 地址，
            # 用户拿到的是 mock 回答却以为「已经接上 Dify 了」。
            logger.warning("Dify chat 调用失败（app=%s，base=%s），降级到 mock：%s",
                           app, self.base_url, exc)
            result = _mock_chat_answer(app, query)
            result.degraded = True
            result.metadata = {
                **result.metadata,
                "degraded": True,
                "degraded_reason": f"{type(exc).__name__}: {exc}",
            }
            return result

        metadata = data.get("metadata") or {}
        return DifyChatResult(
            answer=data.get("answer", ""),
            conversation_id=data.get("conversation_id", ""),
            message_id=data.get("message_id", ""),
            metadata=metadata,
            references=metadata.get("retriever_resources") or [],
            raw=data,
        )

    # ------------------------------------------------------------------ #
    # 对话（streaming -> SSE）
    # ------------------------------------------------------------------ #
    async def stream_chat(self, app: str, query: str, user: str,
                          conversation_id: Optional[str] = None,
                          inputs: Optional[Dict[str, Any]] = None) -> AsyncIterator[dict]:
        if self.mode != "live":
            result = _mock_chat_answer(app, query)
            # 按句子切片模拟流式，前端体验一致且结果确定
            for piece in _chunk_text(result.answer):
                yield {"event": "message", "answer": piece}
            yield {"event": "message_end", "conversation_id": result.conversation_id,
                   "references": result.references}
            return

        payload = {
            "inputs": inputs or {},
            "query": query,
            "response_mode": "streaming",
            "user": user,
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id

        # 与 blocking 路径同样的原则：Dify 挂了也要有答复，且必须让降级可见。
        emitted = 0
        stack = AsyncExitStack()
        effective_conv = conversation_id
        try:
            stack, resp, effective_conv = await self._open_chat_stream(
                app, payload, conversation_id)
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    event = json.loads(body)
                except json.JSONDecodeError:
                    continue
                emitted += 1
                yield event
        except Exception as exc:                      # noqa: BLE001 - 统一降级
            logger.warning("Dify stream 调用失败（app=%s，base=%s）：%s",
                           app, self.base_url, exc)
            reason = f"{type(exc).__name__}: {exc}"
            if emitted:
                # 已经吐了一部分，再补一段完整 mock 会前后矛盾；
                # 收尾并明确标记「这轮不完整」。
                yield {"event": "message_end", "conversation_id": effective_conv or "",
                       "references": [], "degraded": True, "degraded_reason": reason}
                return
            result = _mock_chat_answer(app, query)
            for piece in chunk_text(result.answer):
                yield {"event": "message", "answer": piece}
            yield {"event": "message_end", "conversation_id": result.conversation_id,
                   "references": result.references, "degraded": True,
                   "degraded_reason": reason}
        finally:
            await stack.aclose()

    async def _open_chat_stream(self, app: str, payload: Dict[str, Any],
                                conversation_id: Optional[str]
                                ) -> Tuple[AsyncExitStack, httpx.Response, Optional[str]]:
        """打开 Dify 的流式对话响应。

        返回 `(退出栈, 响应, 实际生效的会话 ID)`，**调用方负责 `stack.aclose()`**。

        若 `conversation_id` 不属于本应用（跨 Agent 复用 / 会话被删 / 垃圾值），
        这里就丢掉它**重开一次**再返回 —— 不能把 404 抛给上层的降级逻辑，
        否则用户拿到的是一段与问题无关的 mock 套话。
        """
        stack = AsyncExitStack()
        try:
            client = await stack.enter_async_context(self._client(self._key(app)))
            resp = await stack.enter_async_context(
                client.stream("POST", "/chat-messages", json=payload))
            if conversation_id and resp.status_code in (400, 404):
                await resp.aread()
                if _conversation_missing(_json_or_none(resp)):
                    logger.info("Dify 会话 %s 不属于应用 %s，改为新建会话重试",
                                conversation_id, app)
                    await stack.aclose()
                    return await self._open_chat_stream(
                        app, _drop_conversation(payload), None)
        except Exception:
            await stack.aclose()
            raise
        return stack, resp, conversation_id

    # ------------------------------------------------------------------ #
    # 工作流（研判 / 报告 / 意图路由）
    # ------------------------------------------------------------------ #
    async def run_workflow(self, app: str, inputs: Dict[str, Any], user: str) -> Dict[str, Any]:
        if self.mode != "live":
            return {"outputs": _mock_workflow_outputs(app, inputs),
                    "status": "succeeded", "id": f"run-mock-{app}"}

        payload = {"inputs": inputs, "response_mode": "blocking", "user": user}
        try:
            async with self._client(self._key(app)) as client:
                resp = await client.post("/workflows/run", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:                      # noqa: BLE001
            logger.warning("Dify workflow 调用失败，降级到 mock: %s", exc)
            return {"outputs": _mock_workflow_outputs(app, inputs),
                    "status": "succeeded", "id": f"run-mock-{app}", "degraded": True}
        return data.get("data", data)

    # ------------------------------------------------------------------ #
    # 语音转写
    # ------------------------------------------------------------------ #
    async def audio_to_text(self, app: str, filename: str, content: bytes,
                            user: str) -> str:
        if self.mode != "live":
            raise UpstreamError("mock 模式下不提供远端转写")
        files = {"file": (filename, content)}
        data = {"user": user}
        async with self._client(self._key(app)) as client:
            resp = await client.post("/audio-to-text", files=files, data=data)
            resp.raise_for_status()
            return resp.json().get("text", "")


def chunk_text(text: str, size: int = 12) -> List[str]:
    """把整段文本切成小片，用于模拟流式输出（前端体验与真实 Dify 一致）。"""
    return [text[i:i + size] for i in range(0, len(text), size)]


# 兼容旧名字（曾用私有名）
_chunk_text = chunk_text


def _mock_workflow_outputs(app: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
    """mock 工作流输出：结构真实、内容确定，便于断言。"""
    if app == APP_ROUTER:
        text = str(inputs.get("query", ""))
        agent, intent, conf = _mock_route(text)
        return {"agent": agent, "intent": intent, "confidence": conf, "slots": {}}

    if app == APP_SCREENER:
        text = str(inputs.get("text", ""))
        hit = [p for p in ("澳洲", "英国", "加拿大", "日本", "新加坡") if p in text] or ["英国"]
        has_score = any(ch.isdigit() for ch in text)
        conclusion = "符合" if has_score else "信息不足"
        return {
            "conclusion": conclusion,
            "hit_products": [{"name": f"{h}硕士直申", "match": 0.8} for h in hit],
            "extracted_fields": {
                "intention_country": hit[0],
                "gpa": next((t for t in text.split() if "/" in t and any(c.isdigit() for c in t)), None),
                "language": "IELTS" if "雅思" in text else None,
            },
            "evidence": [{"rule": "学历背景匹配", "excerpt": text[:60]}],
            "confidence": 0.86 if has_score else 0.42,
        }

    if app == APP_REPORTER:
        return {"content": "# 周报（mock）\n\n- 新增意向客户：12\n- 成交转化：3\n- 待跟进：27\n",
                "summary": "本周线索 12 条，转化 3 单，待跟进 27 条。"}

    return {"answer": "（mock 工作流输出）"}


_ROUTE_RULES = [
    (("我要请假", "请假申请", "请个假", "考务申请"), "student_helper", "leave_apply"),
    (("投诉", "建议", "不满意", "反馈"), "student_helper", "after_sales"),
    (("成绩", "分数", "考试", "模考"), "student_helper", "score_query"),
    (("活动", "报名", "讲座", "分享会"), "student_helper", "activity_enroll"),
    # 知识类问句优先判定：命中「政策/材料/流程/费用」等词时不该被当成取数请求。
    # 注意「哪些」「多少」这类疑问词过于宽泛，必须放在这里而不是 nl2sql 规则里，
    # 否则「留学需要准备哪些材料」会被误路由到 enterprise_assistant。
    (("校区", "地址", "在哪", "费用", "多少钱", "学费", "价格", "退费", "退款", "政策",
      "签证", "材料", "流程", "机构", "服务", "文书", "排名", "院校", "选校",
      "均分", "gpa", "雅思", "托福", "语言", "奖学金", "住宿", "宿舍", "实习", "就业",
      "时间轴", "开学", "申请季", "截止", "师资", "试听", "课程安排", "课表", "合同",
      "保录取", "背景评估", "工作时间", "营业时间", "联系方式", "电话"),
     "customer_service", "company_info"),
    # 取数类：放在知识类之后，避免「查一下你们的服务流程」被当成 SQL 请求。
    # 「各科平均分」是特意保留的**长词**：它跟知识类的「均分」只差一个字，
    # 同分时靠前的知识规则会赢，于是「统计一下各科平均分」被答成均分要求。
    # 用 5 字强信号词压过「均分」，同时不会误伤「平均分要求是多少」这类政策问法。
    (("跟进记录", "跟进情况", "成绩单", "查一下", "查询", "查查", "看一下", "看下", "看看",
      "统计", "列出", "列表", "多少条", "几条", "多少个", "汇总", "报表", "数据",
      "各科平均分"),
     "enterprise_assistant", "nl2sql_query"),
    (("日报", "审批", "录入"), "enterprise_assistant", "daily_report"),
    (("周报", "月报", "报告"), "reporter", "report_generate"),
    (("预约", "到访", "咨询", "试听"), "customer_service", "booking"),
    (("转人工", "人工客服", "找人工"), "customer_service", "handover"),
    (("难受", "焦虑", "压力", "失眠", "崩溃", "不想活"), "mental_care", "emotion_support"),
]


def _mock_route(text: str):
    """按「最长命中关键词优先」选规则，长度相同时先声明的规则赢。

    为什么要打分而不是 first-match：规则里既有「机构」「服务」这种宽泛词，
    也有「跟进情况」「多少钱」这种强信号词。first-match 会让
    「统计一下客户线索的数量」被宽泛词抢走；打分则让强信号词胜出，
    规则顺序只在同分时充当优先级。
    """
    lowered = text.lower()
    best = None
    best_score = (0, 0)
    for keywords, agent, intent in _ROUTE_RULES:
        matched = [kw for kw in keywords if kw in text or kw in lowered]
        if not matched:
            continue
        score = (max(len(k) for k in matched), len(matched))
        if score > best_score:                 # 严格大于 → 同分保留靠前的规则
            best, best_score = (agent, intent), score
    if best is None:
        return "customer_service", "unknown", 0.4
    return best[0], best[1], 0.92


dify_client = DifyClient()
