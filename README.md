# 留学机构 AI 智能助手系统 · 交付包

- **整合时间**：2026-09-15 20:5x ｜ **最近更新**：2026-09-16（Dify 切 live、端口约定、基线刷新）
- **整合方式**：**纯复制** —— 原始文件全部保留在原处，本包是可独立带走的完整副本
- **项目根（主目录）**：`C:\Users\机械革命\Desktop\留学机构AI助手系统_交付\02-源代码\ai-assistant\`
  > ⚠️ 原 `2026-09-12-11-04-16\`（WorkBuddy 下 / 桌面）**均已不存在**。
  > 本包的 `02-源代码\` 即 live 工作区，**不是冻结快照** —— 后续开发直接在这里改。

---

## 一、目录结构

```
留学机构AI助手系统_交付\
├── README.md                    ← 本文件
├── 01-需求与技术文档\            ← 甲方需求 + 设计/实现文档
│   ├── 客户需求表.xlsx
│   ├── AI智能助手系统_需求规格说明书.docx      (SRS V1.0)
│   ├── AI智能助手系统_技术方案设计文档.docx    (TDD V1.0)
│   ├── AI智能助手系统_技术实现与部署文档.docx
│   └── 回填前备份\              ← 上述 docx 在「回填前」的版本快照
├── 02-源代码\
│   └── ai-assistant\            ← 项目主体（含交付报告、项目级技能）
├── 03-设计素材\
│   ├── 留学机构AI助手系统_模块总览.html
│   └── 留学机构AI助手系统_模块总览.png
└── 04-项目记忆\                  ← 项目开发过程记忆（按日归档）
    ├── 2026-09-12.md
    ├── 2026-09-13.md
    ├── 2026-09-14.md
    ├── 2026-09-15.md
    ├── 2026-09-16.md
    └── MEMORY.md
```

---

## 二、验证基线（2026-09-16 复测）

| 验证项 | 结果 |
| --- | --- |
| 单元 / 集成测试（pytest） | **415 / 415** |
| 一键自检（`--selfcheck`） | **13 / 13** |
| 接口冒烟测试 | **149 / 149** |
| 浏览器端到端（E2E） | **37 / 37** |
| Dify 接线预检（`check_dify.py`） | **7 / 7 [OK]** |

**一条命令复现全部基线**：

```bash
python scripts/launcher.py --selfcheck --e2e
```

> ⚠️ E2E 必须在**同一进程内**跑。自检流程会在服务起来后同进程执行 E2E，跑完再收服务。
> 不要用 `&` 后台挂起 launcher —— stdout 管道不收口会假死，服务也会被连带杀掉。
>
> ⚠️ 本机 curl 结果不可信（Windows schannel 后端会假报 `http=000`）。判断连通性请用 Python urllib 或 `scripts/check_dify.py`。

### 端口约定（🔴 以 `scripts/launcher.py` 为唯一权威源）

后端 **8010**、前端 **8020**。各脚本的默认端口必须跟随 launcher，**不得自己硬编码**。

```bash
python scripts/launcher.py --headless --no-browser   # 起服
python scripts/launcher.py --status                  # 查状态
python scripts/smoke_test.py                         # 冒烟（--base 默认自动发现端口）
python scripts/launcher.py --stop                    # 停服
```

> 事故记录：`smoke_test.py` 的 `--base` 默认值曾写死 `8000`，而 launcher 起在 8010 ——
> 手工跑冒烟会全部打空、149 条集体 FAIL。已修为自动发现，并由
> `tests/test_port_contract.py`（8 条）钉住，防止再次漂移。

---

## 三、项目内技能（7 个）

这些技能放在 `02-源代码\ai-assistant\.workbuddy\skills\` 下，是**项目级技能**，
在本项目内工作时可被直接加载。

| 技能 | 用途 |
| --- | --- |
| `zero-build-frontend-verify` | 验证「零构建前端」（原生 ES Module，无打包器）真能跑：JS 语法 + import 一致性 + 浏览器端到端截图 |
| `windows-python-one-click-launcher` | 给 Windows 上的 Python Web 项目做「双击即跑」：找解释器、查依赖、建库灌种子、挑端口、探活 |
| `http-smoke-baseline` | 对真实运行中的服务发请求，建 HTTP 冒烟基线（健康/登录/业务链路/越权/参数校验/幂等） |
| `idempotent-demo-seed` | 写幂等演示数据播种脚本，可反复灌、断言有稳定预期 |
| `mock-first-external-service` | 接外部 SaaS 时用「本地 mock 优先 + 显式降级 + 切 live 前接线预检」 |
| `additive-column-migration` | 给已有真实数据的库加字段（只加不改的 ADD COLUMN），附回归测试写法 |
| `zero-dependency-doc-export` | 不引第三方库，用标准库导出 Excel(xlsx) 与 PDF（含 CJK 字体坑） |

> 同一批技能也存在于用户级目录 `~\.workbuddy\skills\`（跨项目可用）。
> **项目级与用户级同名并存，加载优先级存在歧义** —— 若行为不符预期，可移除项目内副本。
>
> 2026-09-16 已把 `windows-python-one-click-launcher` 的最新版同步到项目级副本
> （新增「沙箱内让服务常驻的正确解法」「后台任务不跨轮次保活」「端口默认值漂移」三节），两处内容一致。

---

## 四、交付报告

项目内 `02-源代码\ai-assistant\reports\`：

| 文件 | 内容 |
| --- | --- |
| `验收基线汇总.md` | **入口文档**：四条基线 + 文件清单 + 需求完成度 |
| `需求完成情况查验报告.html` / `.md` | 主报告：首次查验结论 + 修复进展复核表 |
| `smoke_report.md` | 149 条接口冒烟用例逐条结果 |
| `frontend_e2e_report.md` / `.json` | 37 条端到端用例逐条结果 |
| `frontend_shots\` | E2E 各步骤截图 |

---

## 五、当前状态

**功能需求 34 条（M1–M5）**：✅ 已实现 22 / 🟡 部分实现 5 / ⚪ 依赖外部接线 7 / 🔴 未实现 0

首次查验发现的 3 条 🔴 已全部闭环：

| 原空档 | 修复落点 |
| --- | --- |
| REQ-M1-03 画像研判规则导入与版本化 | `app/services/rules.py` |
| REQ-M4-04 学业考务（DDL + 考前提醒） | `app/services/deadline.py` |
| 报告定时推送（SRS 4.5.3 / AC-08） | `reports.run_scheduled_with_push()` + `app/services/notify.py` |

**2026-09-16 追加闭环**（Dify 切 live 后的收口）：

| 项 | 修复落点 |
| --- | --- |
| Dify 真链路接入（7 应用 + 知识库） | 本机 Dify 1.10.0，`.env` 置 `DIFY_MODE=live`；`check_dify.py` 7/7 [OK] |
| 知识库引用恒为空（`refs=0`） | `model-config` 的 `dataset_configs` 补 `enabled: true` → `refs=4` |
| 研判结论抖动 | `screening_rule` 表回填（`seed.py` 补判空守卫）→ `rule_source=local` 确定 |
| `intent` 返回整句而非短标识 | router workflow 的 schema 加 17 值 enum + 提示词约束 |
| `smoke_test.py` 默认端口漂移 | `--base` 改自动发现 + `tests/test_port_contract.py` 守护 |

**未闭环项**（生产化非功能能力，已拆为 14 条事项跟踪，分 P0/P1/P2 三级）：
HTTPS、字段级加密、限流熔断、内容安全过滤、监控看板、撤销机制、渠道接入、
CI/CD、Redis 会话记忆、向量库 / RAG / Rerank、Prompt 版本化、行级数据隔离、
性能压测与渗透测试。

> ✅ 原列表中的「**Dify 切 live 真链路回归**」已于 2026-09-16 完成并移出未闭环清单。

---

## 六、注意事项

- **`.env` 含凭据**（Dify 密钥等）。本包按「完整副本」原则包含该文件，**对外分发前请先剔除或脱敏**。
- 本包为**复制**产物，非移动。原工作区 `2026-09-12-11-04-16\` 未被改动。
- `ai-assistant.zip`（09-14 旧快照，落后于当前代码）**未纳入**本包，仍在原工作区。
- 复制时排除了 `__pycache__` / `*.pyc` / `.pytest_cache` / `.coverage` / `.run` 等临时产物，
  其余 **738 个文件**与原件逐一对应。
- **2026-09-16 后续变更**（02 目录是 live 工作区，以下为整合后的增量）：
  - 新增 `tests/test_port_contract.py`（端口契约，8 条）
  - 修改 `scripts/smoke_test.py`（`--base` 改自动发现端口）、`scripts/seed.py`（规则播种补判空守卫）
  - 新增 `tests/test_dify_live.py` 中 1 条健康检查契约用例
  - 04-项目记忆 补入 `2026-09-15.md` / `2026-09-16.md`，`MEMORY.md` 更新至最新
  - 各改动文件均留有 `.bak-<时间戳>` 备份（项目**无 git**，备份是唯一回退手段）
