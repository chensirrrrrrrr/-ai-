# -*- coding: utf-8 -*-
"""建表 + 灌入演示数据（幂等，可重复执行）。

用法：
    python scripts/seed.py            # 追加缺失数据
    python scripts/seed.py --reset    # 先删表再重建（慎用）
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import hash_password                       # noqa: E402
from app.db import SessionLocal, drop_all, init_db       # noqa: E402
from app.models import (Activity, ActivityEnrollment, AfterSalesTicket,  # noqa: E402
                        CourseProject, CustomerFollowup, CustomerLead,
                        Employee, EmployeeReport, KnowledgeDoc, LeadScreening,
                        MentalAlert, ScreeningRule, Student, StudentDeadline,
                        StudentMentalProfile, StudentProgress,
                        StudentRequest, StudentScore, SysAccount)
from app.services import material, onboarding, rules        # noqa: E402

ACCOUNTS = [
    ("admin",    "admin123",   "admin",    None, "系统管理员"),
    ("manager",  "manager123", "manager",  3,    "陈总（管理层）"),
    ("advisor",  "advisor123", "employee", 1,    "王敏（顾问）"),
    ("teacher",  "teacher123", "employee", 2,    "李强（带教老师）"),
    ("student",  "student123", "student",  1,    "张三（学生）"),
]


def seed(reset: bool = False) -> None:
    if reset:
        drop_all()
    init_db()

    db = SessionLocal()
    try:
        now = datetime.now()

        # ---------------- 账号 ----------------
        for username, pwd, role, ref_id, display in ACCOUNTS:
            if not db.query(SysAccount).filter(SysAccount.username == username).first():
                db.add(SysAccount(username=username, password_hash=hash_password(pwd),
                                  role=role, ref_id=ref_id, display_name=display,
                                  is_active=True))
        db.flush()

        # ---------------- 组织 ----------------
        if not db.query(Employee).filter(Employee.emp_no == "E001").first():
            db.add_all([
                Employee(emp_no="E001", name="王敏", department="顾问部", title="高级顾问",
                         phone="13800000001", email="wangmin@example.com",
                         biz_role="advisor", status="ACTIVE"),
                Employee(emp_no="E002", name="李强", department="教务部", title="带教老师",
                         phone="13800000002", email="liqiang@example.com",
                         biz_role="teacher", status="ACTIVE"),
                Employee(emp_no="E003", name="陈总", department="管理层", title="总经理",
                         phone="13800000003", email="chen@example.com",
                         biz_role="manager", status="ACTIVE"),
            ])
            db.flush()
            admin = db.query(Employee).filter(Employee.emp_no == "E001").first()
            leader = db.query(Employee).filter(Employee.emp_no == "E003").first()
            admin.manager_id = leader.id

        # ---------------- 学员 ----------------
        if not db.query(Student).filter(Student.student_no == "S2026001").first():
            db.add_all([
                Student(student_no="S2026001", name="张三", gender="男",
                        phone="13900000001", email="zhangsan@example.com",
                        id_card_masked="5101**********1234", country_target="澳大利亚",
                        program_level="硕士", stage="APPLYING", advisor_id=1,
                        enroll_date=date(2025, 9, 1), status="ACTIVE"),
                Student(student_no="S2026002", name="李四", gender="女",
                        phone="13900000002", email="lisi@example.com",
                        id_card_masked="5101**********5678", country_target="英国",
                        program_level="硕士", stage="PREPARING", advisor_id=1,
                        enroll_date=date(2026, 2, 15), status="ACTIVE"),
                Student(student_no="S2026003", name="王五", gender="男",
                        phone="13900000003", email="wangwu@example.com",
                        country_target="加拿大", program_level="本科",
                        stage="OFFERED", advisor_id=1, enroll_date=date(2025, 6, 1),
                        status="ACTIVE"),
            ])
            db.flush()

        # ---------------- 客户 ----------------
        if not db.query(CustomerLead).first():
            db.add_all([
                CustomerLead(name="赵六", phone="13700000001", source="官网表单",
                             intention_country="英国", intention_stage="硕士",
                             status="FOLLOWING", owner_id=1),
                CustomerLead(name="孙七", phone="13700000002", source="微信公众号",
                             intention_country="澳大利亚", intention_stage="硕士",
                             status="NEW", owner_id=1),
                CustomerLead(name="周八", phone="13700000003", source="线下活动",
                             intention_country="加拿大", intention_stage="本科",
                             status="SIGNED", owner_id=1),
            ])
            db.flush()
            lead = db.query(CustomerLead).filter(CustomerLead.name == "赵六").first()
            db.add(CustomerFollowup(
                lead_id=lead.id, content="电话沟通，客户关注英国 G5 录取率与语言要求，"
                                         "已发送选校方案初稿。",
                follow_type="PHONE", next_plan="3 天后跟进雅思分数",
                next_follow_at=now + timedelta(days=3), owner_id=1))

        # ---------------- 客户研判（含三种复核状态，便于演示 REQ-M1-06/07） ----------------
        if not db.query(LeadScreening).first():
            seeds = [
                # 赵六：材料齐全，AI 判「符合」，人工已确认
                dict(lead_name="赵六", source_type="PDF", source_name="赵六-简历.pdf",
                     text="姓名：赵六\n年龄：24\n学历：本科\n"
                          "毕业院校：西南财经大学\n专业：金融学\n"
                          "GPA 3.4/4.0\n雅思 7.0\n意向国家：英国\n意向阶段：待签约\n",
                     ai_conclusion="符合", conclusion="符合",
                     review_status="CONFIRMED", reviewed=True,
                     remark=None,
                     evidence=[{"rule": "学历背景匹配：本科及以上", "excerpt": "学历：本科"},
                               {"rule": "语言成绩达标：IELTS >= 6.5", "excerpt": "雅思 7.0"}]),
                # 孙七：只有登记表，字段大面积缺失 → 按规则只能给「信息不足」
                dict(lead_name="孙七", source_type="EXCEL", source_name="孙七-登记表.xlsx",
                     text="【客户登记表】\n意向国家：澳大利亚\n意向阶段：了解中\n",
                     ai_conclusion="信息不足", conclusion="信息不足",
                     review_status="PENDING", reviewed=False,
                     remark=None, evidence=None),
                # 周八：AI 按学历判「符合」，人工复核推翻 → 进修正清单做规则优化素材
                dict(lead_name="周八", source_type="TEXT", source_name=None,
                     text="客户姓名：周八\n意向国家：加拿大\n学历：本科\n"
                          "毕业院校：成都理工大学\n专业：土木工程\n均分 68\n"
                          "意向阶段：已签约\n",
                     ai_conclusion="符合", conclusion="不符合",
                     review_status="OVERRIDDEN", reviewed=True,
                     remark="均分 68 低于加拿大直申门槛，AI 只按学历命中就判符合，属误判。",
                     evidence=[{"rule": "学历背景匹配：本科及以上", "excerpt": "学历：本科"}]),
            ]
            for item in seeds:
                lead_row = db.query(CustomerLead).filter(
                    CustomerLead.name == item["lead_name"]).first()
                if lead_row is None:
                    continue
                fields = material.extract_fields(item["text"])
                db.add(LeadScreening(
                    lead_id=lead_row.id,
                    source_type=item["source_type"],
                    source_name=item["source_name"],
                    batch_id=None,
                    extracted_fields=fields,
                    missing_fields=material.missing_fields(fields),
                    hit_products=[{"name": f"{fields.get('intention_country') or '英国'}硕士直申",
                                   "match": 0.82 if item["review_status"] != "PENDING" else 0.42}],
                    conclusion=item["conclusion"],
                    ai_conclusion=item["ai_conclusion"],
                    evidence=item["evidence"],
                    confidence=0.86 if item["review_status"] != "PENDING" else 0.42,
                    review_status=item["review_status"],
                    reviewed_by=1 if item["reviewed"] else None,
                    reviewed_at=now if item["reviewed"] else None,
                    review_remark=item["remark"],
                ))
            db.flush()

        # ---------------- 成绩 / 申请 / 工单 / 预警 ----------------
        if not db.query(StudentScore).first():
            db.add_all([
                StudentScore(student_id=1, exam_name="雅思第一次", subject="IELTS",
                             score=6.5, full_score=9, exam_date=date(2026, 6, 20)),
                StudentScore(student_id=1, exam_name="雅思第二次", subject="IELTS",
                             score=7.0, full_score=9, exam_date=date(2026, 8, 25)),
                StudentScore(student_id=2, exam_name="期中模考", subject="数学",
                             score=88, full_score=100, exam_date=date(2026, 5, 10)),
            ])

        if not db.query(StudentRequest).first():
            db.add_all([
                StudentRequest(student_id=1, type="LEAVE", status="PENDING",
                               content={"start_date": "2026-09-20", "days": 2,
                                        "reason": "看病"},
                               idempotency_key="seed-leave-1"),
                StudentRequest(student_id=2, type="EXAM", status="APPROVED",
                               content={"exam": "雅思", "date": "2026-10-12"},
                               approver_id=2, approved_at=now),
            ])

        if not db.query(AfterSalesTicket).first():
            db.add(AfterSalesTicket(student_id=2, content="签证材料清单回复较慢，希望加快。",
                                    summary="签证材料清单回复较慢，希望加快。",
                                    category="签证", status="OPEN"))

        if not db.query(MentalAlert).first():
            db.add_all([
                StudentMentalProfile(student_id=1, emotion_score=72, risk_level="LOW",
                                     tags=["稳定"], summary="情绪平稳，申请节奏正常。",
                                     last_assessed_at=now),
                MentalAlert(student_id=2, risk_level="MEDIUM",
                            reason="连续三次对话出现焦虑表达，提到论文与语言双重压力。",
                            evidence="“感觉自己什么都做不好”（已脱敏）", status="OPEN",
                            handler_id=2, notified_at=now, notified_to=2,
                            notify_channel="chat",
                            intervention="1. 24 小时内单独联系（线上亦可），先听不评判\n"
                                         "2. 围绕触发点：论文与语言双重压力\n"
                                         "3. 3 个工作日内回填跟进记录"),
                # 未触达的高危预警：用来演示「主动待办推送 → 一键触达 → 留痕」整条链路
                MentalAlert(student_id=1, risk_level="HIGH",
                            reason="连续 4 天情绪打卡低于 40 分，出现「不想继续了」等表述。",
                            evidence="“真的撑不住了，签证再卡下去就不读了”（已脱敏）",
                            status="OPEN"),
            ])

        # 到期未跟进的客户（主动待办里「有没有客户到了约定跟进时间」的来源）
        if not db.query(CustomerFollowup).filter(CustomerFollowup.next_follow_at < now).first():
            lead2 = db.query(CustomerLead).filter(CustomerLead.name == "孙七").first()
            if lead2 is not None:
                db.add(CustomerFollowup(
                    lead_id=lead2.id,
                    content="微信沟通，家长希望对比澳洲与英国的预算差异，已发对比表。",
                    follow_type="WECHAT", next_plan="确认家长预算区间后再出选校清单",
                    next_follow_at=now - timedelta(days=2), owner_id=1))

        # ---------------- 活动 / 项目 / 知识库 ----------------
        if not db.query(Activity).first():
            db.add_all([
                Activity(title="澳洲八大申请分享会", category="宣讲",
                         description="澳洲八大招生官现场答疑", start_at=now + timedelta(days=5),
                         end_at=now + timedelta(days=5, hours=2),
                         location="成都武侯服务中心", capacity=60, enrolled_count=0,
                         status="OPEN"),
                Activity(title="雅思口语模考营", category="培训",
                         description="一对一模拟口语考试", start_at=now + timedelta(days=12),
                         location="成都高新申请中心", capacity=20, enrolled_count=0,
                         status="OPEN"),
            ])
            db.flush()

        if not db.query(CourseProject).first():
            db.add_all([
                CourseProject(name="英国硕士直申计划", category="申请项目", country="英国",
                              degree_level="硕士", tuition=38000, duration_months=12,
                              description="G5 + 罗素集团院校申请", status="ACTIVE"),
                CourseProject(name="澳洲八大保录计划", category="申请项目", country="澳大利亚",
                              degree_level="硕士", tuition=32000, duration_months=12,
                              description="含语言班与住宿衔接", status="ACTIVE"),
            ])

        if not db.query(KnowledgeDoc).first():
            db.add_all([
                KnowledgeDoc(title="公司信息与服务政策", category="公司信息",
                             version="V2026.08", dify_dataset_id="ds-company-001",
                             chunk_count=128, status="INDEXED", effective_at=now),
                KnowledgeDoc(title="各国签证材料清单", category="签证", version="V2026.07",
                             dify_dataset_id="ds-visa-001", chunk_count=342,
                             status="INDEXED", effective_at=now),
            ])

        # 新人入职指引：内容以 services/onboarding.py 为唯一来源（版本号跟着它走），
        # 这里只做「知识资产登记」，让后台能检索、改版、下线，也能进 Dify 数据集被对话召回。
        # 单独判断而不是挂在上面那个 if 里 —— 老库已有知识文档时，这一条也补得进去。
        if not db.query(KnowledgeDoc).filter(
                KnowledgeDoc.category == onboarding.GUIDE_CATEGORY).first():
            stats = onboarding.guide()["stats"]
            db.add(KnowledgeDoc(
                title=onboarding.GUIDE_TITLE, category=onboarding.GUIDE_CATEGORY,
                version=onboarding.GUIDE_VERSION,
                source_url="/api/v1/org/onboarding/guide",
                dify_dataset_id="ds-neo-001",
                chunk_count=stats["item_count"] + stats["faq_count"],
                status="INDEXED", effective_at=now))

        if not db.query(EmployeeReport).first():
            db.add(EmployeeReport(employee_id=1, report_date=date.today(),
                                  content="今天陪张三核对澳国立与墨尔本材料清单，"
                                          "跟进李四雅思成绩 6.5，约明天复盘选校方案。",
                                  summary="跟进 2 名学生，材料核对与选校复盘。",
                                  source="voice", transcript="（语音转写省略）"))
            db.add(EmployeeReport(employee_id=2, report_date=date.today(),
                                  content="完成雅思口语模考排课，处理 1 条签证咨询工单。",
                                  summary="排课与工单处理。", source="manual"))

        # ---------------- 报告演示数据 ----------------
        # 五类报告（M5）要看的是「趋势 / 分布 / 时效」，3 条线索 + 2 篇日报撑不起任何结论。
        # 这里补齐一个像样的漏斗与一段时间分布。
        # 每块单独判断（不是 `if not xxx.first()`）：老库缺哪块就补哪块，重复跑也不会翻倍。
        if db.query(CustomerLead).count() < 10:
            def _lead(name, phone, country, source, status, created_days, updated_days,
                      remark=None, stage="硕士"):
                return CustomerLead(
                    name=name, phone=phone, source=source, intention_country=country,
                    intention_stage=stage, status=status, owner_id=1, remark=remark,
                    created_at=now - timedelta(days=created_days),
                    updated_at=now - timedelta(days=updated_days))

            db.add_all([
                _lead("吴一", "13700000011", "英国", "官网表单", "FOLLOWING", 9, 8,
                      "已发选校清单，等家长确认预算区间"),
                _lead("郑二", "13700000012", "澳大利亚", "转介绍", "SIGNED", 14, 4,
                      "签约英澳联申，文书已定稿"),
                _lead("王三", "13700000013", "中国香港", "微信公众号", "SIGNED", 18, 6,
                      "港三商科，语言已达标"),
                _lead("冯四", "13700000014", "美国", "线下活动", "LOST", 16, 5,
                      "预算不足，家里希望先考公，暂缓出国"),
                _lead("陈五", "13700000015", "英国", "官网表单", "LOST", 12, 7,
                      "同时对比了三家机构，最终选了本地小机构"),
                _lead("褚六", "13700000016", "加拿大", "微信公众号", "FOLLOWING", 21, 11,
                      "家长犹豫，两次跟进未回复"),
                _lead("卫七", "13700000017", "新加坡", "转介绍", "FOLLOWING", 6, 2,
                      "关注新加坡国立，等雅思出分"),
                _lead("蒋八", "13700000018", "澳大利亚", "官网表单", "NEW", 3, 3),
                _lead("沈九", "13700000019", "日本", "线下活动", "NEW", 1, 1,
                      "咨询日本修士直申"),
                _lead("韩十", "13700000020", "加拿大", "官网表单", "NEW", 2, 2),
            ])
            db.flush()

        if db.query(EmployeeReport).count() < 10:
            db.add_all([
                EmployeeReport(employee_id=1, report_date=date.today() - timedelta(days=1),
                               content="跟进士七的新加坡国立申请，整理选校对比表；"
                                       "回访赵六确认预算区间。",
                               summary="新加坡选校 + 客户回访。", source="manual"),
                EmployeeReport(employee_id=3, report_date=date.today() - timedelta(days=1),
                               content="复盘本月转化数据，发现跟进超过 7 天的线索有 2 条，"
                                       "已要求顾问本周内处理。",
                               summary="转化复盘，指出现有风险。", source="manual"),
                EmployeeReport(employee_id=2, report_date=date.today() - timedelta(days=2),
                               content="雅思口语模考营报名 6 人，超出容量 20 人以内；"
                                       "处理 2 条院校申请类咨询。",
                               summary="活动报名与咨询处理。", source="manual"),
                EmployeeReport(employee_id=1, report_date=date.today() - timedelta(days=2),
                               content="陪同冯四家长沟通预算问题，客户明确表示暂缓；"
                                       "协助王三递交港三商科申请。",
                               summary="流失沟通与申请递交。", source="voice",
                               transcript="（语音转写省略）"),
                EmployeeReport(employee_id=2, report_date=date.today() - timedelta(days=3),
                               content="签证材料清单被退回 1 份，客户不满，存在流失风险，"
                                       "已上报主管跟进。",
                               summary="签证材料退回，存在流失风险。", source="manual"),
                EmployeeReport(employee_id=1, report_date=date.today() - timedelta(days=3),
                               content="签约郑二英澳联申，合同与文书启动会已完成；"
                                       "梳理下周待跟进名单 5 人。",
                               summary="签单 1 单 + 下周计划。", source="manual"),
                EmployeeReport(employee_id=3, report_date=date.today() - timedelta(days=4),
                               content="组织周会，对齐本月目标：新增线索 20 条、成交 4 单。",
                               summary="目标对齐。", source="manual"),
                EmployeeReport(employee_id=1, report_date=date.today() - timedelta(days=4),
                               content="推进陈五的对比方案，客户对费用仍有疑虑。",
                               summary="客户比价中。", source="manual"),
                EmployeeReport(employee_id=2, report_date=date.today() - timedelta(days=5),
                               content="完成 3 场一对一文书批注；安排下周试听课 2 场。",
                               summary="文书批注与试听排期。", source="voice",
                               transcript="（语音转写省略）"),
                EmployeeReport(employee_id=1, report_date=date.today() - timedelta(days=6),
                               content="整理英国方向案例库 8 个，供新顾问培训使用。",
                               summary="案例库整理。", source="manual"),
                EmployeeReport(employee_id=2, report_date=date.today() - timedelta(days=6),
                               content="跟进褚六家长三次未回复，判断跟进意愿下降。",
                               summary="客户跟进受阻。", source="manual"),
            ])

        if db.query(AfterSalesTicket).count() < 5:
            db.add_all([
                AfterSalesTicket(student_id=1, content="文书批注等待时间偏长，希望加快。",
                                 summary="文书批注等待偏长。", category="院校申请",
                                 status="RESOLVED", handler_id=2,
                                 resolved_at=now - timedelta(days=4), satisfaction=4,
                                 created_at=now - timedelta(days=5)),
                AfterSalesTicket(student_id=3, content="宿舍申请指导不够细，材料被打回一次。",
                                 summary="宿舍申请材料被退回。", category="生活服务",
                                 status="CLOSED", handler_id=2,
                                 resolved_at=now - timedelta(days=2), satisfaction=3,
                                 created_at=now - timedelta(days=3)),
                AfterSalesTicket(student_id=2, content="申请进度更新不及时，想了解具体节点。",
                                 summary="进度更新不及时。", category="院校申请",
                                 status="PROCESSING", handler_id=1,
                                 created_at=now - timedelta(days=2)),
                AfterSalesTicket(student_id=1, content="对服务费用明细有疑问，希望书面说明。",
                                 summary="费用明细疑问。", category="费用",
                                 status="OPEN", created_at=now - timedelta(days=1)),
                AfterSalesTicket(student_id=3, content="签证预约时间与考试冲突，希望协助调整。",
                                 summary="签证预约冲突。", category="签证",
                                 status="RESOLVED", handler_id=1,
                                 resolved_at=now - timedelta(days=1), satisfaction=5,
                                 created_at=now - timedelta(days=2)),
                AfterSalesTicket(student_id=2, content="签证材料清单反复修改，沟通成本高。",
                                 summary="签证材料反复修改（长期未决）。", category="签证",
                                 status="OPEN", created_at=now - timedelta(days=9)),
            ])

        if db.query(StudentMentalProfile).count() < 3:
            db.add_all([
                StudentMentalProfile(student_id=2, emotion_score=58, risk_level="MEDIUM",
                                     tags=["学业焦虑", "语言压力"],
                                     summary="语言刷分与论文双重压力，睡眠质量下降。",
                                     last_assessed_at=now - timedelta(days=1)),
                StudentMentalProfile(student_id=3, emotion_score=64, risk_level="MEDIUM",
                                     tags=["文化冲突", "孤独感"],
                                     summary="刚到海外，社交圈尚未建立，节假日情绪波动明显。",
                                     last_assessed_at=now - timedelta(days=2)),
            ])

        if db.query(MentalAlert).count() < 3:
            db.add_all([
                MentalAlert(student_id=2, risk_level="MEDIUM",
                            reason="本周情绪打卡 3 次低于 55 分，提到「怕考不出来」。",
                            evidence="「雅思考了三次，真的有点撑不住」（已脱敏）",
                            status="FOLLOWING", handler_id=2,
                            notified_at=now - timedelta(days=1), notified_to=2,
                            notify_channel="chat",
                            intervention="1. 24 小时内单独联系\n2. 拆解本周可完成的小任务",
                            created_at=now - timedelta(days=5)),
                MentalAlert(student_id=3, risk_level="MEDIUM",
                            reason="节假日前后出现明显孤独感表达，社交回避倾向。",
                            evidence="「放假一个人待着挺难受的」（已脱敏）",
                            status="CLOSED", handler_id=2,
                            notified_at=now - timedelta(days=6), notified_to=2,
                            notify_channel="chat",
                            intervention="1. 邀请加入同城学长社群\n2. 一周后回访",
                            created_at=now - timedelta(days=6)),
            ])

        db.commit()

        # 画像研判规则引擎：灌入示例规则（幂等）
        # ⚠️ 老库回填：这一行是本脚本后加的，早期建好的库不会自动补上 ——
        #    规则表为空时 `rules.load_active()` 返回空，研判就静默降级成
        #    「纯 Dify 判定」（rule_source=dify），示例文本会随模型抖动。
        #    所以这里显式判空补灌，和上面几块「老库缺哪块就补哪块」保持一致。
        if db.query(ScreeningRule).count() == 0:
            rules.seed_default_rules(db)

        # 学业考务：截止日期与阶段进度（幂等）
        if db.query(StudentDeadline).count() == 0:
            db.add_all([
                StudentDeadline(student_id=2, kind="EXAM", title="雅思考试（第 4 次）",
                                subject="IELTS", due_at=now + timedelta(days=12),
                                source="手动录入", remind_before_hours=72,
                                status="PENDING", note="目标 6.5，口语需重点补"),
                StudentDeadline(student_id=2, kind="DDL", title="研究计划书终稿",
                                subject="Research Proposal", due_at=now + timedelta(days=5),
                                source="手动录入", remind_before_hours=48,
                                status="PENDING", note="导师反馈 2 轮后定稿"),
                StudentDeadline(student_id=3, kind="VISA", title="学生签证面签",
                                subject="VISA", due_at=now + timedelta(days=21),
                                source="手动录入", remind_before_hours=120,
                                status="PENDING", note="需携带资金证明原件"),
                StudentDeadline(student_id=3, kind="INTERVIEW", title="导师线上面试",
                                subject="Interview", due_at=now + timedelta(days=3),
                                source="手动录入", remind_before_hours=24,
                                status="PENDING"),
            ])
        if db.query(StudentProgress).count() == 0:
            db.add_all([
                StudentProgress(student_id=2, phase="DOC", item="文书素材收集",
                                status="DONE", owner_id=1,
                                due_at=now - timedelta(days=10)),
                StudentProgress(student_id=2, phase="DOC", item="研究计划书撰写",
                                status="DOING", owner_id=2,
                                due_at=now + timedelta(days=5)),
                StudentProgress(student_id=2, phase="APPLY", item="确定选校清单",
                                status="TODO", owner_id=1,
                                due_at=now + timedelta(days=14)),
                StudentProgress(student_id=3, phase="VISA", item="签证材料准备",
                                status="DOING", owner_id=1,
                                due_at=now + timedelta(days=18)),
            ])
        db.commit()

        counts = {
            "employee": db.query(Employee).count(),
            "student": db.query(Student).count(),
            "customer_lead": db.query(CustomerLead).count(),
            "account": db.query(SysAccount).count(),
            "activity": db.query(Activity).count(),
        }
        print("[seed] 完成：", counts)
        print("[seed] 演示账号：admin/admin123  manager/manager123  "
              "advisor/advisor123  teacher/teacher123  student/student123")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="先 drop_all 再重建")
    args = parser.parse_args()
    seed(reset=args.reset)
