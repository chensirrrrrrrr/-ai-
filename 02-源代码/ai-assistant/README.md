# 留学机构 AI 智能助手系统

> 技术栈：**Python 3.11+ / FastAPI / Dify(Agent 编排) / MySQL 8 或 SQLite 3**
> 前端：**原生 ES Module + CSS 变量，零构建零依赖，前后端完全分离**

一个统一对话入口 + 多智能体路由的系统。**Agent 的推理与生成交给 Dify，
业务写操作留在 FastAPI**（可事务、可鉴权、可审计），两条线通过「工具回调」打通。
前端只消费 HTTP 契约，不感知后端的数据库方言 / Dify 应用 / 密钥。

---

## 一、一键跑起来（推荐）

Windows 上直接**双击 `start.bat`**。它会自己完成「找 Python → 查依赖（缺了就装）→
生成 `.env` → 建表灌种子 → 挑空闲端口 → 起前后端 → 探活 → 开浏览器」，
然后弹出两个服务窗口（日志实时可见，关掉窗口即停服）。

```bat
start.bat                  :: 启动（后端 8010 / 前端 8020，自动开浏览器）
start.bat stop             :: 停掉前后端（或直接双击 stop.bat）
start.bat --status         :: 看现在在不在跑
start.bat --selfcheck      :: 起服 → 13 项体检 → 收服（CI / 无桌面环境用）
start.bat --headless       :: 不开窗口，日志写进 .run\logs\
start.bat --reseed         :: 重建演示数据
start.bat --smoke          :: 启动后顺带跑 149 项冒烟测试
start.bat --help           :: 全部参数
```

不想用 bat 也行：

```bash
python scripts/launcher.py            # 等价于双击 start.bat
python scripts/launcher.py --selfcheck
```

启动成功后：

| 入口 | 地址 |
|---|---|
| 前端界面 | http://127.0.0.1:8020 |
| 接口文档 | http://127.0.0.1:8010/docs |
| 健康检查 | http://127.0.0.1:8010/api/v1/health |

> 端口自动避让：8010/8020 被占就往后找（最多试 20 个），选中的后端端口会
> 通过 `serve_frontend.py --api-base` **自动注入前端**，所以换端口也不用改代码。
> 若检测到本项目已在运行，则直接复用，不会起两套。

### 手动分步启动（想自己控制每一步）

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows；本机已用隔离 venv
pip install -r requirements.txt                  # 生产用 MySQL 再加 requirements-mysql.txt
copy .env.example .env                           # 默认 SQLite + Dify mock，无需外部依赖
python scripts/seed.py                           # 建表 + 演示数据
uvicorn app.main:app --reload --port 8010        # 后端
python scripts/serve_frontend.py                 # 前端（另开终端）→ http://127.0.0.1:8020
```

> 前端用了 ES Module，**必须通过 HTTP 访问**；直接双击 `frontend/index.html`
> 会被浏览器的模块同源策略拦住（白屏）。前端拿后端地址的优先级是：
> 登录页「接口地址」（存 `localStorage.uas.api_base`）> 静态服务器注入
> `runtime-config.js` > 默认 8010。

演示账号：

| 用户名 | 密码 | 角色 | 能做什么 |
|---|---|---|---|
| `admin` | `admin123` | admin | 全部能力 |
| `manager` | `manager123` | manager | 报告、审计、心理预警、NL2SQL（全范围） |
| `advisor` | `advisor123` | employee | 客户/线索、研判、NL2SQL（销售范围） |
| `teacher` | `teacher123` | employee | 成绩录入、请假审批、工单处理 |
| `student` | `student123` | student | 自己的成绩/进度/请假/投诉 |

访客无需注册：`POST /api/v1/auth/visitor` 直接换一个只含 `visitor` 角色的 Token。
未登录时默认打开**访客落地页**（`#landing`），页面上「免费咨询」按钮即走这条链路
（无需账号，只能进「对话助手」和「系统状态」）；「员工登录」按钮才切到登录屏。

---

## 二、目录结构

```
ai-assistant/                  ← 项目根，所有东西都在这里面
├── start.bat                  # ★ 一键启动（双击即用）
├── stop.bat                   # ★ 一键停止
├── app/                       # 后端源码
│   ├── main.py                # 应用入口：中间件、CORS、异常处理、路由挂载
│   ├── config.py              # pydantic-settings，全部配置走环境变量
│   ├── core.py                # 响应信封 / trace_id / 领域异常 / 密码 / JWT / HMAC
│   ├── db.py                  # 引擎与会话（SQLite 与 MySQL 同一套模型）
│   ├── models.py              # 18 张业务表 + sys_account
│   ├── schemas.py             # Pydantic v2 请求/响应模型
│   ├── services/
│   │   ├── dify.py            # Dify 客户端（chat / workflow / stream / audio，含 mock 与降级）
│   │   ├── mock_agent.py      # mock 模式下的本地编排：按 intent 真查库 / 真落库
│   │   ├── intent.py          # 两阶段意图路由 + 角色权限过滤
│   │   ├── nl2sql.py          # 受控模板 + 白名单
│   │   ├── asr.py             # 语音转写 Provider + 槽位抽取
│   │   └── audit.py           # 审计日志
│   └── api/
│       ├── deps.py            # 鉴权依赖（Bearer -> Principal）
│       └── v1/                # 按业务域拆分的路由
├── frontend/                  # 前端（独立部署单元）
│   ├── index.html             # 访客落地页 + 登录屏 + 控制台骨架
│   ├── runtime-config.js      # 后端地址注入点（被静态服务器动态覆盖）
│   ├── styles.css             # 设计令牌 + 双主题（深浅色）
│   └── js/
│       ├── config.js          # 接口地址、Token、会话号、角色字典
│       ├── api.js             # API 客户端（统一信封解包 / 错误分类 / SSE）
│       ├── ui.js              # 转义、Toast、弹窗、表格、徽标、图表
│       ├── main.js            # 落地页/登录屏/控制台三态切换、导航、hash 路由、主题
│       └── views/             # 七个业务视图
├── scripts/                   # 运维与测试脚本
│   ├── launcher.py            # ★ 一键启动/停止/状态/自检（start.bat 调它）
│   ├── check_dify.py          # Dify 接线校验：切 live 前逐个探活 7 个应用
│   ├── seed.py                # 建表 + 演示数据（幂等，支持 --reset）
│   ├── smoke_test.py          # 端到端冒烟测试（需服务已启动）
│   ├── serve_frontend.py      # 前端静态服务器（补 MIME、关缓存、注入后端地址）
│   ├── check_frontend_imports.py  # 前端导入一致性校验（抓白屏级错误）
│   └── frontend_e2e.mjs       # 浏览器端到端验证 + 截图
├── tests/                     # pytest 自动化测试
├── docs/                      # 交付文档
│   ├── AI智能助手系统_需求规格说明书.docx
│   ├── AI智能助手系统_技术方案设计文档.docx
│   ├── AI智能助手系统_技术实现与部署文档.docx
│   └── tech-doc-pipeline/     # 技术文档的三阶段生成溯源（MD → HTML → DOCX）
├── reports/                   # 测试报告与截图
│   ├── smoke_report.md
│   ├── frontend_e2e_report.md / .json
│   └── frontend_shots*/       # E2E 逐页截图
├── _lab/                      # 调试存档（非交付物，可整个删掉）
├── .run/                      # 运行态：pids.json + logs/（gitignore）
├── data/                      # SQLite 数据文件（运行时生成，gitignore）
├── Dockerfile
├── docker-compose.yml         # api + mysql
└── requirements*.txt
```

> 目录约定：`docs/` 是给人看的交付物，`reports/` 是测试证据，`_lab/` 是过程垃圾。
> `docs/README.md` 与 `_lab/README.md` 各自说明了里面的东西。

---

## 三、Dify 怎么接（关键）

### 3.1 一个 Agent 一个 Dify 应用

| 本地 Agent 名 | Dify 应用类型 | 用途 |
|---|---|---|
| `router` | Workflow（问题分类器节点） | 意图识别，返回 `agent/intent/confidence/slots` |
| `customer_service` | Chatflow | 客服问答（挂知识库 + RAG） |
| `student_helper` | Agent（挂 Tool） | 学生行政、售后、学业 |
| `enterprise_assistant` | Agent（挂 Tool） | 员工取数、录入、审批 |
| `mental_care` | Chatflow | 情绪疏导 + 风险识别 |
| `screener` | Workflow | 文档研判，输出结构化结论 |
| `reporter` | Workflow | 取数 + 指标计算 + 叙述生成 |

### 3.2 从 mock 切到 live（四步，别跳第 3 步）

**为什么必须按顺序**：live 模式下 Dify 调用失败会被降级到 mock（这是「不出现无响应」的设计），
副作用是**配错了也看不出来** —— 你拿到的是 mock 回答，却以为已经接上 Dify 了。
所以第 3 步的探活是必须的，不是可选的。

```bash
# 1) 指向你的 Dify 服务 API 地址（⚠️ 要带 /v1）
#    Dify 官方 docker compose 默认监听 80，所以本机自托管就是 http://127.0.0.1/v1
DIFY_BASE_URL=http://dify.internal/v1

# 2) 7 个应用各建一个「API 访问」Key，填进 DIFY_APP_KEYS
#    JSON 与 k=v 两种写法都支持（k=v 更少打错引号）
DIFY_APP_KEYS={"router":"app-xxx","customer_service":"app-yyy","screener":"app-zzz"}
DIFY_APP_KEYS=router=app-xxx,customer_service=app-yyy,student_helper=app-zzz,enterprise_assistant=app-aaa,mental_care=app-bbb,screener=app-ccc,reporter=app-ddd

# 3) 逐个探活 —— 这一步会告诉你到底通没通
python scripts/check_dify.py          # 也可用 start.bat --dify-check
#    [OK]   全部通过 ✓ 可以放心把 DIFY_MODE 切到 live

# 4) 确认全绿后再切模式并重启
DIFY_MODE=live
```

`check_dify.py` 的判定口径（能把常见错误分开）：

| 现象 | 结论 | 怎么办 |
|---|---|---|
| `200` | 通过 | —— |
| `401 / 403` | Key 无效 | 回 Dify 控制台重新复制 API Key |
| 全部 `404` | base_url 路径不对 | 十有八九是少了 `/v1` |
| 连不上 / 超时 | 服务没起或地址不通 | 先确认容器起来了、端口通 |
| `502` 且「走系统代理：是」 | **被代理接走了** | 见下面的代理坑 |

> ⚠️ **代理坑（实测踩过）**：httpx 默认会沿用 `HTTP_PROXY` / `HTTPS_PROXY`，
> 于是发往 `127.0.0.1` 的请求也会被系统代理接走，返回 502，
> 再被降级逻辑吞掉变成 mock 回答。本项目的处理是**自动判断**：
> 本机/内网地址（`127.0.0.1`、`localhost`、`10./172.16-31./192.168.`、`host.docker.internal`）
> 绕开代理，公网地址沿用代理。要强制覆盖就写 `DIFY_TRUST_ENV_PROXY=true|false`。

启动时也会自动预检：若 `DIFY_MODE=live` 但 Key 没配齐，直接打印缺哪几个，
不会等你问到一半才发现。`GET /api/v1/health` 同样会暴露
`dify_live` / `dify_ready` / `dify_key_count` / `dify_missing_keys`。

`DIFY_MODE=mock` 时**完全不联网**，用内置确定性应答 —— 本地开发与单元测试因此不依赖 Dify。

### 3.3 mock 模式下也能真干活（`services/mock_agent.py`）

mock 不等于「什么都不会」。统一对话入口在 mock 模式下走 `mock_agent.respond()`，
它**先看路由结果再决定怎么答**，而不是拿用户原话去撞一张静态关键词表：

| 路由到的 intent | mock 下的实际行为 |
|---|---|
| `nl2sql_query` | 真跑受控模板查库，返回数据行 + 模板名 + SQL 预览；角色不够就明说没权限 |
| `after_sales` | 真写 `after_sales_ticket`（10 分钟内重复提交自动合并，不重复开单） |
| `leave_apply` | 真写 `student_request`（带 `idempotency_key`）；槽位不全时先追问 |
| `onboarding` | 真读入职指引（版本化常量），答复带出处；五阶段步骤 / FAQ / 关键联系人都来自现算 |
| `todo_push` | **先问再答**：列出本人待办（四类业务待办 + 主动询问话术），不含敏感类 |
| `alert_digest` | 仅管理层：汇总未触达心理预警并给出建议动作；员工侧在 handler 里再卡一道 |
| 寒暄 / 能力询问 | 「你好」「在吗」「你能做什么」给正常应答 + 能力清单；**只有整句都是寒暄才算** |
| 知识类 | 检索内置语料（按**最长关键词优先**匹配），并标出引用出处 |
| 兜底 | 说明能力边界 + 列出可问的问题 + 引导「转人工」，不再回「暂未查询到」 |

> 寒暄那条有个容易踩的坑：「你好，请问雅思要考多少分」**不能被当成寒暄吞掉**，
> 否则就从「答非所问」变成「干脆不答」。所以判定是「把开头的寒暄词和标点逐个吃掉后，
> 整句是否已空」，而不是关键词包含。`tests/test_mock_agent.py` 有用例锁住。

之所以要做这一层：曾经 mock 应答**无视路由结果**，导致「帮我查一下最近的意向客户跟进情况」
这种库里明明有数据的问题，也只会回一句「暂未查询到相关信息」。

> 边界要说清：这是**离线替代品**，不是通用问答。开放域自由问答（例如「帮我用 Rust 写个红黑树」）
> 需要真实模型 —— 配 `DIFY_MODE=live` + `DIFY_APP_KEYS` 接上 Dify 后，本层不参与。

### 3.4 让 Dify 反过来调 FastAPI（工具节点）

Dify 的 Agent 通过 HTTP 工具节点调用：

```
POST {FASTAPI}/internal/tools/{tool_name}
X-Tool-Timestamp: <unix 秒>
X-Tool-Signature: HMAC_SHA256(tool_shared_secret, timestamp + "." + raw_body)
```

可用工具：`lead_lookup` / `student_scores` / `pending_requests` / `nl2sql`。
清单见 `GET /internal/tools`。生产环境请在网关只放行 Dify 容器网段。

---

## 四、数据库：SQLite ↔ MySQL 一键切换

只改一个环境变量：

```env
# 开发（默认，文件即库，零依赖）
DATABASE_URL=sqlite:///./data/ai_assistant.db

# 生产
DATABASE_URL=mysql+pymysql://uas:pwd@127.0.0.1:3306/ai_assistant?charset=utf8mb4
```

兼容做法：`BigIntPK` 在 SQLite 上退化为 `INTEGER`（否则自增失效），
`JSON` 列 SQLite 存 TEXT / MySQL 存原生 JSON；连接池参数仅在非 SQLite 时生效。

---

## 五、测试

```bash
pytest -q                                              # 全部用例（320 项）
pytest --cov=app --cov-report=term-missing             # 带覆盖率

python scripts/launcher.py --selfcheck                 # 真起服 → 13 项体检 → 收服
python scripts/launcher.py --smoke                     # 启动后跑 149 项冒烟测试
python scripts/launcher.py --dify-check                # Dify 接线探活（切 live 前必跑）
```

测试用独立的 SQLite 文件库，session 级建表 + 灌种子数据，跑完自动清理；
`DIFY_MODE=mock`，不依赖任何外部服务。

三层验证的分工：

| 层 | 命令 | 验什么 |
|---|---|---|
| 单元 / 接口 | `pytest -q` | 业务逻辑、鉴权矩阵、状态机、响应契约 |
| 起服体检 | `launcher.py --selfcheck` | 依赖、`.env`、库、端口、探活、鉴权链路、前端资源可加载 |
| 真浏览器 | `scripts/frontend_e2e.mjs` | 落地页、登录、七视图、对话链路、双主题、越权、坏地址容错、访客入口、截图 |

当前实测基线（四块改进交付后的最终结果，**改动后不得低于**）：

| 层 | 结果 | 证据文件 |
|---|---|---|
| 单元 / 接口 | **533 passed**，覆盖率 **93%** | `pytest -q` 输出（一键：`scripts/regression.py`） |
| 起服体检 | **13 / 13** | `launcher.py --selfcheck` |
| 端到端冒烟 | **149 / 149**（离线 mock 563 ms；接真实 Dify 约 30 s） | `reports/smoke_report.md` |
| 浏览器 E2E | **37 / 37**，0 页面异常 | `reports/frontend_e2e_report.json` |
| 前端导入一致性 | **OK**（12 文件 / 26 处 import） | `scripts/check_frontend_imports.py` |

---

## 六、常见问题

**`start.bat` 一闪而过 / 启动器退出码非 0**
先看 `start.bat --status`；窗口模式看不到报错就用 `start.bat --headless`，
日志会落到 `.run/logs/backend.log` 与 `.run/logs/frontend.log`。

**后端起不来，报 `error parsing value for field "cors_origins"`**
`.env` 里的 `CORS_ORIGINS` 用了逗号分隔，而 pydantic-settings 对 `List` 字段
默认按 JSON 解析、并在数据源层抢先 `json.loads`。本项目已给该字段加了
`NoDecode` 并自写校验器（同时接受逗号分隔与 JSON 数组），
`tests/test_config.py` 已锁住这条路径；若你新增了别的 `List` / `Dict` 字段，
记得同样处理，或直接写成 JSON。

**弹出的控制台窗口里报「`"C:\...\python.exe"` 不是内部或外部命令，也不是可运行的程序或批处理文件」**
这是**在 cmd.exe 里错误转义引号**造成的：把整条命令当 **list** 传给
`subprocess.Popen` 时，Python 会先用 `list2cmdline` 把内层引号转义成 `\"`，
而 **cmd.exe 不认反斜杠转义** —— 于是它去找一个字面名叫 `\"C:\...\python.exe\"`
的文件，自然找不到。表现就是窗口标题（`title` 那半句）正常设上了、
下一句却报错，服务起不来。

正确写法在 `scripts/launcher.py::console_cmdline()`：
把 `cmd /k "title X & "C:\...\python.exe" args"` 当作**一整个字符串**交给
`Popen`（字符串 = 原样送给 `CreateProcess`，不再二次转义），
让 cmd 自己按「引号数 > 2 就掐掉最外层首尾引号」的规则去解析。
（用 `&` 不用 `&&`：`title` 只是改窗口标题的装饰，万一它失败，
用 `&&` 会让服务**根本不启动**。）
`tests/test_launcher_spawn.py` 既断言拼出来的串里**不含 `\"`**，
也**真的把这条命令行交给 cmd 跑一遍**（还有一条反证用例钉死老写法就是坏的）——
以后再有人把参数改回 list，测试会直接红。

**前端白屏**
一定是没用 HTTP 打开的（`file://` 下 ES Module 被同源策略拦），或者端口写错。
用 `start.bat` 起的服务不会踩这个坑。

**端口被占用**
启动器会自动往后找空闲端口，并把选中的后端端口注入前端，无需手动改配置。

**`start.bat stop` 说「可能已经退出」，但服务还在跑**
PID 记录会失效（控制台窗口被关过、进程被系统回收过）。`--stop` 现在除按 PID 清理外，
还会**按端口兜底**：先用探针确认该端口上跑的是本项目的服务，再结束占用进程，
不会误伤占用同端口的其他程序。实在清不掉时，`--status` 会如实报告哪个端口还活着。

---

## 七、Docker 部署

```bash
docker compose up -d --build
# api  ->  http://127.0.0.1:8000/docs    (容器内固定 8000；本地 launcher 跑的是 8010，两者无关)
# mysql -> 127.0.0.1:3306
```

进容器初始化数据库：

```bash
docker compose exec api python scripts/seed.py
```

---

## 八、安全要点

- 角色一律以 **Token 里的角色**为准，请求体里的 `role` 只作参考，防止伪造越权；
- 写操作支持 `Idempotency-Key`，重复提交返回同一条记录；
- 状态机非法流转直接 409（如重复审批、超额报名）；
- NL2SQL 只能走内置模板，执行前再过一遍表名/关键字白名单，行数硬上限；
- 敏感数据（心理预警、身份证）仅管理层可见且脱敏返回；
- 每笔写操作落 `audit_log`，带 `trace_id` 可全链路回放。

---

## 九、前端（前后端分离）

### 9.1 设计取向

| 约束 | 做法 |
|---|---|
| 零构建 | 原生 ES Module，浏览器直接跑，没有 npm / 打包器 / node_modules |
| 零依赖 | 不引 UI 框架和图表库，表格 / 弹窗 / Toast / 柱状图全部自己写 |
| 只认契约 | 前端代码里不出现数据库、Dify、密钥、模板清单等后端实现细节 |
| 换后端不改代码 | 登录页可填「接口地址」（存 `localStorage`），或由静态服务器注入 `runtime-config.js` |
| 双主题 | CSS 变量 + `data-theme`，跟随系统并可手动切换 |

### 9.2 三态入口与目录

页面只有三种状态，同时只显示一个：

| 状态 | 元素 | 何时出现 |
|---|---|---|
| 访客落地页 | `#landing` | 未登录时的默认入口；有 Token 但校验失败不会停留在这里 |
| 登录屏 | `#login-screen` | 点落地页「员工登录」；或会话失效（401）时带提示进入 |
| 控制台 | `#console` | 登录成功 / 访客进入 / 本地 Token 校验通过 |

落地页的「免费咨询」直接调 `POST /api/v1/auth/visitor`，不经过登录屏；
控制台「退出」回到落地页，登录屏「返回首页」也回到落地页。

```
frontend/js/
├── config.js   接口地址 / Token / 会话号 / 角色字典（唯一与后端地址耦合的地方）
├── api.js      统一信封解包、错误分类（401/403/404/409）、SSE 流式解析
├── ui.js       esc 转义、Toast、Modal、table()、badge()、barChart()
├── main.js     落地页/登录屏/控制台三态切换、导航、hash 路由、主题、401 自动登出
└── views/
    ├── chat.js     对话助手：流式/阻塞、路由状态可视化、语音录入、主动提醒横幅
    ├── crm.js      客户管理：线索 CRUD、跟进（幂等）、材料上传解析、研判复核、批量研判
    ├── student.js  学员中心：学员只看自己；员工看花名册/审批/工单/预警
    ├── org.js      组织与指南：组织架构树、部门概览、花名册、新人入职指引、联系人、FAQ
    ├── ops.js      运营中心：活动报名、日报（含语音）、知识库、报告（五类 + 定时 + 导出）、主动待办
    ├── data.js     数据洞察：NL2SQL（模板提示 + 越权演示）、审计日志
    └── system.js   系统状态：探针、身份、改密、Dify 工具清单、部署说明
```

路由用 hash（`#/chat`、`#/crm` …），刷新不丢当前页；导航按角色动态裁剪，
学员看不到「客户管理」，访客只能进「对话助手」和「系统状态」。

### 9.3 前端测试

```bash
# 纯静态检查（不需要浏览器）
python scripts/check_frontend_imports.py    # 具名导入是否都存在（白屏级错误）
# 语法检查：把 js 复制成 .mjs 后跑 node --check
#   （.js 带 import 会被 node 当 CommonJS，直接 --check 必然报错）

# 真实浏览器端到端（需要前后端都已启动 + 本机 Edge）
set WB_NODE_WORKSPACE=<含 node_modules 的目录>   # ESM 不认 NODE_PATH
node scripts/frontend_e2e.mjs
```

`frontend_e2e.mjs` 用 puppeteer-core 驱动本机 Edge，覆盖 36 项断言：未登录默认落到
访客落地页、落地页内容渲染、两个出口（员工登录 / 免费咨询）、登录、七个业务视图逐个
打开（含「组织与指南」：架构树 / 花名册 / 指引 / 联系人 / FAQ 计数断言）、材料研判列表与
复核入口、修正清单弹窗、批量研判队列、对话页「主动提醒」横幅、运营中心待办卡片与推送留痕、
报告列表（五类口径 + 定时标记 + 导出入口）、生成报告弹窗、报告详情四种导出、对话链路、
意图路由、双主题对比度、访客越权 403、坏接口地址容错、返回首页、
退出回落地页、无 422 / 无 404 / 无未捕获异常，并逐页截图到 `reports/frontend_shots/`，
结果写 `reports/frontend_e2e_report.json`（全通过时退出码 0）。

当前结果：**37 / 37 通过**，0 个页面异常，控制台仅剩两条「故意触发」的错误
（越权测试的 403、坏端口测试的 `ERR_UNSAFE_PORT`）。

