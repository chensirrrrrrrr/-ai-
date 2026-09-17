# docs/ —— 交付文档与产出溯源

本目录存放这个项目的**对人类可读的交付物**，以及与生成过程对应的溯源材料。

## 一、正式交付文档（.docx）

| 文件 | 说明 |
|---|---|
| `AI智能助手系统_需求规格说明书.docx` | 需求侧：角色、场景、功能清单、验收口径 |
| `AI智能助手系统_技术方案设计文档.docx` | 设计侧：架构、数据模型（含横向 ER 图页）、接口契约 |
| `AI智能助手系统_技术实现与部署文档.docx` | 实现侧：技术选型、核心机制、接口清单、部署步骤、测试结论 |

三份文档同源，都来自 `../客户需求表.xlsx`（桌面）。

## 二、`tech-doc-pipeline/` —— 技术文档的三阶段流水线溯源

《技术实现与部署文档》不是手写的，是走「创作 → 排版 → 转换」三阶段产出的，
这里保留每一步的中间产物，便于复现和追责：

```
tech-doc-pipeline/
├── pipeline-state.yaml          # 流水线状态机：各阶段输入/输出/角色/结论
├── params/topic.yaml            # Stage 0 解析出的写作参数（体裁/受众/篇幅）
├── research/…_research.md       # Stage 1 的轻量研究记录（Dify 集成、SQLAlchemy 方言等外部事实）
├── stage1/final_draft.md        # 终稿 Markdown（正文之源）
├── stage2/
│   ├── design_tokens.json       # 设计令牌（颜色/字号/间距，供 HTML 与 DOCX 对齐）
│   ├── formatted-…html          # 美化后的 HTML（HTML 质量门禁 100 分通过）
│   └── intermediate/            # 排版中间产物
├── stage3/                      # HTML → DOCX 转换阶段
├── working/docx_check.json      # 成品 docx 的结构校验（段落/表格/标题计数）
└── trace/pipeline.log           # 全流程日志
```

复现方式：走 tencent-docx 插件（技能 `tencent-docx`）的三阶段流水线
—— Stage 1 创作 Markdown → Stage 2 用它美化 HTML（过 html-review 质量门禁）
→ Stage 3 HTML 转 DOCX。每一步的输入输出在上面的目录里都能对上。

## 三、`SKILLS.md` —— 技能复盘与说明

本项目过程中沉淀下来的**可复用工作流**（技能）都记在 `SKILLS.md`：
复盘结论、每个技能的触发时机与内含、自带脚本、验证记录，以及"刻意不做"的部分。
技能本体放在用户级目录 `~\.workbuddy\skills\<技能名>\`，本文只做索引与说明。

## 四、注意

- 文档里的**所有数字都是实测值**（接口数、表数、测试通过数、覆盖率、耗时），
  不是估算。改代码后若要重新出文档，这些数字需要重新采集。
- 演示账号密码是弱口令，仅用于本地演示；上线前必须全部重置，`SECRET_KEY` 同理。
