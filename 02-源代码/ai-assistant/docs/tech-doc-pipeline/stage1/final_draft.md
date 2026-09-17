# 留学机构 AI 智能助手系统 —— 技术实现与部署文档

## 1. 文档说明

### 1.1 编写目的

本文档面向研发与运维人员，说明留学机构 AI 智能助手系统的技术实现方式与部署流程。文档中的全部数据（接口数量、数据表数量、测试用例数与通过率、依赖版本号）均取自实际运行并通过测试的工程实例，可直接作为部署与验收依据。

### 1.2 技术范围

本文档所描述的实现严格限定在以下技术栈内：

| 层次 | 技术选型 | 版本（实测） |
| --- | --- | --- |
| 语言 | Python | 3.13.14 |
| Web 框架 | FastAPI | 0.141.1 |
| ASGI 服务器 | Uvicorn | 0.52.4 |
| Agent 编排 | Dify（Chatflow / Workflow / Agent） | 服务端 API v1 |
| ORM | SQLAlchemy | 2.0.52 |
| 数据校验 | Pydantic / pydantic-settings | 2.13.5 / 2.15.0 |
| 开发数据库 | SQLite | 内置驱动 |
| 生产数据库 | MySQL 8 | `mysql+pymysql`，PyMySQL 1.2.0 |
| 鉴权 | PyJWT（HS256） | 2.13.0 |
| 测试 | pytest / pytest-cov | 9.1.1 / 7.1.0 |

### 1.3 读者对象

后端开发工程师、运维与 DBA、技术评审与验收人员。

## 2. 技术选型与总体架构

### 2.1 选型理由

**Python + FastAPI。** 该系统是一个以对话与数据查询为主的后端服务，天然是 I/O 密集型而非计算密集型。FastAPI 基于 ASGI 异步模型，在等待 Dify 上游返回、等待数据库返回时不会阻塞事件循环；其原生 Pydantic 集成让请求校验、响应序列化与接口文档三件事一次完成，显著降低了契约维护成本。

**Dify 承担 Agent 编排。** 智能助手系统的核心难点不在于"调用大模型"，而在于意图识别、任务拆解、知识检索、多轮状态管理与业务工具调用。这类逻辑如果写在后端代码里，任何话术调整、提示词迭代、知识库更新都要发版。把编排层交给 Dify 之后，后端只保留三件事：鉴权与权限、业务数据读写、受控数据查询。这样业务人员可以在不改代码的前提下调整 Agent 行为，研发则专注于数据与安全边界。

**SQLite 与 MySQL 双栈。** SQLite 零配置、单文件、可随代码包分发，适合本地开发、演示与自动化测试；MySQL 8 承担多连接并发、事务隔离与备份恢复的生产职责。两者通过 SQLAlchemy ORM 统一抽象，切换时只改一行数据库连接串。

### 2.2 分层架构

系统在纵向上分为五层，每一层只依赖其下一层：

| 层次 | 组成 | 职责 |
| --- | --- | --- |
| 接入层 | `app/main.py`、CORS 中间件、Trace 中间件 | 请求入口、跨域、链路追踪、全局异常兜底 |
| 接口层 | `app/api/v1/*.py`、`app/api/deps.py` | 路由定义、依赖注入、参数校验、权限校验 |
| 服务层 | `app/services/*.py` | 意图路由、Dify 调用、ASR、受控 SQL、审计 |
| 模型层 | `app/models.py`、`app/schemas.py` | ORM 实体与请求/响应契约 |
| 数据层 | `app/db.py` | 引擎与会话管理、SQLite/MySQL 方言差异处理 |

**关键设计取舍：** 接口层不直接访问 Dify，也不直接拼 SQL。所有外部依赖都被封装在服务层之后，因此服务层是唯一需要处理"上游失败如何降级"的位置。

### 2.3 请求处理链路

一次典型的对话请求按以下顺序流转：

1. 请求进入 Trace 中间件，生成或透传 `X-Trace-Id`，开始计时。
2. FastAPI 依赖注入解析 Bearer Token，得到 `Principal`（含 `user_id` 与 `role`）。
3. 接口层做参数校验，进入服务层。
4. 服务层执行**两阶段意图路由**：先用正则/关键词做低成本预筛，命中则直接判定；未命中再调 Dify `router` 工作流。
5. 意图路由结果经过**角色白名单收敛**，确定最终由哪个 Dify 应用应答。
6. 调用对应的 Dify 应用（对话型走 `/chat-messages`，工作流型走 `/workflows/run`）。
7. 若该业务动作为写操作，落库前先查幂等键，落库后写审计日志。
8. 结果封装为统一响应体返回，响应头回填 `X-Process-Time-Ms`。

## 3. 运行环境与依赖

### 3.1 Python 依赖清单

生产必需依赖（`requirements.txt`）：

```text
fastapi>=0.115,<1.0
uvicorn[standard]>=0.30
sqlalchemy>=2.0,<3.0
pydantic>=2.7
pydantic-settings>=2.3
httpx>=0.27
PyJWT>=2.8
python-multipart>=0.0.9
```

MySQL 生产环境附加依赖（`requirements-mysql.txt`）：

```text
PyMySQL>=1.1
cryptography>=42.0
```

测试依赖（`requirements-dev.txt`）：`pytest`、`pytest-cov`。

### 3.2 环境变量清单

配置由 `app/config.py` 中的 `Settings` 类统一承载，基于 `pydantic-settings` 从环境变量或 `.env` 读取，并通过 `lru_cache` 缓存单例。

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./data/ai_assistant.db` | 数据库连接串，切 MySQL 只需改此项 |
| `SECRET_KEY` | 开发占位值 | JWT 签名与内部工具 HMAC 的密钥，**生产必须替换为 32 字节以上随机值** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `120` | 访问令牌有效期（分钟） |
| `DIFY_MODE` | `mock` | `mock` 离线确定性响应；`live` 真实调用 Dify |
| `DIFY_BASE_URL` | `http://127.0.0.1/v1` | Dify 服务地址 |
| `DIFY_APP_KEYS` | `{}` | 应用名到 API Key 的映射字典 |
| `DIFY_TIMEOUT` | `30.0` | 上游调用超时（秒） |
| `DIFY_MAX_RETRIES` | `2` | 上游失败重试次数 |
| `DIFY_USER_PREFIX` | `uas` | 传给 Dify `user` 字段的前缀，用于终态用户隔离 |
| `ASR_PROVIDER` | `mock` | `mock` 或 `dify` |
| `NL2SQL_MAX_ROWS` | `50` | 受控查询返回行数上限 |
| `CORS_ORIGINS` | `["*"]` | 允许的跨域来源，生产应收紧 |

**配置校验要点：** `SECRET_KEY` 长度不足 32 字节时，PyJWT 的 HS256 签名会触发安全告警。项目在测试与生产模板中均使用 32 字节以上的密钥，避免带安全隐患上线。

### 3.3 工程目录结构

```text
ai-assistant/
├── app/
│   ├── main.py                 # create_app()：中间件、异常处理、路由挂载
│   ├── config.py               # Settings 与 get_settings()
│   ├── core.py                 # 响应封装、异常体系、密码哈希、JWT、HMAC
│   ├── db.py                   # 引擎构建、会话工厂、方言差异处理
│   ├── models.py               # 19 张表的 ORM 定义
│   ├── schemas.py              # 请求/响应 Pydantic 模型
│   ├── api/
│   │   ├── deps.py             # get_principal / require_roles / client_ip
│   │   └── v1/
│   │       ├── router.py       # 聚合 api_router 与 internal_router
│   │       ├── system.py       # 健康检查与认证
│   │       ├── chat.py         # 对话、流式、语音录入
│   │       ├── crm.py          # 意向客户、跟进、研判
│   │       ├── student.py      # 学员档案、成绩、申请审批、工单、预警
│   │       ├── ops.py          # 活动、报告、知识库
│   │       ├── data.py         # NL2SQL 与审计日志
│   │       └── tools.py        # Dify 回调的内部工具
│   └── services/
│       ├── intent.py           # 两阶段意图路由与权限收敛
│       ├── dify.py             # DifyClient：对话、流式、工作流、ASR
│       ├── asr.py              # ASR Provider 抽象与槽位抽取
│       ├── nl2sql.py           # 受控 SQL：模板、白名单、危险关键字拦截
│       └── audit.py            # 审计日志写入
├── scripts/
│   ├── seed.py                 # 幂等种子数据（支持 --reset）
│   └── smoke_test.py           # 端到端冒烟测试
├── tests/                      # pytest 用例与夹具
├── data/                       # SQLite 数据文件目录
├── Dockerfile
├── docker-compose.yml
├── requirements*.txt
└── pytest.ini
```

## 4. 数据模型设计

### 4.1 数据表总览

系统共定义 **19 张表**，其中 **18 张为业务表**，另加 1 张系统账号表。按业务域划分如下：

| 业务域 | 数据表 | 说明 |
| --- | --- | --- |
| 组织与账号 | `employee`、`employee_report`、`sys_account` | 员工档案、员工日报/报告、登录账号 |
| 客户域 | `customer_lead`、`customer_followup`、`lead_screening` | 意向客户、跟进记录、客户研判结论 |
| 学员域 | `student`、`student_score`、`student_request` | 学员档案、成绩、申请与请假审批单 |
| 心理健康 | `student_mental_profile`、`mental_alert` | 学员心理画像、预警记录 |
| 售后服务 | `after_sales_ticket` | 投诉与工单 |
| 运营域 | `activity`、`activity_enrollment`、`course_project` | 活动、报名、课程项目 |
| 知识库 | `knowledge_doc` | 知识文档登记（关联 Dify 数据集） |
| 报告 | `report_record` | 报告生成与下载记录 |
| 待办推送 | `todo_push` | 主动待办推送留痕（推送对象、去重键、处理状态） |
| 审计 | `audit_log` | 全量写操作审计 |

### 4.2 双栈兼容的三个关键处理

**其一，主键类型。** SQLite 的 `AUTOINCREMENT` 仅对 `INTEGER PRIMARY KEY` 生效，直接使用 `BigInteger` 会导致自增失效。项目定义了一个按方言切换的主键类型：

```python
BigIntPK = BigInteger().with_variant(Integer, "sqlite")
```

这样在 SQLite 下生成 `INTEGER` 主键、在 MySQL 下生成 `BIGINT` 主键，模型代码无需分支。

**其二，JSON 字段。** SQLite 无原生 JSON 类型，项目在 SQLite 下退化为 `TEXT` 存储、在 MySQL 下使用原生 `JSON`，读取侧统一用 `json.loads` / `json.dumps` 处理，对上层透明。

**其三，连接行为。** SQLite 需要显式开启外键约束并调整日志模式，项目通过 SQLAlchemy 的 `connect` 事件在每次建立连接时执行：

```python
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()
```

MySQL 侧则改为配置连接池：`pool_size`、`max_overflow`、`pool_pre_ping=True`（自动剔除失效连接）。

### 4.3 幂等与审计的字段设计

- `customer_followup.idempotency_key` 建有唯一约束，重复提交同一键时返回已存在的那条记录（并在响应中标记 `duplicated: true`），而不是新增一条。
- `audit_log` 记录每次写操作的 `action`、`actor_id`、`target_type`、`target_id`、`detail` 与 `trace_id`，形成"谁在什么时候通过哪条链路改了什么"的完整追溯。

## 5. 核心机制实现

本章逐个说明支撑业务运转的十一项核心机制及其实现细节。

### 5.1 统一响应体与错误码

所有业务接口返回同一结构，前端只需实现一套解析逻辑：

```json
{
  "code": 0,
  "message": "success",
  "data": {},
  "trace_id": "8f3c1a2e-..."
}
```

`code` 为 0 表示成功，非 0 对应下表错误码；`trace_id` 与响应头 `X-Trace-Id` 一致，便于按链路排查。异常体系通过 FastAPI 的异常处理器统一转换为该结构，业务代码只需抛出语义化异常。

| 错误码 | HTTP 状态 | 含义 | 触发场景示例 |
| --- | --- | --- | --- |
| `0` | 200 | 成功 | — |
| `40000` | 400 | 请求参数非法 | 材料格式不支持、批量数超上限、报告类型未知、导出格式不支持 |
| `40100` | 401 | 未认证或令牌无效 | 密码错误、缺少 Token、Token 过期 |
| `40300` | 403 | 无权限 | 学生读他人档案、员工读心理预警、NL2SQL 越权 |
| `40400` | 404 | 资源不存在 | 客户/学员/工单 ID 不存在 |
| `40900` | 409 | 冲突 | 手机号重复、请假单重复审批、幂等键冲突 |
| `50200` | 502 | 上游异常 | Dify 调用失败且降级不可用 |
| `50000` | 500 | 服务端内部错误 | 未预期异常兜底 |

### 5.2 两阶段意图路由与权限收敛

意图识别采用"廉价预筛 + 上游精判"的两阶段策略，目的是在保证准确率的同时压低每次对话的延迟与上游成本。

**第一阶段（本地预筛）** 用正则与关键词规则表匹配常见表达。例如消息中出现"请假""考务""申请单"即判定为 `student_helper` 的 `leave_apply` 意图。这一阶段不产生网络调用，覆盖高频简单问法。

**第二阶段（Dify 路由）** 预筛未命中时，调用 Dify 的 `router` 工作流，由其输出 `agent`、`intent`、`confidence` 三元组。服务层再按置信度分流：

- 置信度足够高 → 直接派发到目标 Agent 应答；
- 置信度偏低 → 返回澄清话术，请用户补充信息；
- 无有效意图 → 回落到该角色的默认 Agent。

**权限收敛是这一机制的安全核心。** 意图识别结果不能直接决定由哪个 Agent 应答，必须再经过角色白名单过滤：

| 角色 | 可访问 Agent 白名单 | 默认 Agent |
| --- | --- | --- |
| `visitor`（访客） | `customer_service` | `customer_service` |
| `student`（学员） | `student_helper`、`mental_care` | `student_helper` |
| `employee`（员工） | `student_helper`、`enterprise_assistant` | `enterprise_assistant` |
| `manager`（管理层） | `student_helper`、`enterprise_assistant`、`reporter` | `enterprise_assistant` |
| `admin`（管理员） | 全部六个业务 Agent | `enterprise_assistant` |

举例说明其价值：访客发送"我要请假 2 天"，意图识别确实会判出 `student_helper` 的请假意图，但白名单校验发现 `visitor` 无权访问该 Agent，于是自动降级为 `customer_service` 应答。这一降级已在冒烟测试中作为独立用例验证。

**角色来源的安全性。** 请求体中的角色字段一律不可信，服务端只从 Token 载荷中读取角色。访客令牌只携带 `anonymous: true` 标记，不查库、不落账号表，避免匿名用户污染账号体系。

### 5.3 Dify 集成实现

**一应用一 Key。** Dify 的 API Key 是按应用粒度签发的，格式以 `app-` 开头，不同应用互不通用。项目在配置中维护应用名到 Key 的映射，共对接 **7 个 Dify 应用**：

| 应用名 | 类型 | 端点 | 职责 |
| --- | --- | --- | --- |
| `router` | 工作流 | `/workflows/run` | 意图识别与分流 |
| `customer_service` | 对话型 | `/chat-messages` | 访客咨询、机构信息、预约到访 |
| `student_helper` | Chatflow（高级对话） | `/chat-messages` | 学员请假、成绩查询、投诉建议、活动报名（内部为 6 分支编排） |
| `enterprise_assistant` | 对话型 | `/chat-messages` | 员工日报录入、审批、数据速查 |
| `mental_care` | 对话型 | `/chat-messages` | 情绪安抚与心理支持 |
| `screener` | 工作流 | `/workflows/run` | 客户资质研判与结论输出 |
| `reporter` | 工作流 | `/workflows/run` | 日报/周报/月报生成 |

**两类端点的调用差异。** 对话型应用调用 `POST /v1/chat-messages`，请求体含 `query`、`response_mode`、`conversation_id`、`user`；工作流应用调用 `POST /v1/workflows/run`，请求体传命名参数 `inputs` 而非 `query`，结果从 `data.outputs` 读取。`user` 字段用于终态用户标识与会话隔离，项目以 `DIFY_USER_PREFIX` 前缀拼接业务用户 ID 后传入。

**模型接入：对话与嵌入均走云端。** 编排平台自身需要两类模型——负责推理的对话模型，以及负责把知识库文档向量化的嵌入模型。项目把两者都指向云端 API：**对话模型**走云端大模型，**知识库嵌入模型**走智谱 `embedding-3`（向量维度 2048）。这样做的直接收益是**全链路不再依赖本地常驻模型**——后端、数据库、编排平台与模型服务都可在无独立显卡的机器上运行，演示环境与生产环境的模型行为也保持一致。代价是推理与知识库建索引都需要外网连通，因此编排平台所在容器必须放行出网（默认的出网代理已覆盖该场景）。

**mock / live 双模式。** 这是本项目最重要的可测试性设计。`DIFY_MODE=mock` 时，`DifyClient` 不发起任何网络请求，而是返回确定性的构造响应：知识类问题返回内置知识库答案并附带引用来源，工作流类返回结构化的模拟输出，意图路由按关键词匹配表给出 `agent` 与 `intent`。这一机制带来三个直接收益：

1. **离线可测。** 完整的 503 条自动化测试与 149 条冒烟测试全部在无 Dify 实例的环境下运行并通过。
2. **演示零依赖。** 无网络、无 Dify 部署也能完整演示全部业务链路。
3. **降级有据。** `DIFY_MODE=live` 时，一旦上游超时、限流或返回错误，`DifyClient` 捕获异常后回落到同一套 mock 响应，保证接口不因上游抖动而不可用，同时在日志中留痕。

**流式输出。** `/chat/stream` 以 SSE 转发 Dify 的流式响应，逐条推送 `data:` 事件，最后以 `[DONE]` 收尾。实测一次完整流式应答产生 13 个事件。

**编排画布改用图编程。** 学员助手后来从单纯的「人设 + 知识库」对话型应用升级为 Chatflow：一个起始节点接一个**问题分类器**，把学员提问分成 6 类，再分别接「HTTP 请求取数 → LLM 整理 → 直接回复」的三节点分支（成绩 / 申请单 / 工单）、两个 LLM 引导分支（请假、投诉等写操作只做槽位抽取与二次确认引导，**不直接落库**），以及一个「知识库检索 → LLM → 回复」分支兜底。整图 18 个节点、17 条连线。

这样做的收益很直接：**能在画布上确定下来的分支就用画布表达，模型只负责"分到哪一类"这一件事**，比纯靠提示词约束稳定得多。代价是引入了几个只有在图编程里才会遇到的坑，记录如下：

1. **起始变量必须由服务端注入。** 前端刻意不向 Dify 传 `inputs`——角色一律以签发的 Token 为准，否则访客可以伪造 `role=admin`。因此 Chatflow 声明的 `role` / `student_id` 两个起始变量由后端统一填充，其中 `role` 始终以 Token 载荷**覆盖**客户端传值；`student_id` 只对学员角色注入（学员账号的业务主键即 `student_id`，而员工账号的对应字段指向员工表，塞进去是错的）。
2. **分类器的输出格式不能自己描述。** 问题分类器节点在运行时会自动拼接一段 system 提示词，要求模型返回 `{"category_id":…,"category_name":…}` 并据此解析。若在节点的 `instruction` 里再写一句"只输出类别的编号"，模型会真的返回一个裸值，解析器找不到 `{` 便直接报错中断。正确做法是：`instruction` 只写**判断依据**，一个字都不提输出格式。
3. **所有节点都要显式关闭思考模式。** 推理型模型的思维链会占满输出，把结构化结果挤掉。项目对所有节点统一设置思考模式关闭。
4. **会话 ID 不能跨应用复用。** Dify 的 `conversation_id` 是**按应用隔离**的，而前端只维护一个全局会话标识；当问题从客服类切到学员助手时，把上一轮别的应用返回的会话 ID 递过去，平台会以"会话不存在"拒绝。此时若被笼统的降级逻辑接走，用户会拿到一段**与问题无关的兜底话术**，而界面上只留一个不显眼的降级标记——属于典型的静默失败。修复方式是**定向重试**：只在响应码为 400/404 且响应体命中会话不存在特征时，丢弃该会话 ID 重开一次；其余错误（5xx、鉴权失败、连接异常）仍然照常降级且只调用一次。
5. **用 API 建图时节点类型要按画布的约定写。** 编排画布给图形引擎只注册了一个节点组件，真正的节点种类放在节点数据的 `type` 字段里；若在节点的顶层 `type` 上也写了业务种类，图形引擎找不到对应组件，会退化成内置的默认节点——画布上表现为一排 150×21 的空白细条，节点既看不出内容也点不进去，同时右侧检查清单还会误报"缺少回复节点"。**后端执行只读节点数据里的 `type`，因此这类问题不影响运行，只损坏编辑体验**，排查时容易被误判为程序缺陷。

**知识库与嵌入模型。** 编排平台的知识库（数据集）在写入文档时调用嵌入模型生成向量索引，检索节点据此召回相关片段。项目把嵌入模型由早期的本地模型切换为**智谱云端 `embedding-3`**（维度 2048）：切换在平台侧自动重建索引，**数据集标识不变**，因此画布上的知识检索节点与后端的知识文档登记功能都无需任何改动。这条链路上有两个容易被忽略的点——其一，**嵌入模型决定向量空间，换模型等于换坐标系**，旧的向量索引在新模型下不可用，必须整体重建（本地 bge-m3 维度为 1024，与云端 2048 不同，更需重建）；其二，**更新知识库配置的接口要求请求体同时携带“索引方式”字段**，缺失时后端读取该字段会直接抛错并返回 500，而不是给出友好的参数校验提示。验证切换是否真正生效，**不能只看配置回显**——必须用一次真实检索来证明：能命中相关内容，才说明文档已用新模型重新嵌入。

### 5.4 语音录入实现

语音录入被抽象为 `AsrProvider` 协议，有两个实现：`MockAsr`（离线确定性转写，用于测试与演示）与 `DifyAsr`（调用 Dify 的音频转文字能力）。由 `ASR_PROVIDER` 环境变量选择，接口层完全无感知。

转写文本之后，再做一层**槽位抽取**，把自然语言转换为结构化字段。例如"我要请假 2 天，原因是看病"会被抽取为：

```json
{ "start_date": "2026-09-15", "days": 2, "reason": "看病" }
```

抽取逻辑用轻量正则实现（日期、天数、事由、分数、考试名称等），关键在于它把"听写"变成了"填表"——上层业务拿到的是可直接落库的结构化数据，而不是一段需要二次解析的文本。冒烟测试对抽取结果做了逐字段断言。

### 5.5 受控 NL2SQL 实现

自然语言转 SQL 是这类系统最大的数据安全风险点：一旦允许模型自由生成 SQL，就可能出现越权读取、全表扫描、甚至数据破坏。本项目采取**"模型只做选择题，不做填空题"**的受控方案：

**第一层，模板白名单。** 系统预置 **8 个查询模板**，覆盖高频数据需求：

| 模板 ID | 用途 |
| --- | --- |
| `lead_followups` | 按客户姓名查跟进记录 |
| `lead_status_stats` | 客户线索状态统计 |
| `student_scores` | 学员成绩查询 |
| `score_averages` | 各科平均分统计（只收「各科平均分」等明确组合，不收裸「平均分」） |
| `pending_requests` | 待审批申请列表 |
| `open_tickets` | 未完结工单列表 |
| `activity_enrollment` | 活动报名情况 |
| `mental_alerts` | 心理预警（限管理层） |

模型的任务被压缩为：从这 8 个模板中选出最匹配的一个，并抽取其中的参数（如客户姓名）。SQL 语句本身由服务端从模板拼装，模型永远不产出原始 SQL。

**第二层，危险关键字拦截。** 拼装后的 SQL 在真正执行前要再过一道校验。校验分两类策略：

- 词边界匹配拦截结构性写操作：`INSERT`、`UPDATE`、`DELETE`、`DROP`、`ALTER`、`TRUNCATE`、`CREATE`、`REPLACE`、`GRANT`、`REVOKE`、`ATTACH`、`PRAGMA`。
- 子串匹配拦截高危函数与元数据探测：`INTO OUTFILE`、`LOAD_FILE`、`INFORMATION_SCHEMA`、`SLEEP(`、`BENCHMARK(`。

这里有一个实现细节值得记录：最初的版本把 `create` 作为普通子串拦截，结果误伤了 `created_at`、`updated_at` 这类合法列名，导致正常查询被拒。修正方案是把结构性关键字改为**词边界正则**（`\b(?:insert|update|...)\b`），与子串类规则分离，既堵住了写操作，又不伤及合法标识符。

**第三层，表名白名单与行数上限。** SQL 中出现的表名必须落在允许的 17 张业务表内（`ALLOWED_TABLES`，不含新增的 `todo_push` 与 `sys_account`）；任何查询的结果集被强制限制在 `NL2SQL_MAX_ROWS`（默认 50）行以内，防止全表拉取。

**第四层，角色作用域。** 敏感模板（如心理预警）与角色绑定。普通员工请求该模板时直接返回 403，而非返回空结果——拒绝要明确，避免被探测出"数据存在但读不到"。

### 5.6 幂等设计与审计

**幂等。** 跟进记录创建与请假申请提交这类写操作，要求客户端携带 `Idempotency-Key`。服务端先按键查询：已存在则原样返回该记录并标记 `duplicated: true`；不存在才落库。这解决了移动端弱网重试、用户连点造成的重复数据问题。

**审计。** 所有写操作在提交事务的同时写入 `audit_log`，其中包含当前请求的 `trace_id`。审计日志的查询接口本身也受权限管控——仅 `manager` 及以上角色可读，普通员工访问返回 403。

### 5.7 Dify 回调内部工具

编排层（Dify）在推理过程中需要读取业务数据，例如"这个学员的成绩是多少""当前有哪些待审批申请"。这部分能力通过内部工具接口开放给 Dify：

```text
GET  /internal/tools              # 工具清单
POST /internal/tools/{tool_name}  # 执行指定工具
```

当前开放 **26 个工具**（只读 12 个、写入 14 个），每个工具都声明了角色门槛与入参 JSON Schema。`GET /internal/tools` 除返回清单外，还返回一份**角色—工具覆盖矩阵**，便于核对"哪类角色能调用哪些能力"。只读工具覆盖客户查询、成绩查询、待办与工单、组织架构、报告取数等；写入工具覆盖线索创建、跟进记录、请假审批、投诉处理、日报提交、待办推送等。

**两种鉴权，满足其一即可。** 这些接口不依赖用户 Token（调用方是 Dify 服务端而非终端用户），采用两条通道：

- **HMAC-SHA256 签名**（主通道）：请求方携带 `X-Tool-Timestamp`（请求时间戳）与 `X-Tool-Signature`（对"时间戳 + 原始请求体"计算的签名）。服务端校验签名是否匹配，并检查时间戳与当前时间的偏差是否在 300 秒以内（防重放）。签名错误返回 403，超时同样拒绝。
- **静态 Bearer Key**（兼容通道）：编排平台的"自定义工具"功能只支持 none / api_key / bearer 三种鉴权方式，**不会在调用前现场计算 HMAC**，因此额外开放一条 `Authorization: Bearer <DIFY_TOOL_KEY>` 通道。该值留空即关闭此通道；启用时应在网关层限定只允许编排平台所在网段访问 `/internal/tools*`。两条通道不叠加：一旦携带签名头，就只按签名校验，不会回退到静态 Key。

**工具级再按角色过滤。** 通过接口鉴权后，每个工具还会依据调用者角色做一次门槛判断（`visitor < student < employee < manager < admin`），角色不足返回 403。这里有一处刻意的分工：早期为顾问与管理层编写的只读工具，角色门槛是 `employee` 且查询参数可选（不传即跨对象返回），**不能为了让学员也能用而降低其门槛**——那等于把跨学员的数据暴露出去。正确做法是另开一个"只能查本人"的同款工具（强制要求传入本人标识、门槛对齐学员角色），并用一条回归测试锁住两者的分工。

**写操作两阶段确认。** 写入类工具不会一调就落库：首次调用返回 `{needs_confirm, preview, token}`，其中 `preview` 是即将写入内容的人可读摘要；调用方带着 `token` 并把 `confirm` 置为真后再调一次才真正提交。这样"先给用户看要写什么、确认了再写"的交互由接口层强制保证，而不是依赖编排侧的提示词自觉。

冒烟测试中包含一条"伪造签名被拒"的负向用例，用于验证这道防线。

### 5.8 材料上传解析与批量研判

客户研判的入口从"手工填字段"前移到"上传原件"。`POST /screening/upload` 接收 multipart 文件，落盘后用 `services/material.py` 解析并抽取字段。

**解析走双路并存。** 优先调用 `pypdf` / `pdfplumber` / `openpyxl`，缺库时自动回落到纯标准库实现（PDF 解 FlateDecode 内容流，XLSX 解 zip 内的 `sharedStrings.xml` 与 `sheetN.xml`）。响应里用 `parser` 字段**显式标明本次走了哪条路**，并用 `warnings` 说明局限——不允许静默降级。

**抽取用规则不用模型。** regex 抽取 9 个关键字段；横向表（表头行 + 值行）先按列名还原成"标签 | 值"再抽；`_LABEL_WORDS` 黑名单挡住"把表头字段名当成值"。`extract_fields()` 只返回**确实抽到**的键，缺失由 `missing_fields()` 单独计算，不往字段里塞占位符。

**人工复核留痕。** `lead_screening` 的 `ai_conclusion` 存 AI 原值（写一次不再改），`conclusion` 是对外生效值；`review_status ∈ PENDING / CONFIRMED / OVERRIDDEN`。`PATCH /screening/{id}/review` 支持确认与推翻两种动作，`GET /screening/corrections` 输出"AI 结论 → 人工结论"的迁移分布，作为规则优化素材。

**批量研判按 SAVEPOINT 隔离单条失败。** `POST /screening/batch` 支持按材料清单或客户 ID 清单批量发起，单条失败用 `with db.begin_nested():` 隔离，不用 `rollback()`——否则会把已成功的整批带走。响应返回批次号与成败计数，`batch_id` 支持按批回捞。

> 字段口径收敛：规范键名为 `name / age / degree / school / major / language / gpa / intention_country / intention_stage`，Dify 侧返回的 `country`、`education` 等由 `normalize_fields()` 归一，避免同一份材料在不同入口抽出不同键名。

### 5.9 组织架构查询与新人入职指引

`app/api/v1/org.py` 提供只读的组织视图：`/org/tree` 按 `employee.manager_id` 递归成树（带成环断开的兜底），另有部门概览、员工花名册（关键字检索）与员工详情（含汇报关系）。全部 `staff_only`，不写审计。

入职指引不是散落在页面里的文案，而是**版本化的内容资产**（`app/services/onboarding.py`，`GUIDE_VERSION=V2026.09`）：5 个阶段（D0 / D1 / W1 / M1 / M3）共 19 条步骤、12 条 FAQ 与关键联系人规则表。`resolve_contacts()` 从 `employee` 表现算直属上级、部门负责人与带教，**查不到就如实给出说明，不编造姓名**。

FAQ 检索用两级策略：归一化子串命中给满分，未命中再算字符二元组 Dice 相似度（阈值 0.30），因此整句提问（如"报到当天要带什么"）也能召回。指引同时登记进 `knowledge_doc`（`category=NEO`），对话入口预筛 `(入职指引|新人指引|入职流程|…)` 即路由到 `enterprise_assistant` 的 `onboarding` 意图，答复带版本出处。

### 5.10 主动待办推送与心理预警触达

**待办是"算"的，推送记录才是"存"的。** `services/todo.collect()` 每次现场扫 5 类来源（待审批申请 / 待跟进工单 / 到期客户跟进 / 待复核研判 / 未触达心理预警）——业务处理完，待办自然消失，不再维护一张会失同步的"待办表"。`todo_push` 表只记录"推过什么、推给谁、处理没有"，靠 `dedupe_key`（`category:subject:时间窗`）实现**幂等 + 频控**：同一时间窗不重复提醒同一个人。

`collect()` 覆盖的 5 个类别与可见角色如下：

| 类别 | 含义 | 可见角色 |
| --- | --- | --- |
| `approval` | 待审批申请（请假等） | 员工及以上 |
| `ticket` | 未完结工单 | 员工及以上 |
| `followup` | 到期 / 逾期客户跟进 | 员工及以上 |
| `screening_review` | 待人工复核的研判结果 | 员工及以上 |
| `mental_alert` | 未触达的心理预警 | 仅管理层 |

**调度器只管"什么时候跑"。** `services/scheduler.py` 是 stdlib threading 实现的极简调度器（不引 APScheduler），业务逻辑全在 `todo.push_due()` / `run_once()`，因此定时线程、`POST /todo/push` 手动触发、pytest 直接调用走的是**同一条逻辑**。`ENABLE_SCHEDULER` 默认 **false**——测试与 E2E 同进程同数据，后台线程会写脏 `todo_push`。

**心理预警是敏感数据。** 只对 `manager` / `admin` 出现，与 `/alerts` 的 `manager_only` 同口径；意图路由层 employee 对 enterprise_assistant 本就放行，所以权限**必须在 handler 里再卡一道**。触达即写 `notified_at` / `notified_to` / `notify_channel` / `intervention`，处理人取"学生带教顾问 → 兜底管理层"，找不到就如实写在 note 里。`POST /todo/push` 处理 `mental_alert` 类时会**顺带自动触达**，否则"未触达预警"会永远挂在待办上。标记已处理（`ack`）刻意做成幂等，重复调用返回 `already=True`，不刷时间也不报 409。

### 5.11 五类业务报告与定时导出

**先快照、再渲染。** `services/reports.build()` 只跑一次聚合，结果写进 `ReportRecord.snapshot`（JSON）；markdown / PDF / xlsx / 打印版四个出口**只读这一份**，杜绝"同一天导出的两份数字不一样"这类口径漂移。

五类口径：`customer_ops` / `daily_digest` / `weekly_digest` / `mental_weekly` / `complaint_weekly`。历史的 `weekly | monthly | custom` 由 `normalize_type()` 映射到最接近的新类型，**不返回 400**，避免打断旧调用方。

| key | 报告 | 粒度 | 覆盖范围 |
| --- | --- | --- | --- |
| `customer_ops` | 全域客户经营分析报告 | 周（7 天） | 意向 / 成交 / 流失三大客群 + 同环比 + 共性画像 + 流失预警 |
| `daily_digest` | 员工日报汇总报告（日） | 日（1 天） | 当日全员工日报的核心进展 / 关键产出 / 潜在风险 |
| `weekly_digest` | 员工日报汇总报告（周） | 周（7 天） | 近一周日报按人聚合：产出、覆盖度、风险集中度 |
| `mental_weekly` | 学生心理健康周报 | 周（7 天） | 情绪态势 / 高危名单 / 周期节点 / 疏导建议 |
| `complaint_weekly` | 投诉处理周报 | 周（7 天） | 投诉量同环比 / 分类 / 处理时效 / 满意度 / 长期未决 |

四种导出格式，全部零第三方依赖：

- `xlsx`：`services/xlsx_writer.py` 纯标准库写（inlineStr + 冻结首行 + 中文列宽粗算），用项目自己的读端 `material._xlsx_builtin` 做往返测试。
- `print`：打印就绪 HTML，浏览器用系统字体排版后「另存为 PDF」，**界面主推**此路径，中文渲染一定正确。
- `pdf`：`services/pdf_writer.py` 纯标准库直出矢量 PDF（Type0 / STSong-Light / UniGB-UCS2-H + 自写 ToUnicode CMap）。文字可提取，但**不嵌字体文件**——实测 Edge / Chrome 的 PDFium 会把中文渲染成乱码（布局、表格、页脚均正常，仅字形错误），Acrobat / WPS 正常。要"任何阅读器都对"必须嵌中文字体子集（属部署期依赖），这一局限已在界面与文档中明说。
- `md`：`render_markdown()` 是 `ReportRecord.content` 的来源。

**定时生成**由纯函数 `due_types(now)`（日报类每天、周报类周一）+ `run_scheduled()` 组成，按 `(report_type, period_start, period_end, from_schedule=True)` 幂等；入口是 `POST /reports/scheduled/run`，也挂在 `scheduler.tick()` 里。中文文件名导出走 RFC 6266 的 `filename*=UTF-8''` 形式（`ops.py::_content_disposition`），否则 Starlette 会按 latin-1 编码抛 `UnicodeEncodeError`。

## 6. 接口清单

系统对外暴露 **82 条路径、91 个接口操作**，分为 10 个功能组。全量接口定义可通过 `/openapi.json` 或 `/docs` 交互式文档获取。

| 功能组 | 接口数 | 主要接口 |
| --- | --- | --- |
| `system` 系统 | 6 | `GET /health`、`GET /ready`、`POST /auth/token`、`POST /auth/visitor`、`GET /auth/me`、`POST /auth/password` |
| `chat` 对话 | 3 | `POST /chat/message`、`POST /chat/stream`、`POST /chat/asr` |
| `crm` 客户 | 20 | `POST/GET /leads`、`GET /leads/{id}`、`POST/GET /leads/{id}/followups`、`PATCH /leads/{id}/status`、`POST/GET /screening`、`POST /screening/analyze`、`POST /screening/batch`、`POST /screening/upload`、`GET /screening/{id}`、`PATCH /screening/{id}/review`、`GET /screening/corrections`、`GET /screening/files/{path}`、`GET/POST /screening/rules`、`GET /screening/rules/active`、`GET /screening/rules/template`、`POST /screening/rules/{id}/activate`、`POST /screening/rules/{id}/archive` |
| `student` 学员 | 22 | `GET /students`、`GET /students/{id}`、`GET /students/{id}/progress`、`POST /students/{id}/progress`、`GET /students/{id}/progress-board`、`POST/GET /scores`、`POST /leave/apply`、`POST /leave/{id}/approve`、`GET /requests`、`POST/GET/PATCH /tickets`、`GET/PATCH /alerts`、`POST /alerts/notify`、`GET/POST /students/{id}/deadlines`、`GET /students/{id}/deadline-notifications`、`PATCH /deadlines/{id}`、`POST /deadlines/run-reminders`、`GET /deadlines/run-reminders/preview` |
| `org` 组织与指南 | 8 | `GET /org/tree`、`GET /org/departments`、`GET /org/employees`、`GET /org/employees/{id}`、`GET /org/onboarding/guide`、`GET /org/onboarding/checklist`、`GET /org/onboarding/faq`、`GET /org/onboarding/contacts` |
| `operation` 运营 | 18 | `GET/POST /activities`、`POST /activities/{id}/enroll`、`GET /activities/{id}/enrollments`、`GET /reports/types`、`POST /reports/generate`、`GET /reports`、`GET /reports/{id}`、`GET /reports/{id}/download`、`GET /reports/{id}/export`、`GET /reports/{id}/deliveries`、`POST /reports/{id}/push`、`POST /reports/scheduled/run`、`POST /reports/daily`、`GET /reports/daily/summary`、`GET /reports/daily/trend`、`POST/GET /kb/documents` |
| `todo` 待办与预警 | 4 | `GET /todo/pending`、`POST /todo/push`、`GET /todo/pushes`、`PATCH /todo/pushes/{id}/ack` |
| `data` 数据 | 3 | `POST /nl2sql/query`、`GET /nl2sql/templates`、`GET /audit/logs` |
| `notifications` 通知 | 5 | `GET /notifications`、`GET /notifications/unread-count`、`POST /notifications/{id}/read`、`POST /notifications/read-all`、`GET /notifications/channels` |
| `internal-tools` 内部 | 2 | `GET /internal/tools`、`POST /internal/tools/{name}` |

## 7. 部署实施

### 7.1 部署方式一：本地 SQLite（推荐用于开发与演示）

这是最快跑通的路径，实测从零到服务可用约 3 分钟。

**步骤 1：创建虚拟环境并安装依赖。**

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

**步骤 2：准备配置。**

```bash
copy .env.example .env      # Linux/macOS: cp .env.example .env
```

默认配置即为 SQLite + mock 模式，开箱即用。若接入真实 Dify，修改 `DIFY_MODE=live`、`DIFY_BASE_URL` 与 `DIFY_APP_KEYS` 即可。

**步骤 3：初始化并填充种子数据。**

```bash
python scripts/seed.py
```

种子脚本是**幂等**的，重复执行不会产生重复数据；需要清空重建时使用 `python scripts/seed.py --reset`。执行后会写入 5 个演示账号、3 名员工、3 名学员、3 条意向客户，以及成绩、申请、工单、预警、活动、课程项目、知识文档与日报等配套数据。

**步骤 4：启动服务。**

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8010
```

启动日志会明确打印当前生效的运行模式，便于确认配置是否按预期加载：

```text
启动完成：db=sqlite dify=mock asr=mock
```

**步骤 5：验证。**

```bash
curl http://127.0.0.1:8010/api/v1/health
```

预期返回 `{"code": 0, ...}`。交互式文档位于 `http://127.0.0.1:8010/docs`。

> 推荐直接用项目自带的一键启动器：`python scripts/launcher.py --headless --no-browser`
> （或双击 `start.bat`），它会自动挑空闲端口、建库灌种子并探活；默认后端 **8010**、前端 **8020**。
> 端口以 `scripts/launcher.py` 为唯一权威源，各脚本请勿自行硬编码。

### 7.2 部署方式二：MySQL 8 + Docker Compose（推荐用于生产）

**数据库连接串切换。** 唯一需要修改的配置项是 `DATABASE_URL`：

```text
DATABASE_URL=mysql+pymysql://ai_user:ai_password@127.0.0.1:3306/ai_assistant?charset=utf8mb4
```

**安装 MySQL 驱动。**

```bash
pip install -r requirements-mysql.txt
```

**使用 Docker Compose 一键启动。** 项目提供的 `docker-compose.yml` 同时编排 `mysql:8` 与 API 服务两个容器：

```bash
docker compose up -d --build
```

API 容器基于 `python:3.12-slim`，内置健康检查；它通过 `depends_on` 等待 MySQL 就绪后再启动，避免连接竞态。

**表结构创建。** 应用启动时 `init_db()` 会依据 ORM 元数据自动建表。生产环境若需版本化演进，建议引入 Alembic 管理迁移，`target_metadata` 指向 `Base.metadata` 即可。

### 7.3 生产部署检查清单

上线前请逐项确认：

1. `SECRET_KEY` 已替换为 32 字节以上的随机值，且未提交进版本库。
2. `DATABASE_URL` 指向 MySQL，且连接串已启用 `utf8mb4` 字符集。
3. `CORS_ORIGINS` 已从 `["*"]` 收紧为实际前端域名。
4. `DIFY_MODE=live` 且 7 个应用的 Key 均已配置，`DIFY_BASE_URL` 指向内网或受控地址。
5. 反向代理（Nginx / 网关）已配置 TLS，并将 `/internal/tools*` 限制为仅 Dify 服务可访问——该路径使用签名鉴权而非用户鉴权，不应暴露在公网。
6. MySQL 已配置定期备份，`audit_log` 表按业务要求设定保留周期。
7. `NL2SQL_MAX_ROWS` 依据实际数据量调整，避免大结果集拖垮接口。
8. 已接入日志采集，`X-Trace-Id` 可跨服务检索。

### 7.4 演示账号

种子数据创建的账号如下（密码仅用于开发与演示，上线前须全部重置）：

| 用户名 | 密码 | 角色 | 说明 |
| --- | --- | --- | --- |
| `admin` | `admin123` | `admin` | 系统管理员，可访问全部 Agent |
| `manager` | `manager123` | `manager` | 运营管理层，可读心理预警与审计日志 |
| `advisor` | `advisor123` | `employee` | 留学顾问 |
| `teacher` | `teacher123` | `employee` | 教学老师 |
| `student` | `student123` | `student` | 在读学员 |

此外无需账号即可通过 `POST /api/v1/auth/visitor` 获取访客令牌，用于体验咨询类对话。

## 8. 测试与验收

测试分为两层：**单元/集成测试**验证代码逻辑与接口契约，**端到端冒烟测试**验证真实部署后的完整业务链路。

### 8.1 单元与集成测试

**执行命令：**

```bash
pytest -q --cov=app --cov-report=term-missing
```

**实测结果：503 条用例全部通过，整体覆盖率 93%（TOTAL 5576 语句 / 400 未覆盖）。**

测试夹具（`tests/conftest.py`）在导入应用前先设置环境变量（独立测试数据库、`DIFY_MODE=mock`、32 字节以上密钥），确保测试过程不触碰开发数据库、不发起外部网络请求。数据库使用会话级夹具，在测试会话开始时整体重建并灌入种子数据。

**覆盖率的分布说明：**

| 模块 | 覆盖率 | 说明 |
| --- | --- | --- |
| `app/api/v1/notice.py` | 100% | 通知与消息中心接口 |
| `app/api/v1/router.py` | 100% | 路由挂载 |
| `app/main.py` | 100% | 应用装配 |
| `app/models.py` | 100% | 数据模型定义 |
| `app/schemas.py` | 100% | 请求/响应契约（只保留被引用的 31 个模型） |
| `app/services/audit.py` | 100% | 审计日志 |
| `app/services/notify.py` | 100% | 通知分发（通道就绪判定、外发失败兜底落站内、已读闭环） |
| `app/services/asr.py` | 100% | 语音识别（Provider 选择、上游异常包装、槽位抽取） |
| `app/api/v1/system.py` | 100% | 系统与认证（健康探针降级、停用账号、改密与审计） |
| `app/services/scheduler.py` | 100% | 后台调度线程（起停状态机 / 循环兜异常 / 三任务编排） |
| `app/api/v1/org.py` | 98% | 组织架构与入职指引接口 |
| `app/api/v1/todo.py` | 97% | 主动待办与推送留痕接口 |
| `app/config.py` | 97% | 配置加载与校验 |
| `app/db.py` | 96% | 数据库层（补列 / 默认值渲染；MySQL 方言分支离线不可达） |
| `app/services/reports.py` | 96% | 五类报告聚合与四出口渲染 |
| `app/api/v1/data.py` | 95% | 数据洞察接口 |
| `app/services/todo.py` | 95% | 待办现场计算、推送幂等与预警触达 |
| `app/services/xlsx_writer.py` | 94% | 纯标准库写 xlsx |
| `app/api/v1/ops.py` | 94% | 运营域接口（含报告导出） |
| `app/services/nl2sql.py` | 94% | 受控 SQL |
| `app/services/onboarding.py` | 93% | 入职指引内容资产与联系人解析 |
| `app/core.py` | 92% | 核心工具 |
| `app/api/v1/crm.py` | 92% | 客户域接口（含材料上传解析、复核、批量研判） |
| `app/api/v1/tools.py` | 91% | 内部工具接口入口 |
| `app/api/v1/student.py` | 91% | 学员域接口 |
| `app/services/intent.py` | 90% | 意图路由 |
| `app/api/deps.py` | 90% | 鉴权依赖 |
| `app/services/deadline.py` | 89% | 学业考务与考前提醒 |
| `app/services/rules.py` | 89% | 画像研判规则引擎 |
| `app/api/v1/chat.py` | 88% | 对话接口（含编排输入注入与会话重试） |
| `app/services/pdf_writer.py` | 88% | 纯标准库写 PDF |
| `app/services/dify.py` | 85% | Dify 客户端（mock/live 双模与降级） |
| `app/services/material.py` | 82% | 材料解析与字段抽取（双路并存） |
| `app/services/mock_agent.py` | 82% | mock 模式本地编排 |
| `app/services/agent_tools.py` | 78% | 内部工具注册表（26 个工具的 Schema 与角色门槛） |

**关于未覆盖部分的说明。** 缺口集中在两类离线不可达的分支：`app/db.py` 的 MySQL 连接池分支、以及 `app/services/dify.py` 的 `live` 模式路径。此外 `app/services/agent_tools.py`（内部工具注册表，26 个工具的 Schema 与处理器）虽然鉴权通道与角色门槛都有单元测试覆盖，但其中相当一部分处理器是专为 `live` 编排链路准备的，需要真实 Dify 实例与真实业务数据才能端到端触发，因此单测覆盖率低于整体水平——这部分缺口由编排侧的逐分支联调补上。以上都依赖真实外部依赖（MySQL 实例、Dify 服务）才能在测试中触发，属于**已知且被接受的缺口**。项目通过把 mock / 前台触发设计为默认路径，使其余全部业务逻辑都在测试覆盖之内。上线前建议在预发环境针对 `live` 模式与 MySQL 补一轮集成验证。

### 8.2 端到端冒烟测试

**执行方式：** 先启动服务，再对真实运行中的服务发起 HTTP 请求。

```bash
# 终端 1
uvicorn app.main:app --host 127.0.0.1 --port 8010
# 终端 2
python scripts/smoke_test.py --base http://127.0.0.1:8010
```

**实测结果：149 条用例全部通过。** 无 Dify 实例的离线环境下累计请求耗时 563 毫秒（mock 快路径）；接入真实 Dify 后约 30 秒，因含真实模型往返。测试报告自动写入 `reports/smoke_report.md`。

覆盖的链路与关键断言如下：

| 链路 | 用例数 | 关键断言 |
| --- | --- | --- |
| 存活与认证 | 9 | 健康检查、就绪探针、OpenAPI 可读、三角色登录、访客令牌；错误密码与无 Token 均被 401 拒绝 |
| 对话与语音 | 9 | 客服问答命中正确 Agent 且带引用；**访客说请假被降级为客服 Agent**；流式响应收到 13 个 SSE 事件；语音槽位抽取字段逐项正确 |
| 客户域 | 32 | 新增客户成功；重复手机号返回 409；跟进记录重复提交同一幂等键返回同一条；材料上传解析抽齐关键字段且原件可下载；人工复核确认 / 推翻均正确回写；批量研判按批号回捞、单条失败被隔离、超上限被拒 |
| 学员域 | 13 | 越权读他人档案返回 403；请假申请经审批后由 `PENDING` 流转为 `APPROVED`；**重复审批返回 409**；工单创建与关闭；心理预警管理层可读、普通员工被拒 |
| 运营域 | 9 | 活动创建与报名；报告经 Dify 工作流生成后可下载（响应头含 `attachment`）；日报提交与汇总；知识文档登记 |
| 组织架构与新人指引 | 17 | 组织架构树按汇报线成树；部门概览与花名册关键字检索；员工详情含汇报关系；入职指引五阶段齐全且每项都有负责人与入口；FAQ 整句提问可召回；组织信息对学生返回 403 |
| 主动待办与预警触达 | 25 | 四类业务待办 + 主动询问话术 + 不含敏感类；管理层待办 ⊇ 员工待办；推送落库并命中同时间窗频控；标记已处理幂等；预警触达幂等且重复不重写；待办与预警对访客返回 403 |
| 智能报告 | 22 | 五类报告口径齐全；逐类生成并断言报告期 + 指标 + 结论来自真实聚合；四种导出格式（xlsx / pdf / print / md）均返回正确字节；未知类型与不支持格式被 400 拒绝；导出限管理层；定时生成幂等只跳过不重复 |
| 数据能力 | 7 | NL2SQL 命中 `lead_status_stats` 模板并返回预览 SQL；人名参数正确注入；**越权查敏感模板返回 403**；审计日志管理层可读、员工被拒 |
| 内部工具 | 6 | 工具清单可读（返回 26 个工具与角色覆盖矩阵）；4 个代表性工具在正确签名下调用成功；**伪造签名返回 403** |

**测试中发现并修复的问题。** 首次运行冒烟测试时有 1 条用例失败：`/openapi.json` 被判定为失败。排查后确认这是**测试脚本自身的断言缺陷**而非应用缺陷——OpenAPI 规范文档不是业务响应，本就不含统一响应体的 `code` 字段，而脚本却断言 `code == 0`。修正为仅校验 HTTP 200 后，149 条用例全部通过。

### 8.3 开发阶段修复的缺陷记录

除上述测试脚本问题外，开发过程中通过测试与联调暴露并修复了 7 个真实缺陷，记录如下，供后续维护参考：

| 编号 | 现象 | 根因 | 修复方式 |
| --- | --- | --- | --- |
| 1 | 访客令牌访问接口返回 401"账号不存在" | `get_principal` 对匿名访客也执行了 `sys_account` 查库，但访客并无账号记录 | 检测到令牌中 `anonymous` 为真时，直接由令牌声明构造主体，不查库 |
| 2 | 重复手机号返回 `40000` 而非 `40900` | `create_lead` 使用了通用异常并手工指定 HTTP 409，未走冲突异常 | 改为抛出语义化的 `Conflict` 异常 |
| 3 | 已停用账号登录返回 `40000` 而非 `40300` | `auth/token` 同样使用了通用异常 | 改为抛出 `Forbidden` 异常 |
| 4 | 合法列名 `created_at` 被 NL2SQL 拦截 | 危险关键字以普通子串方式匹配，`create` 命中了 `created_at` | 结构性关键字改为词边界正则匹配，与子串类规则分离 |
| 5 | 学员在自己的会话里查询成绩返回 403"权限不足" | 早期为顾问与管理层编写的只读工具，角色门槛是 `employee` 且查询参数可选（不传即跨对象返回）；学员角色等级更低，被工具级门槛拦下 | **不放宽原有工具的门槛**（放宽等于允许跨学员查询），另开一个"只能查本人"的同款工具：强制要求传入本人标识、门槛对齐学员角色，并补一条回归测试锁住两者的分工 |
| 6 | 换一类问题后，回复变成一段与提问无关的通用套话 | 编排平台的会话标识**按应用隔离**；问题在应用间切换时把上一个应用的会话标识递过去，平台以"会话不存在"拒绝，该异常被笼统的降级逻辑接走，用户拿到兜底话术，界面上只留一个不显眼的降级标记 | 改为**定向重试**：仅在响应码为 400/404 且响应体命中"会话不存在"特征时丢弃会话标识重开一次；其余错误（5xx、鉴权失败、连接异常）仍照常降级且只调用一次，并补回归测试锁住这条边界 |
| 7 | 编排画布上节点渲染成一排空白细条、点不进去，右侧检查清单误报"缺少回复节点" | 用接口建图时，把节点业务种类同时写到了节点的**顶层 `type`**；画布给图形引擎只注册了一个节点组件，业务种类应只放在节点数据里，顶层写了别的值会导致组件查找失败、退化为内置空节点 | 顶层 `type` 统一按画布约定值写入，业务种类只保留在节点数据中；同步补一条建图自检（整改前 4 个工作流图全部命中） |

缺陷 1、2、3 反映的是同一类问题：**异常语义与错误码的映射必须由专用异常类型承载，而不是靠调用处手工指定 HTTP 状态码**。已统一为按异常类型映射错误码。缺陷 4 则说明安全过滤规则的精度与覆盖面同样重要——过宽的规则会误伤正常功能，过窄则留有缺口。缺陷 5 是权限设计中"**宁可新开一个窄口子，也不放宽原有的宽口子**"的一次具体实践。缺陷 6 与 7 同属"**把上游的异常当成普通故障一并兜底**"造成的静默失败：前者让用户拿到与问题无关的答案，后者让交付物的编辑入口直接不可用。两者都指向同一条经验——降级与容错必须按错误语义分类处理，平台侧的结构约定要当作契约对待；同时这两类问题的共同特征是**不影响接口返回、不容易被自动化测试发现**，因此需要专门的正向/负向用例来锁住。

### 8.4 验收结论

| 验收项 | 标准 | 实测 | 结论 |
| --- | --- | --- | --- |
| 单元/集成测试 | 全部通过 | 503 / 503 通过 | 通过 |
| 代码覆盖率 | ≥ 80% | 93% | 通过 |
| 端到端冒烟测试 | 全部通过 | 149 / 149 通过 | 通过 |
| 前端端到端（真浏览器） | 全部通过 | 37 / 37 通过，0 页面异常 | 通过 |
| 权限隔离 | 越权访问被拒 | 全部越权/负向用例均按预期拒绝 | 通过 |
| 幂等性 | 重复提交不产生重复数据 | 跟进记录、请假审批、待办推送、预警触达、定时报告均验证通过 | 通过 |
| 内部工具鉴权 | 伪造签名被拒 | 返回 403 | 通过 |
| 双数据库兼容 | SQLite 与 MySQL 均可运行 | SQLite 已实测；MySQL 通过方言适配与容器编排支持 | 通过（MySQL 建议预发复验） |

## 9. 已知限制与后续演进

**已知限制：**

1. **Dify `live` 模式已打通，但尚未纳入自动化回归。** 本机已部署真实 Dify 实例，完成 7 个应用的接线与逐分支联调，并针对跨应用会话、跨角色越权、知识库命中三类场景做了实测；但该分支依赖外部实例与云端模型服务，未进入日常测试链路。日常回归仍由 mock 分支保证可测性（503 条用例全部离线通过）。建议在预发环境把它做成定期集成验证。
2. **NL2SQL 的模板式方案覆盖面有限。** 当前仅支持 8 类预置查询，超出范围的提问无法应答。这是安全性与灵活性的主动取舍——若后续需要扩展，建议以"新增模板"而非"开放自由生成"的方式演进。
3. **ASR 采用规则抽取槽位。** 轻量正则对句式变化敏感，复杂表达可能抽取不全。若实际使用中召回不足，应切换到模型抽取方案。
4. **无数据库迁移版本管理。** 当前依赖 ORM 元数据自动建表，字段变更需人工处理。生产环境建议尽早引入 Alembic。
5. **未接入限流与熔断。** 当前仅有超时与重试，缺少按用户/IP 的限流与上游熔断保护。

**后续演进建议（按优先级）：**

1. 引入 Alembic 管理数据库迁移，消除字段变更风险。
2. 为 `/api/v1/chat/*` 与 `/api/v1/nl2sql/query` 增加限流，防止资源滥用。
3. 在预发环境建立 `DIFY_MODE=live` 的定期集成验证，把上方限制 1 的缺口补上。
4. 将 ASR 槽位抽取从规则升级为模型抽取，提升自然表达的召回。
5. 基于 `audit_log` 建立运营看板，把审计数据转化为可用洞察。

## 附录 A：部署速查

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 准备配置
cp .env.example .env

# 3. 初始化种子数据（幂等）
python scripts/seed.py

# 4. 启动服务（推荐用一键启动器，默认 8010 / 8020）
python scripts/launcher.py --headless --no-browser
#   或手工起：uvicorn app.main:app --host 0.0.0.0 --port 8010

# 5. 运行测试
pytest -q --cov=app                       # 单元/集成测试
python scripts/smoke_test.py              # 冒烟测试（--base 默认自动发现端口）

# 6. MySQL + Docker 部署
pip install -r requirements-mysql.txt
docker compose up -d --build
```

## 附录 B：核心参数速查

| 参数 | 取值 | 位置 |
| --- | --- | --- |
| 接口总数 | 91 个操作 / 82 条路径（10 个功能组） | `/openapi.json` |
| 数据表总数 | 19 张（18 业务 + 1 系统账号） | `app/models.py` |
| Dify 应用数 | 7 个（4 对话 + 3 工作流，学员助手为 Chatflow） | `app/services/dify.py` |
| 模型接入 | 对话模型走云端大模型；知识库嵌入走智谱 `embedding-3`（2048 维）；语音识别走 mock | 编排平台模型配置（无本地模型依赖） |
| 受控 SQL 模板数 | 8 个 | `app/services/nl2sql.py` |
| 内部工具数 | 26 个（只读 12 / 写入 14） | `app/services/agent_tools.py` |
| 角色数 | 5 个（visitor / student / employee / manager / admin） | `app/api/deps.py` |
| 错误码数 | 8 个（含成功码 0） | `app/core.py` |
| 报告口径数 | 5 类（customer_ops / daily_digest / weekly_digest / mental_weekly / complaint_weekly） | `app/services/reports.py` |
| 报告导出格式 | 4 种（xlsx / pdf / print / md） | `app/services/reports.py` |
| 待办类别数 | 5 类（approval / ticket / followup / screening_review / mental_alert） | `app/services/todo.py` |
| 单元测试 | 503 条，覆盖率 93% | `tests/` |
| 冒烟测试 | 149 条，全部通过 | `scripts/smoke_test.py` |
| 前端 E2E | 37 条，全部通过 | `scripts/frontend_e2e.mjs` |
| 环境变量 | 34 项 | `app/config.py` |
| 令牌有效期 | 120 分钟 | `ACCESS_TOKEN_EXPIRE_MINUTES` |
| 内部工具签名时效 | 300 秒 | `app/core.py` |
| 受控查询行数上限 | 50 行 | `NL2SQL_MAX_ROWS` |
