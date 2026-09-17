# 研究快照：留学机构 AI 智能助手系统技术实现

> Phase 2 产出。研究目标：校验本方案所选集成方式与工程实践是否符合上游产品（Dify）与框架（SQLAlchemy / FastAPI）的现行约定。

## 1. Dify 服务端 API 约定（已核实）

| 事实项 | 结论 | 对本方案的影响 |
|---|---|---|
| 鉴权方式 | 所有 Service API 使用 HTTP Bearer：`Authorization: Bearer {API_KEY}` | `DifyClient` 统一以 Bearer 头调用，Key 只放服务端 |
| Key 粒度 | **按应用（per-app）**，每个应用一把 Key，互不通用，格式以 `app-` 开头 | 采用 `DIFY_APP_KEYS` 字典存储 7 个应用各自的 Key |
| 对话型端点 | `POST /v1/chat-messages`（Chatbot / Chatflow / Agent 共用） | `customer_service` / `student_helper` / `enterprise_assistant` / `mental_care` 走此端点 |
| 工作流端点 | `POST /v1/workflows/run`（传命名 `inputs`，非 `query`） | `router` / `screener` / `reporter` 走此端点 |
| 响应模式 | `response_mode: blocking` 或 `streaming`（SSE 逐条 `data:` 事件，以 `message_end` 结束） | 阻塞式用于 `/chat/message`，流式用于 `/chat/stream` |
| 终态用户隔离 | 请求体 `user` 字段用于标识终端用户、隔离会话与用量统计，**必填** | 以 `user_prefix` 前缀拼业务用户 ID 传入 |
| 会话隔离 | API 创建的会话与 WebApp 界面会话**不共享** | 会话状态由本系统自管，不依赖 Dify WebApp |
| 常见错误码 | 400 入参不符 / 401 Key 错 / 404 应用未发布 / 429 限流 / 500 服务端错误 | 对应 `UpstreamError`(50200) + 降级返回 mock |

## 2. SQLite → MySQL 迁移的工程实践（已核实）

| 事实项 | 结论 | 对本方案的影响 |
|---|---|---|
| 迁移成本 | 使用 SQLAlchemy ORM 时迁移成本极低，仅改数据库 URL | 全部数据访问走 ORM，不写裸 SQL（NL2SQL 除外，见下） |
| BigInteger 主键 | SQLite 的 AUTOINCREMENT **只支持 INTEGER 主键**，BigInteger 自增失效 | 采用 `BigInteger().with_variant(Integer, "sqlite")` 双栈主键类型 |
| 驱动 | MySQL 用 `pymysql` | `requirements-mysql.txt` 单独声明 PyMySQL + cryptography |
| 连接池 | MySQL 需配置 `pool_size` / `max_overflow` / `pool_pre_ping` | `_build_engine()` 中按方言分支配置 |
| 原生 SQL 风险 | 分页语法、占位符（`?` vs `%s`）、日期函数差异大 | NL2SQL 采取"模板 + 白名单"策略，禁止自由生成 SQL |

## 3. 结论

研究结论与本项目既定实现一致，无需调整技术路线。全部外部事实均支持：**Python + FastAPI + Dify(Agent) + MySQL/SQLite** 是可行且低迁移成本的组合。
