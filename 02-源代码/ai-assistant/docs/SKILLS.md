# 技能（Skill）复盘与说明

本文档回答三件事：

1. 这个项目里**哪些地方值得沉淀成技能**（以及哪些刻意不做）；
2. 每个技能的**定位、触发时机、内含什么、怎么验证**；
3. 怎么用、放在哪、后续怎么维护。

> 技能存放位置：`~/.workbuddy/skills/<技能名>/`
> 即 `C:\Users\机械革命\.workbuddy\skills\`（用户级，跨项目可用）
> 每个技能一个目录，入口是 `SKILL.md`，可执行资产放 `scripts/`。

---

## 一、复盘结论总览

| 技能 | 来源 | 状态 | 一句话定位 |
|---|---|---|---|
| `windows-python-one-click-launcher` | 本项目 | 已有 | Windows 上 Python Web 项目「双击即跑」一键启动方案 |
| `zero-build-frontend-verify` | 本项目 | 已有 | 零构建前端（原生 ESM）的三层验证与浏览器 E2E |
| `mock-first-external-service` | 本项目 | **本次新增** | 外部 SaaS 的 mock/live 双模接入 + 切 live 前接线预检 |
| `idempotent-demo-seed` | 本项目 | **本次新增** | 可反复执行的演示数据播种 |
| `http-smoke-baseline` | 本项目 | **本次新增** | 对真实服务的 HTTP 冒烟基线 + 报告 |
| `workbuddy-session-triage` | 本项目过程中 | **本次新增** | 诊断并修复 WorkBuddy 会话「一直转圈」 |
| `dify-workflow-draft-node-repair` | 本项目过程中 | **本次新增** | 修 Dify 画布节点变细白条 / 误报缺必需节点 |
| `dify-kb-embedding-switch` | 本项目过程中 | **本次新增** | Dify 知识库嵌入模型原地切换（含索引重建与命中验证） |
| `api-implementation-audit` | 本项目过程中 | **本次新增** | 交付文档 vs 真实代码「三查」：功能对账 / AST 死代码 / 覆盖定位 |
| `additive-column-migration` | 本项目 | 已有 | 给已有真实数据的库加字段而不重建（ADD COLUMN 补列 + 老库回归测试） |
| `zero-dependency-doc-export` | 本项目 | 已有 | 零依赖导出 Excel(xlsx) / PDF（标准库手写最小结构） |

> **本次回填（2026-09-16 晚）**：新增 `dify-workflow-draft-node-repair` 并同步到交付包
> `.workbuddy/skills/`；`mock-first-external-service` §8.1「接外部服务的高频坑」由 6 个扩到
> **7 个**（新增「Dify 画布节点顶层 `type` 必须写 `custom`」）；同时把 `additive-column-migration`、
> `zero-dependency-doc-export` 也纳入交付包技能副本，本索引一并登记。

> **本次回填（2026-09-17）**：知识库嵌入模型由本地 `Ollama + bge-m3` 切到**智谱云端
> `embedding-3`**，「本地模型已停用、Ollama 可停」这一结论同步进
> `04-项目记忆/MEMORY.md`、`reports/验收基线汇总.md` 与技术文档（§5.3 新增「模型接入」「知识库与嵌入模型」两段、附录 B 新增「模型接入」行）。
> 由此沉淀出技能 **`dify-kb-embedding-switch`**（见 2.6）。

> **本次回填（2026-09-17 晚）**：对交付文档做「功能是否都实现 / 代码是否冗余 / 各功能是否有 bug」三查，
> 产出 `reports/代码实现与测试查验报告.md`；随后 5 项整改**同日全部落地**（回填接口清单 21 条 +
> 删 20 个死 schema + `cancel()` 补接线 + 移出临时物 + 补 25 条单测），全量回归
> **pytest 472/472（91%）/ smoke 149/149 / E2E 37/37**；再补工具层分支 30 条 → **502/502（93%）**；行级隔离修复 +1 条 → **503/503（93%）**；限流+监控探针 +9 条 → **512/512（93%）**；字段加密+内容安全 +19 条 → **531/531（93%）**；工具行级收敛 +2 条 → **533/533（93%）**。
> 由此沉淀出技能 **`api-implementation-audit`**（见 2.7）。

判定一条经验值不值得做技能，用三个问题过筛：

1. **它是"可执行的工作流"吗？** —— 只描述代码风格的是约定，不是技能。
2. **换个项目还成立吗？** —— 只对本项目成立的是文档内容（写进 README）。
3. **现有的技能/插件覆盖了吗？** —— 覆盖了就别重复造，只补差异部分。

---

## 二、本次新增的 7 个技能

### 2.1 `mock-first-external-service` —— 外部服务 mock 优先双模接入

**解决的真实问题**：外部 SaaS（本例是 Dify，7 个应用）没就绪时开发不能停，
于是加了一层「连不上就降级到 mock」。但这个兜底有致命副作用——
**Key 写错 / 地址写错 / 应用没建，用户拿到的是 mock 回答，却以为已经接上了**，
从答复里根本看不出来。

**触发时机**：要接入会调外部 API 的服务、外部服务还没就绪、
或者出现「配了 Key 但回复不对」时。

**核心内容**：

| 部分 | 要点 |
|---|---|
| 三条铁律 | mock 是一等公民（有完整本地实现，不是占位符）／降级必须可观测／切 live 前必须能证明 |
| 配置形状 | `is_x_live` / `x_key(name)` / `x_missing_keys` / `x_live_ready` 四个派生属性收口判断逻辑 |
| ⚠️ 复杂 env 字段坑 | `List`/`Dict` 字段必须标 `NoDecode`，否则 `.env` 里写逗号分隔会让**进程起不来**；且这坑只在有 `.env` 时才炸，测试会假绿 |
| ⚠️ 代理坑 | 本机常驻全局代理时，`httpx` 会把发往 127.0.0.1 的请求也交给代理 → 502 → 被降级吞掉。按目标是否内网自动决定 `trust_env` |
| preflight 设计 | 5 种结论分类、无副作用的探针（workflow 用**故意不合法**的请求体）、Key 脱敏、退出码分层、给结论配自查顺序 |
| 双模测试 | `httpx.MockTransport` 注入，每条判定分支都能不联网单测 |

**自带脚本**：

- `scripts/preflight_external_service.py` —— 通用接线预检（多应用逐个探 + `--json` + 退出码 0/1/2）
- `scripts/settings_env_regression_test.py` —— 「用真实 `.env.example` 构造 Settings」的回归测试模板

> 本次更新：§8.1「接外部服务的高频坑」由 6 个扩到 **7 个** —— 新增第 7 坑
> 「Dify 画布节点顶层 `type` 必须写 `custom`，节点种类放 `data.type`，否则编辑器渲染成细白条」。

---

### 2.2 `idempotent-demo-seed` —— 幂等演示数据播种

**解决的真实问题**：播种脚本写不好会以最难查的方式暴露——
**跑第二遍主键冲突**、**测试偶发失败**（被上一轮污染）、**E2E 断言飘**（数据每次不一样）。

**触发时机**：需要准备演示/测试数据，或遇到「跑第二遍就报主键冲突」「测试数据偶发不一致」。

**核心内容**：

- 三条硬要求：幂等（连跑 3 次计数一致）／可重置（`--reset`）／可断言（计数回报 + 固定账号表）
- **守卫式插入**：按**自然键**逐条判，而不是「表非空就整块跳过」（后者会导致新增种子在老库上永远补不上）
- ⚠️ **别硬编码自增 id**：`insert → flush → 用自然键查回来 → 拿 id 建关联`（本项目最初的 seed 里就有这个雷）
- 数据必须**确定**：不用 `random`；和"现在"有关的字段用**固定偏移**而非绝对日期
- 演示账号列成**唯一真相表**，README / 前端演示卡片 / E2E / conftest 都从它派生
- 让 pytest **复用同一个 seed**（测试库独立 + `-wal`/`-shm` 一起清 + 环境变量必须在 `import app` 之前设）

**自带脚本**：`scripts/seed_template.py`

---

### 2.3 `http-smoke-baseline` —— 真实服务的 HTTP 冒烟基线

**解决的真实问题**：`pytest` + `TestClient` 是**进程内**调用，抓不到
「启动脚本 / 端口 / CORS / 静态资源 / 环境变量」这一类**只有真起服务才暴露**的问题。
两套都要有：pytest 保证逻辑，冒烟保证"真的跑起来了"。

**触发时机**：验收一个刚跑起来的服务、上线前自查、想让一键启动脚本自带体检。

**核心内容**：

- `check()` 助手四要点：HTTP 状态码**和**业务 code 双校验（非统一信封端点用 `expect_code=None`）；
  异常兜底成一条 FAIL 而不是崩掉整轮；每条记耗时；退出码 = 有失败就非 0
- 用例覆盖顺序：存活/就绪 → 规范文档 → **各角色**登录 → 鉴权边界（401/403/越权后数据未变）
  → 主链路 → 参数校验 422 → 幂等性 → 写后读一致性 → 善后
- 造数**必须带 uuid 后缀**，否则撞唯一约束或改到别人的数据
- 产出 `reports/smoke_report.md`（逐条 + 耗时 + ✅/❌），报告比终端输出重要
- 挂到一键启动器的 `--selfcheck` 里，分层：接口检查失败立刻停，别浪费 30 秒跑冒烟

**自带脚本**：`scripts/smoke_test_template.py`

---

### 2.4 `workbuddy-session-triage` —— 会话卡住诊断

**解决的真实问题**：侧边栏条目一直转圈，看起来像"有后台任务卡住了"。
实测根因是**用户删掉自动化时没有回收它的后台会话**，`sessions.status` 留在 `working`，
一挂 4 天、熬过多次重启也没被回收（应用启动时没有 reconcile 逻辑）。

**触发时机**：用户说侧边栏一直转圈、会话卡住、任务卡在 `working`、或有后台任务疑似没退出。

**核心内容**：

- 先纠正直觉：**转圈 ≠ 有后台进程**；而且转圈的那个很可能就是当前这场对话
- 数据位置：`sessions` 表关键字段、`sessions/<pid>.json` 心跳、`automation_runtime_state.running`
- **「真卡住」四件套判据**（必须同时成立才动手）：自动化没在跑 + 找不到进程 + 心跳不跳 + 库时间停
- 修复四条安全约束：**backup API 备份**（不是裸 `cp`）／只改 `status` 一列／
  `WHERE` 必带 `AND status='working'` 兜底／**保留 `updated_at` 原值**
- 🔴 **绝不触碰** `automations` 系列表（只能走 UI 或 `automation_update` 工具）
- 误判识别法：「某串文案是不是应用内置 UI 文案」→ 在 `app.asar` / `locales/*.pak` 里
  **按字节搜**；只在 `~/.workbuddy/traces/**` 命中 ⇒ 那是**模型生成的文本**（如会话标题）

**自带脚本**：

- `scripts/triage_sessions.py` —— 只读排查（状态分布 + working 明细 + 四件套并排 + 建议）
- `scripts/fix_stuck_sessions.py` —— 备份 + 受控修正（默认 dry-run，需 `--apply`）

**验证记录**：2026-09-14 在真实库上跑通 ——
正确识别出当前对话为 `alive`（`pid=26120` 存活、心跳 28s 前、`updated` 1s 前），
并把两个已软删自动化的历史会话列为可疑但不误杀。过程中修掉了两个自身缺陷：
`tasklist` 中文输出用 utf-8 解码会崩（改为 bytes 匹配）、
同一 `sessionId` 存在多份历史心跳时取了旧的那份（改为取最新）。

---

### 2.5 `dify-workflow-draft-node-repair` —— Dify 画布节点显示异常修复

**解决的真实问题**：用 API 建出来的 Dify 工作流，在网页编辑器里节点全变成
**150×21 的细白条**、点不进去，检查清单还误报「必须添加直接回复节点」。
真因很反直觉：**Dify 画布只注册了一个 ReactFlow 节点组件（键名 `custom`）**，
真正的节点种类放在 `data.type`；若用 API 建图时把**顶层 `type`** 写成块类型
（`answer` / `llm` / `http-request` …），前端在 `nodeTypes` 里查不到这个键，
就退化成 ReactFlow 内置的 default 节点（默认样式 `padding:10px; width:150px`）——
实测量出来正好是 **150×21 白条**。检查清单也只统计 `type === 'custom'` 的节点，
于是把 answer 节点全漏掉、误报缺「直接回复」。

**触发时机**：用脚本/API 建 Dify 工作流后，编辑器里节点显示成细白条或点不进去；
或检查清单误报缺少必需节点。

**核心内容**：

- 🔴 **顶层 `type` 必须恒为 `"custom"`**，节点种类一律写进 `data.type`
  （对应「后端执行只读 `data.type`」—— 已发布版本一直正常，纯编辑器渲染问题）
- 判别法：查 `workflows` 表，draft 节点尺寸被前端回写/塌成 `150x21`；published 版本正常
- 治法：把顶层 `type` 批量改回 `"custom"`，尺寸按 `data.type` 归一化；
  **不要**只去改 `width`/`height`（治标，前端再打开又被塌）
- ⚠️ 改 draft 前必须让用户**先关掉编辑器页**（旧页面里的内存图会覆盖回旧值）；
  改完让用户 `Ctrl+Shift+R` 强刷
- **不动已发布版本**（后端只认 `data.type`，动它反有风险）

**自带脚本**：

- `scripts/dify_draft_dims.py` —— 主修顶层 `type` + 按 `data.type` 归一化尺寸
- 交付包内的独立版：`scripts/check_dify_node_type.py`（凭据读环境变量，`--apply` 才写）

**验证记录**：2026-09-16 定位并修复 4 个工作流 draft（学生助手 Chatflow 18 节点 +
报告 / 筛选 / 路由分发各 3 节点），回读校验 `top-level type = {custom:18}`、`data.type` 原样、
prompt / model / 变量引用 / 分类器 6 类全部完好。

### 2.6 Dify 控制台 API 与工具链路身份（本次新增要点）

**控制台 API（`/console/api/*`）的三条硬规矩**：

- 🔴 **读写都要带 `X-CSRF-Token`**，值 = 登录后种下的 `csrf_token` cookie（只有写操作需要是误区，实测 GET 也会 401）
- 127.0.0.1 **必须禁代理**（`urllib` 用 `ProxyHandler({})`、`httpx` 用 `trust_env=False`）
- 只有 `workflow` / `advanced-chat` 有草稿图；普通 `chat` 应用取 `workflows/draft` 会
  `404 App mode is not in the supported list` —— 盘点时按「不适用」跳过，别当失败

**工具链路的身份传递**（行级收敛之后的配套）：

- 工具一律走 **HTTP 请求节点**，body 里硬写 `"actor": "{{#sys.user_id#}}"`；
  **不要走「自定义工具（OpenAPI）」** —— 那条路身份只能靠 LLM 填参数，不可靠
- `sys.user_id` 形如 `uas-advisor`（`dify_user_prefix` + 登录账号名），后端
  `_operator_employee_id()` 会自动剥前缀查账号，Dify 侧无需做字符串处理
- 规范由 `scripts/check_dify_tool_actor.py` 守：调 `lead_query` / `lead_lookup`
  却没带 `actor` → 判红（退出码 1）；凭据只读环境变量，未配置则跳过（退出码 2）
- **实测结论**：现有 9 个应用里只有「学生助手 Chatflow」调工具，且是
  `my_scores` / `my_requests` / `my_tickets` 三个学生视角只读工具，
  **没有任何节点调 `lead_query`** ⇒ 行级收敛对当前 live 链路零影响

---

### 2.6 `dify-kb-embedding-switch` —— Dify 知识库嵌入模型原地切换

**解决的真实问题**：项目早期把知识库嵌入放在本机 `Ollama + bge-m3` 上，于是「演示机必须常驻一个本地模型进程」。
想换成云端、把这一层依赖彻底去掉时，真正难的不是「改一个模型名」，而是三件容易被忽略的事：
**换模型等于换向量空间**（bge-m3 维度 1024、智谱 `embedding-3` 维度 2048，旧向量在新空间里没有意义）、
**必须整体重建索引**，以及**怎么证明真的换成功了**。

**触发时机**：要给 Dify 的知识库换嵌入模型（换供应商 / 本地换云端），
又不希望重建知识库、重新登记文档、改动画布上的检索节点。

**核心内容**：

- **原地切换为什么可行**：`dataset_id` 不变 ⇒ 画布上的知识检索节点与后端的知识文档登记都无需改动；
  平台在更新数据集配置时**自动触发全量重建**（`deal_dataset_vector_index_task`）。
- 🔴 **PATCH 数据集的硬约束**：请求体必须同时带 `indexing_technique` + `embedding_model` + `embedding_model_provider`
  三个字段。更新配置的处理函数**直接读 `indexing_technique`**，漏了就 KeyError → 接口回 **500**（不是 422 参数校验），
  很容易被误判成平台故障。
- **装插件 ≠ 可用**：先装模型供应商插件（marketplace 通道，带 `plugin_unique_identifier` 并轮询到 `success`），
  再单独 `POST .../credentials` 写 `api_key`，最后确认目标模型出现在 `text-embedding` 列表里。
- 🔴 **验证必须用命中测试，不能只看配置回显**：Weaviate 集合名只由 `dataset_id` 决定，
  **维度变化不会体现在集合名上**；配置变更与重建也是两件事。
  判据是「文档索引全部 `completed`」**且**「`hit-testing` 能召回相关内容」。
- **回滚只要反向 PATCH**（同样自动重建）⇒ 变更前务必记下原 provider/model。

**自带脚本**：

- `scripts/kb_embed_switch.py` —— 一条龙：看现状 → PATCH → 轮询重建 → hit-testing 验证；
  凭据走环境变量，默认 `--dry-run`，`--apply` 才写。

**验证记录**：2026-09-17 把「留学机构业务知识库」「AI课程知识库」两个数据集从 `bge-m3(ollama)`
切到智谱 `embedding-3`；文档全部 `completed`，命中测试返回相关片段（score 0.45~0.56），
知识库检索节点零改动。切换后全链路不再引用 Ollama。

---

### 2.7 `api-implementation-audit` —— 交付文档 vs 真实代码「三查」

**解决的真实问题**：交付前要回答三个问题——**功能都实现了吗？代码有冗余吗？各功能有没有 bug？**
凭读代码"感觉"答不出可复核的结论，必须给出**机器证据**。

**触发时机**：用户拿着需求/技术文档问"这些功能都实现了吗 / 代码有没有冗余 / 测一遍有没有 bug"；
或交付、答辩、验收前需要做一次实现侧自证。

**核心内容（三查各有独立证据源）**：

| 查什么 | 证据源 | 关键手法 |
|---|---|---|
| ① 功能完整性 | `app.openapi()["paths"]` 真实路由清单 | 与文档接口表逐条 diff；🔴 **路径参数先归一成 `{}`**（否则 `/leads/{id}` vs `/leads/{lead_id}` 误报"缺失"）；区分 `doc-real`（真缺失）与 `real-doc`（文档滞后） |
| ② 代码冗余 | AST 静态扫描 | `response_model=` 计数、未引用 schema、未用导入、未调用函数、散落临时物 |
| ③ 功能缺陷 | pytest+覆盖率 / HTTP 冒烟 / 浏览器 E2E 三层 | **看低覆盖模块**而非总分；死函数与低覆盖行交叉印证 |

**沉淀的三个硬坑**：

- 🔴 **判死代码必须算传递可达闭包**：只做"排除自身文件"的朴素扫描会把"只被同文件其他类引用"的类误判为死代码。
  本项目朴素版报 **24** 个，算可达闭包后真死仅 **20** —— `RuleItem/RuleProduct`（`RuleImportRequest.products` ↔
  `RuleProduct.rules`）、`DeadlineItem/ProgressItem` 都是活的，**多报 = 误删有引用的类 = 直接炸运行期**。
- **`response_model=0` 是提示不是结论**：先确认是不是自研响应信封，再决定"清理死类"还是"补响应校验"。
- **报"无 bug"要限定范围**：说"在已覆盖链路上未发现功能性缺陷"，不说"没有 bug"。

**报告骨架**（照抄）：一 功能完整性（实现率 + 分组分布 + 未登记清单）／二 冗余逐项列 ＋ 可清理判断／
三 三层实测 + 低覆盖模块表 + 基线对照／四 结论与建议动作（事项｜优先级｜建议）／五 整改落地明细（若执行）。
**数字分三层口径**：查验时快照 / 整改后实测 / 原基线，别混成一个数。

**验证记录**：2026-09-17 对本次交付文档执行 —— 真实 API 82 路径 / 91 操作、文档登记 70（滞后 21 条，已回填）；
真死 20 个 schema（已删 51→31）；三层全绿后补 25 条单测 → pytest 440/440（90%）；
**同日再收口 notify/system/asr 三个低覆盖模块（各补到 100%）→ 472/472（91%）**；
**再收口 `agent_tools.py`（78%→100%，30 条）→ 502/502（93%）**；行级隔离 +1 → 503/503（93%）**；限流+监控 +9 → 512/512（93%）**；加密+内容安全 +19 → 531/531（93%）**；工具行级 +2 → 533/533（93%）**。

---

## 三、本项目已有的技能（本次复查，未改动结构）

> 索引表里标「已有」的共 4 条：本节详述 `windows-python-one-click-launcher` 与
> `zero-build-frontend-verify`；另两条 `additive-column-migration`、`zero-dependency-doc-export`
> 由 2026-09-16 的回填纳入交付包技能副本，内容见各自 `SKILL.md`（前者：给已有真实数据的库
> 用 ADD COLUMN 补列而不重建 + 老库回归测试；后者：不引第三方库、用标准库手写最小结构导出 xlsx / PDF）。

### 3.1 `windows-python-one-click-launcher`

Windows 上给 Python Web 项目做「双击即跑」：`launcher.py` + `start.bat` / `stop.bat`，
覆盖找解释器 → 查依赖 → 生成 `.env` → 建库灌种子 → 挑空闲端口 → 起前后端 → 探活 →
开浏览器 → 停服 / 状态 / 自检。

积累的两个关键坑（都在技能里）：

- **`.bat` 必须纯 ASCII**（cmd 按 ANSI 读），中文输出只由 python 打印；`if (...)` 块内的 `echo` 文案禁止出现括号
- 🔴 **`Popen` 起 `cmd /k ...` 时参数绝不能传 list**：会经 `list2cmdline` 把内层引号转义成 `\"`，
  而 cmd.exe 不认反斜杠转义 → 去找一个字面名叫 `\"C:\...\python.exe\"` 的文件 →
  报「不是内部或外部命令」。正确做法是把整行**拼成字符串**再交给 `Popen`，
  且分隔用 `&` 不用 `&&`（否则装饰性的 `title` 一失败服务就不启动了）。
  配了一条**反证用例**钉死老写法。

### 3.2 `zero-build-frontend-verify`

零构建前端（原生 ES Module + 手写 CSS）的三层验证：
L1 `node --check`（要先把 `.js` 复制成 `.mjs`）→ L2 import/export 一致性
（专抓白屏级错误，秒级）→ L3 puppeteer-core 驱动本机 Edge 的真实浏览器 E2E + 逐页截图。

本项目新增的补充（本次已写进技能）：

- **首步断言的元素必须就是「未登录时的默认可见元素」**：一旦调整入口页，
  旧的 `waitForSelector('#login-screen')` 会直接超时，而**报错只是 "timeout"，看不出是入口变了**
- 每个新页面都要有「内容渲染完整」的**计数断言**（如"6 张能力卡 / 4 条 FAQ"），截图抓不到这类问题
- 「多个页面状态同屏只显示一个」的写法：默认显示的那个 div **不要写 `hidden`**（JS 挂掉不白屏、首屏不闪）
- 退出与会话失效要分开：主动退出回落地页，401 失效回登录屏并给原因
- 新增了通用编排器 `scripts/run_with_servers.py`（起服 → 跑 node 脚本 → 收服，单次调用内完成）

---

## 四、看过但**刻意不做**的（避免重复造）

| 候选 | 为什么不做 |
|---|---|
| 技术文档三件套流水线（`docs/tech-doc-pipeline/`） | 官方 `tencent-docx` 插件已经覆盖了「Stage1 创作 → Stage2 排版过质量门禁 → Stage3 转 DOCX」全流程；本地 Word 读写另有 `editor-sdk-docx`。重复造一份只会和官方实现漂移 |
| 统一响应信封 / `trace_id` / 「角色以 Token 为准」 | 这是**项目约定**，不是可执行的工作流。写进 README 与设计文档即可 |
| `.bat` 纯 ASCII、cmd.exe 引号规则 | 已并入 `windows-python-one-click-launcher`，不另起一个 |
| 前端导入一致性检查 | 已并入 `zero-build-frontend-verify` 的 L2 |
| 中文路径相关的一堆坑（safe-delete 失败、`.ps1` 被拦、editor_sdk 误报 not found） | 属于**本机环境特性**，跨项目通用但不构成工作流 → 写在用户级 `~/.workbuddy/MEMORY.md` |
| 「同一文件多处 `Edit` 并发会静默丢失」 | 工具使用陷阱，同为环境级 → 写进用户级 `MEMORY.md`，并已顺手记进 `zero-build-frontend-verify` 的坑表 |

---

## 五、怎么用

技能是按需**自动加载**的：对话里出现相关意图时，助手会读取对应 `SKILL.md` 并按其工作流执行。

手动查看或直接用里面的脚本也可以：

```powershell
# 看某个技能
cat "$env:USERPROFILE\.workbuddy\skills\mock-first-external-service\SKILL.md"

# 直接用它带的脚本
python "$env:USERPROFILE\.workbuddy\skills\workbuddy-session-triage\scripts\triage_sessions.py"
python "$env:USERPROFILE\.workbuddy\skills\mock-first-external-service\scripts\preflight_external_service.py"
python "$env:USERPROFILE\.workbuddy\skills\dify-workflow-draft-node-repair\scripts\dify_draft_dims.py" --help
```

**注意**：`SKILL.md` 里的示例代码与选择器是按本项目写的。
换项目使用时，先改**开头的 CONFIG 段 / 表名 / 选择器**，再跑。
`mock-first-external-service` 与 `http-smoke-baseline` 的脚本都预留了统一的 CONFIG 区。

---

## 六、维护约定

1. **技能里写"为什么"，不只写"怎么做"**。每条踩过的坑都保留症状 + 根因 + 解法，
   否则下次只会照着抄、一遇到变体就失效。
2. **改代码发现问题时，同一次就修技能**。技能过时比没有技能更危险。
3. **加脚本就加到 `scripts/` 并在 SKILL.md 的表格里登记**，别只在正文提一句。
4. **发现技能之间职责重叠、命名混乱时**，先提醒再动——不要擅自批量重构或删除。
5. 本机有两类写入位置，别混：
   - 跨项目的**可执行工作流** → `~/.workbuddy/skills/`
   - 跨项目的**环境事实 / 个人习惯** → `~/.workbuddy/MEMORY.md`
   - 本项目约定 → 项目内 `.workbuddy/memory/MEMORY.md`
