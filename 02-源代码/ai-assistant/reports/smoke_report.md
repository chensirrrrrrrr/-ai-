# 冒烟测试报告

- 目标服务：`http://127.0.0.1:8010`
- 执行时间：2026-09-17 21:23:27
- 用例总数：**149**，通过 **149**，失败 **0**
- 累计请求耗时：38517 ms

| # | 用例 | 结果 | 耗时(ms) | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 健康检查 /health | ✅ PASS | 5.1 | HTTP 200 code=0 |
| 2 | 就绪探针 /ready | ✅ PASS | 1.8 | HTTP 200 code=0 |
| 3 | OpenAPI 文档 | ✅ PASS | 134.3 | HTTP 200 code=None |
| 4 | 登录（顾问） | ✅ PASS | 59.4 | HTTP 200 code=0 |
| 5 | 登录（管理层） | ✅ PASS | 58.5 | HTTP 200 code=0 |
| 6 | 登录（学生） | ✅ PASS | 58.4 | HTTP 200 code=0 |
| 7 | 访客 Token | ✅ PASS | 4.1 | HTTP 200 code=0 |
| 8 | 错误密码被拒（预期 401） | ✅ PASS | 56.2 | HTTP 401 code=40100 |
| 9 | 无 Token 被拒（预期 401） | ✅ PASS | 2.1 | HTTP 401 code=40100 |
| 10 | 客服问答（知识类，带引用） | ✅ PASS | 6900.4 | HTTP 200 code=0 |
| 11 |   └ 命中客服 Agent + 有引用 | ✅ PASS | 0.0 | agent=customer_service intent=company_info refs=4 |
| 12 | 权限隔离（访客说请假） | ✅ PASS | 7012.5 | HTTP 200 code=0 |
| 13 |   └ 被降级到客服 Agent | ✅ PASS | 0.0 | agent=customer_service confidence=0.5 |
| 14 | 学生请假意图命中 | ✅ PASS | 1626.4 | HTTP 200 code=0 |
| 15 | 流式对话 SSE | ✅ PASS | 2783.6 | HTTP 200 收到 91 个事件 |
| 16 | 语音录入（请假） | ✅ PASS | 6.0 | HTTP 200 code=0 |
| 17 |   └ 槽位抽取正确 | ✅ PASS | 0.0 | slots={"start_date": "2026-09-15", "days": 2, "reason": "看病"} |
| 18 | 语音录入（日报） | ✅ PASS | 4.6 | HTTP 200 code=0 |
| 19 | 新增意向客户 | ✅ PASS | 8.9 | HTTP 200 code=0 |
| 20 | 重复手机号被拒（预期 409） | ✅ PASS | 3.4 | HTTP 409 code=40900 |
| 21 | 客户列表分页查询 | ✅ PASS | 8.6 | HTTP 200 code=0 |
| 22 | 新增跟进记录（首次） | ✅ PASS | 8.5 | HTTP 200 code=0 |
| 23 | 跟进记录幂等（重复提交） | ✅ PASS | 3.1 | HTTP 200 code=0 |
| 24 |   └ 命中幂等返回同一条 | ✅ PASS | 0.0 | id=49 duplicated=True |
| 25 | 更新客户状态 | ✅ PASS | 4.8 | HTTP 200 code=0 |
| 26 | 客户研判（Dify 工作流） | ✅ PASS | 1330.1 | HTTP 200 code=0 |
| 27 |   └ 研判结论与置信度 | ✅ PASS | 0.0 | 结论=符合 置信度=1.0 |
| 28 | 材料上传解析（xlsx → 字段抽取） | ✅ PASS | 10.7 | HTTP 200 code=0 |
| 29 |   └ 关键字段抽齐且无缺失 | ✅ PASS | 0.0 | 解析器=builtin-xlsx 抽到 9 项 / 缺失 0 项 |
| 30 |   └ 材料原件可下载且字节一致 | ✅ PASS | 42.7 | HTTP 200 1702B |
| 31 | 材料上传解析（PDF 简历） | ✅ PASS | 92.9 | HTTP 200 code=0 |
| 32 | 材料上传对学生不可见（预期 403） | ✅ PASS | 3.3 | HTTP 403 code=40300 |
| 33 | 不支持的格式被拒（预期 400） | ✅ PASS | 3.5 | HTTP 400 code=40000 |
| 34 | 研判（仅给原件链接，服务端重解析） | ✅ PASS | 1410.3 | HTTP 200 code=0 |
| 35 |   └ 材料名回到原始文件名 | ✅ PASS | 0.0 | source_name=客户登记表.xlsx 结论=符合 |
| 36 | 人工复核：确认 AI 结论 | ✅ PASS | 8.2 | HTTP 200 code=0 |
| 37 |   └ 状态落为 CONFIRMED 且结论未变 | ✅ PASS | 0.0 | review_status=CONFIRMED conclusion=符合 |
| 38 | 新建一条用于推翻的研判 | ✅ PASS | 1042.6 | HTTP 200 code=0 |
| 39 | 人工复核：推翻并回写 | ✅ PASS | 6.2 | HTTP 200 code=0 |
| 40 |   └ 结论被覆写且 AI 原结论留痕 | ✅ PASS | 0.0 | 符合 → 不符合 |
| 41 | 人工修正清单（规则优化素材） | ✅ PASS | 20.6 | HTTP 200 code=0 |
| 42 |   └ 列出被推翻记录并按结论迁移聚合 | ✅ PASS | 0.0 | 45 条 / 迁移 ['信息不足 → 不符合', '信息不足，无法判断是否符合产品准入要求：材料仅提供学历（硕士）、均分（68）和意向国家（加拿大），缺少姓名、年龄、学校、专业、语言成绩和意向阶段等关键字段。 → 不符合'] |
| 43 | 批量研判（3 份材料 → 结果清单） | ✅ PASS | 3481.3 | HTTP 200 code=0 |
| 44 |   └ 批次号 + 成败计数 + 结果清单 | ✅ PASS | 0.0 | batch=batch-20260917212350-4aa405 结论分布={'符合': 2, '信息不足': 1} |
| 45 | 按批号回捞研判清单 | ✅ PASS | 27.3 | HTTP 200 code=0 |
| 46 |   └ 批内 3 条全部可查 | ✅ PASS | 0.0 | total=3 |
| 47 | 批量研判：单条失败被隔离 | ✅ PASS | 1961.0 | HTTP 200 code=0 |
| 48 |   └ 一条失败不带走其余两条 | ✅ PASS | 0.0 | 成功 2 / 失败 1 |
| 49 | 批量研判：超上限被拒（预期 400） | ✅ PASS | 3.3 | HTTP 400 code=40000 |
| 50 | 研判列表按复核状态过滤 | ✅ PASS | 4.5 | HTTP 200 code=0 |
| 51 | 学生列表 | ✅ PASS | 11.7 | HTTP 200 code=0 |
| 52 | 申请进度时间轴 | ✅ PASS | 8.7 | HTTP 200 code=0 |
| 53 | 越权读他人档案（预期 403） | ✅ PASS | 3.0 | HTTP 403 code=40300 |
| 54 | 提交请假申请 | ✅ PASS | 7.1 | HTTP 200 code=0 |
| 55 | 审批请假（通过） | ✅ PASS | 7.8 | HTTP 200 code=0 |
| 56 |   └ 状态流转 PENDING→APPROVED | ✅ PASS | 0.0 | status=APPROVED |
| 57 | 重复审批被拒（预期 409） | ✅ PASS | 3.0 | HTTP 409 code=40900 |
| 58 | 创建售后工单 | ✅ PASS | 5.7 | HTTP 200 code=0 |
| 59 | 更新工单状态 | ✅ PASS | 6.7 | HTTP 200 code=0 |
| 60 | 工单列表 | ✅ PASS | 4.5 | HTTP 200 code=0 |
| 61 | 心理预警（管理层可读） | ✅ PASS | 4.3 | HTTP 200 code=0 |
| 62 | 心理预警（普通员工被拒，预期 403） | ✅ PASS | 2.4 | HTTP 403 code=40300 |
| 63 | 更新预警跟进状态 | ✅ PASS | 5.4 | HTTP 200 code=0 |
| 64 | 活动列表 | ✅ PASS | 5.2 | HTTP 200 code=0 |
| 65 | 创建活动 | ✅ PASS | 5.3 | HTTP 200 code=0 |
| 66 | 活动报名 | ✅ PASS | 6.1 | HTTP 200 code=0 |
| 67 | 生成报告（Dify 工作流） | ✅ PASS | 40.7 | HTTP 200 code=0 |
| 68 | 报告详情 | ✅ PASS | 4.3 | HTTP 200 code=0 |
| 69 | 报告下载 | ✅ PASS | 3.2 | HTTP 200 1881B |
| 70 | 提交员工日报（语音来源） | ✅ PASS | 6.8 | HTTP 200 code=0 |
| 71 | 日报汇总 | ✅ PASS | 5.8 | HTTP 200 code=0 |
| 72 | 知识文档登记 | ✅ PASS | 5.8 | HTTP 200 code=0 |
| 73 | 组织架构树 | ✅ PASS | 4.2 | HTTP 200 code=0 |
| 74 |   └ 汇报线成树且覆盖全部在职员工 | ✅ PASS | 0.0 | 员工 3 人 / 根节点 2 / 部门 3 |
| 75 | 部门概览 | ✅ PASS | 2.8 | HTTP 200 code=0 |
| 76 | 员工花名册（关键字） | ✅ PASS | 5.1 | HTTP 200 code=0 |
| 77 |   └ 关键字命中唯一员工 | ✅ PASS | 0.0 | 命中 1 条 |
| 78 | 员工详情（含汇报关系） | ✅ PASS | 5.0 | HTTP 200 code=0 |
| 79 | 组织架构对学生不可见（预期 403） | ✅ PASS | 2.1 | HTTP 403 code=40300 |
| 80 | 新人入职指引（全文） | ✅ PASS | 2.3 | HTTP 200 code=0 |
| 81 |   └ 五阶段步骤齐全且每项都有负责人与入口 | ✅ PASS | 0.0 | V2026.09 · 5 阶段 / 19 项 / 12 FAQ |
| 82 | 入职待办清单（按阶段） | ✅ PASS | 2.1 | HTTP 200 code=0 |
| 83 | 新人常见问题检索 | ✅ PASS | 2.6 | HTTP 200 code=0 |
| 84 |   └ 整句提问也能召回 FAQ | ✅ PASS | 0.0 | 命中 1 条，首条 报到当天要带什么？ |
| 85 | 入职关键联系人（按登录人解析） | ✅ PASS | 2.9 | HTTP 200 code=0 |
| 86 |   └ 直属上级来自 employee.manager_id | ✅ PASS | 0.0 | 上级=陈总 |
| 87 | 入职指引知识条目已登记（category=NEO） | ✅ PASS | 3.9 | HTTP 200 code=0 |
| 88 | 对话：新人入职指引意图 | ✅ PASS | 2254.7 | HTTP 200 code=0 |
| 89 |   └ 路由到 onboarding 且答复带版本出处 | ✅ PASS | 0.0 | agent=enterprise_assistant intent=onboarding refs=4 |
| 90 | 主动待办清单（顾问） | ✅ PASS | 129.5 | HTTP 200 code=0 |
| 91 |   └ 四类业务待办 + 主动询问话术 + 不含敏感类 | ✅ PASS | 0.0 | 292 件 / 4 类 / 最高 high |
| 92 | 主动待办清单（管理层） | ✅ PASS | 19.3 | HTTP 200 code=0 |
| 93 |   └ 管理层待办 ⊇ 员工待办（角色超集） | ✅ PASS | 0.0 | 员工 4 类 / 管理层 4 类 |
| 94 | 心理预警（未触达计数） | ✅ PASS | 3.6 | HTTP 200 code=0 |
| 95 |   └ 有未触达预警时必现「待触达心理预警」 | ✅ PASS | 0.0 | 未触达 0 条 → 待办不含该类 |
| 96 | 待办清单对访客不可见（预期 403） | ✅ PASS | 1.9 | HTTP 403 code=40300 |
| 97 | 手动推送一轮待办 | ✅ PASS | 21.2 | HTTP 200 code=0 |
| 98 |   └ 推送落库（留痕） | ✅ PASS | 0.0 | pushed=0 records=1 |
| 99 | 重复推送命中频控（幂等） | ✅ PASS | 25.1 | HTTP 200 code=0 |
| 100 |   └ 同一时间窗不重复提醒同一个人 | ✅ PASS | 0.0 | pushed=0 duplicated=True |
| 101 | 全员推送限管理层（预期 403） | ✅ PASS | 3.7 | HTTP 403 code=40300 |
| 102 | 推送记录（只看自己） | ✅ PASS | 7.6 | HTTP 200 code=0 |
| 103 |   └ 记录只含本人 + 未处理计数 | ✅ PASS | 0.0 | 25 条 / 未处理 9 条 |
| 104 | 标记待办已处理 | ✅ PASS | 5.0 | HTTP 200 code=0 |
| 105 |   └ 幂等标记（重复调用不报错） | ✅ PASS | 0.0 | already=True |
| 106 | 心理预警触达（未触达全量） | ✅ PASS | 4.3 | HTTP 200 code=0 |
| 107 |   └ 已触达的不重复写（幂等） | ✅ PASS | 0.0 | 没有需要触达的预警（要么都已触达，要么没有未关闭的预警） |
| 108 | 预警触达限管理层（预期 403） | ✅ PASS | 2.6 | HTTP 403 code=40300 |
| 109 | 预警列表按触达状态过滤 | ✅ PASS | 2.5 | HTTP 200 code=0 |
| 110 | 对话：主动待办推送意图 | ✅ PASS | 1803.9 | HTTP 200 code=0 |
| 111 |   └ 路由 todo_push 且答复是「先问再答」 | ✅ PASS | 0.0 | agent=enterprise_assistant intent=todo_push |
| 112 | 对话：心理预警汇总（管理层） | ✅ PASS | 1883.2 | HTTP 200 code=0 |
| 113 |   └ 路由 alert_digest 且给出建议动作 | ✅ PASS | 0.0 | intent=alert_digest |
| 114 | 对话：取数问法仍走 NL2SQL 模板（不被新规则截胡） | ✅ PASS | 3608.1 | HTTP 200 code=0 |
| 115 | 报告口径清单（五类） | ✅ PASS | 3.6 | HTTP 200 code=0 |
| 116 |   └ 五类报告齐全且带默认周期 | ✅ PASS | 0.0 | customer_ops、daily_digest、weekly_digest、mental_weekly、complaint_weekly |
| 117 | 生成报告：全域客户经营分析 | ✅ PASS | 51.0 | HTTP 200 code=0 |
| 118 |   └ 真实聚合：报告期 + 指标 + 结论 | ✅ PASS | 0.0 | #339 2026-09-11 ~ 2026-09-17 指标 6 项 |
| 119 | 生成报告：日报汇总（日） | ✅ PASS | 35.7 | HTTP 200 code=0 |
| 120 |   └ 真实聚合：报告期 + 指标 + 结论 | ✅ PASS | 0.0 | #340 2026-09-17 ~ 2026-09-17 指标 6 项 |
| 121 | 生成报告：日报汇总（周） | ✅ PASS | 38.3 | HTTP 200 code=0 |
| 122 |   └ 真实聚合：报告期 + 指标 + 结论 | ✅ PASS | 0.0 | #341 2026-09-11 ~ 2026-09-17 指标 6 项 |
| 123 | 生成报告：心理健康周报 | ✅ PASS | 46.1 | HTTP 200 code=0 |
| 124 |   └ 真实聚合：报告期 + 指标 + 结论 | ✅ PASS | 0.0 | #342 2026-09-11 ~ 2026-09-17 指标 6 项 |
| 125 | 生成报告：投诉处理周报 | ✅ PASS | 47.1 | HTTP 200 code=0 |
| 126 |   └ 真实聚合：报告期 + 指标 + 结论 | ✅ PASS | 0.0 | #343 2026-09-11 ~ 2026-09-17 指标 6 项 |
| 127 | 生成报告（兼容旧值 weekly） | ✅ PASS | 38.0 | HTTP 200 code=0 |
| 128 | 未知报告类型被拒（预期 400） | ✅ PASS | 3.3 | HTTP 400 code=40000 |
| 129 | 报告导出：Excel | ✅ PASS | 7.6 | HTTP 200 6504B |
| 130 | 报告导出：PDF | ✅ PASS | 8.5 | HTTP 200 9264B |
| 131 | 报告导出：打印版 HTML | ✅ PASS | 5.3 | HTTP 200 5679B |
| 132 | 报告导出：Markdown | ✅ PASS | 5.3 | HTTP 200 2815B |
| 133 | 导出格式不支持被拒（预期 400） | ✅ PASS | 3.6 | HTTP 400 code=40000 |
| 134 | 报告导出限管理层（预期 403） | ✅ PASS | 6.3 | HTTP 403 code=40300 |
| 135 | 手动触发定时报告生成 | ✅ PASS | 12.7 | HTTP 200 code=0 |
| 136 |   └ 幂等：重复触发只跳过、不重复生成 | ✅ PASS | 0.0 | 生成 0 / 跳过 5 |
| 137 | NL2SQL：客户线索统计 | ✅ PASS | 6.6 | HTTP 200 code=0 |
| 138 |   └ 命中受控模板并返回预览 SQL | ✅ PASS | 0.0 | 模板=lead_status_stats 行数=4 |
| 139 | NL2SQL：按姓名查跟进 | ✅ PASS | 6.2 | HTTP 200 code=0 |
| 140 |   └ 人名参数抽取正确 | ✅ PASS | 0.0 | SELECT l.name AS customer, f.follow_type, f.content, f.created_at FROM customer_ |
| 141 | NL2SQL：越权查敏感模板（预期 403） | ✅ PASS | 4.0 | HTTP 403 code=40300 |
| 142 | 审计日志（管理层） | ✅ PASS | 4.8 | HTTP 200 code=0 |
| 143 | 审计日志（员工被拒，预期 403） | ✅ PASS | 3.2 | HTTP 403 code=40300 |
| 144 | 工具清单 | ✅ PASS | 1.9 | HTTP 200 code=0 |
| 145 | 工具：查客户（签名校验通过） | ✅ PASS | 5.1 | HTTP 200 code=0 |
| 146 | 工具：查成绩 | ✅ PASS | 4.0 | HTTP 200 code=0 |
| 147 | 工具：待审批申请 | ✅ PASS | 2.9 | HTTP 200 code=0 |
| 148 | 工具：NL2SQL | ✅ PASS | 1.9 | HTTP 200 code=0 |
| 149 | 工具：伪造签名被拒（预期 403） | ✅ PASS | 1.3 | HTTP 403 code=40300 |

结论：**全部通过**
