# 留学机构 AI 助手系统 — 项目长期记忆

## 主目录（唯一副本，无 .git）

- `C:\Users\机械革命\Desktop\留学机构AI助手系统_交付\02-源代码\ai-assistant\`
  （交付包 `留学机构AI助手系统_交付\` 的 `02-源代码\` 即 live 工作区，已不是冻结快照。）
- ⚠️ 全项目**只有这一份拷贝，且没有 .git**，改坏了没有回退手段。

## 技术栈 / 结构

- FastAPI + SQLAlchemy 2.0（同步）+ SQLite(dev)/MySQL 8(prod)；前端是**零构建原生 ES Module**（无 npm/打包器）。
- 分层 `main.py → app/api/ → app/crud/ → app/models/`；关键目录 `app/ frontend/ scripts/ tests/ reports/ data/ docs/`。
- 解释器（固定）：`C:\Users\机械革命\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
- 一键启停 `start.bat` / `stop.bat`（核心 `scripts\launcher.py`）；自检 `python scripts\launcher.py --selfcheck --e2e`
  （⚠️ E2E 必须**同进程**跑）。调本机一律带 `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost`。
- 种子数据 `scripts\seed.py`（幂等，支持 `--reset`）。

## 验收基线（2026-09-17 全绿）

pytest **472/472（覆盖率 91%）** · smoke_test.py 149/149 · selfcheck 全过 · E2E(frontend_e2e.mjs) 37/37 · check_dify.py 7/7
（演进：09-16 基线 415/88% → 09-17 查验整改后 440/90% → 同日收口低覆盖模块后 **472/91%**。）

⚠️ 起常驻服务必须用**后台任务**：
`python -m uvicorn app.main:app --host 0.0.0.0 --port 8010`
+ `python scripts/serve_frontend.py --host 127.0.0.1 --port 8020 --api-base http://127.0.0.1:8010`。
停：`taskkill /PID <pid> /F /T`（**不是** `//PID`；PID 用 `netstat -ano | tr -d '\r'` 取）。

## 接口面与三查（2026-09-17）

- 真实 API（`app.openapi()`）：**82 路径 / 91 操作**；分组 system6 chat3 crm20 student22 org8 operation18 todo4 data3 通知5 internal2。
  交付 docx 的接口清单表原只登记 **70** 个（滞后 21 条）⇒ **已于 09-17 回填**（md + docx + stage2 HTML + 附录 B），现口径一致。
- 就绪探针是 **`/api/v1/ready`**（`/api/v1/health/ready` 会 404）；健康 `/api/v1/health`。
- 冗余巡查结论（已整改）：`response_model=` 全项目 **0 次**（响应走自研信封 `{code,message,data}`）；
  `schemas.py` 真死 **20 个 `*Out` 类**（⚠️ **不是 24** —— 判死要算**传递可达闭包**，
  `RuleItem/RuleProduct/DeadlineItem/ProgressItem` 被 `schemas.py` 内部引用，是活的）⇒ 已删 51→31；
  未用导入 3 处已清；`agent_tools.cancel()` **补接线**（`{"cancel":token}`）而非删。
- 报告：`02-源代码/ai-assistant/reports/代码实现与测试查验报告.md`（含 §五 整改明细）。
- 覆盖率基线：pytest `--cov=app` = **90%**（原 88%）；低覆盖已补：scheduler 64%→**100%** / db 65%→**96%** / agent_tools 67%→**78%**。
- ⚠️ 解析 docx 要先 `cd C:\Users\Public\uas_doc`，在 `ai-assistant` 目录下 `import docx` 会因同名干扰报错。
- ⚠️ SQLite `ALTER TABLE ... DROP COLUMN` 前必须先 `DROP INDEX IF EXISTS`（否则报 `error in index ... after drop column`）。
- ⚠️ `pytest.ini` 已内置 `-q`，**别再加 `-q`**（会吞掉「N passed」汇总行）。

## AI 编排：路由在 Python，不在 Dify

前端 → `POST /api/v1/chat`（`app/api/v1/chat.py`）→ `services/intent.route()`
① PREFILTER_RULES 关键词预筛 ② Dify router workflow ③ `ROLE_AGENTS` 权限矩阵 ④ 置信度分级 dispatch/clarify/fallback
→ `dify_client.chat(agent)`。`_dify_inputs()` 用 Token 里的 role **覆盖**客户端值，只对学生注入 `student_id`。

反向链路 `/internal/tools`：**26 个 ToolSpec**（读 12 / 写 14），写工具两段式确认；鉴权两道门满足其一
（HMAC 签名 / `Bearer DIFY_TOOL_KEY`），工具级再按 `ROLE_RANK` 拦。
⚠️ 遗留只读工具 `min_role` 常是 employee（如 `student_scores`）⇒ 学生 403；**别降它的门槛**（会跨学生泄露），
另开 `my_scores`（`required=student_id` + `min_role=student`），并有回归测试锁住分工。
⚠️ 生产要在网关只放行 Dify 容器网段访问 `/internal/tools*`。

## Dify 接线（1.10.0 自建）

- Docker Desktop 11 容器，控制台 `chenritiannb@gmail.com`，入口 `http://127.0.0.1/v1`；`.env` `DIFY_MODE=live`。
- 4 chat + 3 workflow 应用；「学生助手」已改 **Chatflow `7d263ed5-efa3-48c6-98c1-aaaaaf33a9b1`**
  （18 节点/17 连线，6 分支；旧 chat 应用 `464f52e0-…` 保留回退）。
- ⚠️ **画布节点顶层 `type` 必须是 `"custom"`**，真正种类放 `data.type`；写成块类型（`answer`/`llm`…）
  会让画布渲染成 150x21 白条，并误报「必须添加直接回复节点」（纯前端问题，后端只认 `data.type`）。
  详见技能 `dify-workflow-draft-node-repair`。
- 分类器（question-classifier）：`instruction` **只写「怎么判断」，绝不提输出格式**；所有节点显式 `"thinking": false`。
- 跨应用复用 `conversation_id` → 平台回 404/400 ⇒ `app/services/dify.py` 定向重试（**不是**通用重试）。
- 知识库：`dataset_configs.datasets.datasets[].dataset` 必须带 `"enabled": true`，否则静默丢弃；
  `POST /console/api/apps/<id>/model-config` 是**整体覆盖**，要先读全量再写回。
- **本地模型已停用**：知识库嵌入改走**智谱云端 `embedding-3`**（provider `langgenius/zhipuai/zhipuai`，
  改后 Dify 自动重建索引）；**对话 LLM 走 DeepSeek 云端 API**（`deepseek-v4-flash`）；ASR=`mock`。
  ⇒ 全链路**不再需要 Ollama**（ollama 插件仍装着，可作回退；历史 bge-m3 配置见 09-16 日志）。
- Dify 自定义工具不支持现算 HMAC ⇒ 加了静态 Key 通道（`DIFY_TOOL_KEY`）。

## 项目约定

- 中间产物统一放 `C:\Users\Public\uas_doc\`（ASCII 路径，避开 COS 中文路径 bug）。
- 删除/改文件前先列清单征得同意。
- `seed.py` 加新内容**必须写判空守卫**（本文件只在新库自动 seed，老库不回填）。
- 事项管理：项目下 14 条 todo（审计 P0/P1/P2 未闭环项）已挂报告；项目成员仅 1 人 ⇒ 无法改派负责人。
- 可复用流程已沉淀为技能：`api-implementation-audit`（交付文档 vs 真实代码三查：openapi 对账 / AST 死代码 / 覆盖定位）、
  `mock-first-external-service`（Dify 接线七坑 + 会话重试）、
  `dify-workflow-draft-node-repair`（白条/节点类型）、`dify-kb-embedding-switch`（知识库嵌入原地换云端 + 命中验证）、
  `zero-build-frontend-verify`（零构建前端三层校验）、
  `http-smoke-baseline`、`windows-python-one-click-launcher`、`idempotent-demo-seed`、`project-delivery-package`。
