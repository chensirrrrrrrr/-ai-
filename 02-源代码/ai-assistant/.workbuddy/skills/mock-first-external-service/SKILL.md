---
name: mock-first-external-service
description: 接入外部 SaaS（Dify / LLM 平台 / 第三方 API）时，用「本地 mock 优先 + 显式降级 + 切 live 前接线预检」的做法，避免「以为接上了其实一直在跑 mock」。包含 mock/live 双模配置形状、降级可观测性、preflight 脚本、内网目标绕开系统代理、复杂 env 字段（List/Dict）导致进程起不来的坑、不联网的双模测试写法，以及**反向调用**（SaaS 回调你后端）时必需的 HMAC 签名与平台能力不匹配的坑与三条出路。当需要接入会调外部 API 的服务、外部服务还没就绪、或排查「配了 Key 但回复不对」时使用。
agent_created: true
---

# 外部服务的 mock 优先双模接入

**核心问题**：外部 SaaS 没就绪时开发不能停；于是大家加一层「连不上就降级」。
这个兜底有个**致命副作用**——Key 写错 / 地址写错 / 应用没建，用户拿到的是 mock 回答，
却以为「已经接上了」，**从答复里根本看不出来**。等上线才发现，代价极大。

所以整套做法围绕一句话：**降级必须显式、可观测、且切 live 之前能被证明。**

---

## 一、三条铁律

### 1. mock 是一等公民，不是「降级状态」

mock 必须有一份**完整的本地实现**——真查数据库、真写数据库、真按业务规则编排，
而不是 `return {"answer": "这是模拟回答"}`。判断标准：

> 把 mock 模式当正式环境跑，功能是否是**可用**的（只是"不够智能"）？
> 如果是，说明 mock 写对了。如果到处是"该功能需接入 Dify"，说明 mock 只是个占位符。

这样 mock 模式天然就是就绪的，就绪探针口径才不会自相矛盾：

```python
@property
def service_ready(self) -> bool:
    """mock 模式天生就绪（有完整本地编排层）；只有 live 模式没配齐 Key 才算没就绪。"""
    return (not self.is_live) or self.live_ready
```

### 2. 降级必须可观测

三处必须能看出来当前是什么模式：

| 位置 | 做法 |
|---|---|
| 健康检查 | `GET /health` 的响应体里带 `{"dify_mode": "mock", "asr_provider": "mock"}` |
| 就绪探针 | `/ready` 按上面的 `service_ready` 口径返回，别把 mock 判成没就绪 |
| 前端状态灯 | 左下角直接显示 `sqlite · dify=mock · asr=mock`，肉眼可见 |

前端的 E2E 断言就是「状态灯里出现了 `dify=`」——模式信息一旦丢，测试立刻红。

### 3. 切 live 之前必须过 preflight

写一个 `scripts/check_<service>.py`，**在改 `MODE=live` 之前**跑。它要能把结论摊开说清楚，
而不是笼统地报"失败"。判定分类至少覆盖：

| 结论 | 含义 |
|---|---|
| `ok` | Key 有效、应用类型对 |
| `key_invalid` | 401/403，Key 要重新复制 |
| `missing` | `.env` 里压根没配（**mock 模式下是正常状态，不算失败**） |
| `unreachable` | 连不上：服务没起 / 端口不通 / host 写错 |
| `path_wrong` | 普遍 404，多半是 `base_url` 少了版本前缀（如 `/v1`） |

---

## 二、配置形状

一个服务一个 `Settings` 字段组，配套 4 个派生属性。**别把判断逻辑散在业务代码里**：

```python
svc_mode: str = "mock"                              # mock | live
svc_base_url: str = "http://127.0.0.1/v1"
svc_app_keys: Annotated[Dict[str, str], NoDecode] = {}
svc_timeout: float = 30.0

@property
def is_svc_live(self) -> bool: ...                  # 模式判断
def svc_key(self, name: str) -> str: ...            # 按应用名取 Key
@property
def svc_missing_keys(self) -> List[str]: ...        # live 下缺哪些（mock 下恒为空）
@property
def svc_live_ready(self) -> bool: ...               # live 且 Key 齐全
```

**「配了一半」是最坑的状态**：部分应用真调、部分偷偷降级，从答复里区分不出来。
所以 `missing_keys` 要按**全部应用名**算（`[n for n in ALL if not key(n)]`），
而不是「字典非空就算齐」。

多应用名用模块级常量元组收口，并写一条断言锁住「配置里的名字」与「代码里常量的名字」不漂移：

```python
DIFY_APP_NAMES = ("router", "customer_service", ...)   # 改一边就红
```

---

## 三、⚠️ 复杂 env 字段会让进程直接起不来

**这是本项目踩过最阴的坑**：`pydantic-settings` 对 `List` / `Dict` 字段默认要求 env 值是 JSON，
且在**数据源层**就先 `json.loads`（早于字段校验器，所以 `field_validator(mode="before")` 救不了）。

```env
CORS_ORIGINS=http://127.0.0.1:8020,http://localhost:5173   # 直觉写法 → 直接崩
DIFY_APP_KEYS=router=app-x,customer_service=app-y          # 同上
```

→ `SettingsError: error parsing value for field "cors_origins"`，服务起不来。

**解法**：字段标 `Annotated[List[str], NoDecode]`（pydantic-settings ≥ 2.3）关掉那层解码，
再用校验器同时接受「逗号分隔」和「JSON」两种写法；k=v 形式的**坏片段必须报错，不能静默丢弃**
（丢掉的话运维会以为配上了）。

**最阴的是这坑只在有 `.env` 时才炸**——测试跑在代码内默认值上会全绿。
所以必须写一条**拿真实 `.env.example` 构造 Settings** 的回归测试，见
`scripts/settings_env_regression_test.py`。

---

## 四、⚠️ 内网目标必须绕开系统代理

本机常驻全局代理（`HTTP_PROXY=http://127.0.0.1:53299`）且 `NO_PROXY` 为空时，
`httpx` / `requests` 默认 `trust_env=True`，会**把发往 127.0.0.1 的请求也交给代理**——
典型症状是 502 或直接连不上，然后被降级逻辑吞掉变成 mock 回答，排查成本极高。

按目标地址自动判定，并留显式覆盖开关：

```python
_PRIVATE_HOST_RE = re.compile(r"^https?://(?:localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|"
                              r"\[::1\]|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|"
                              r"172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|host\.docker\.internal)(?::\d+)?")

def resolve_proxy_trust(url: str) -> bool:
    if settings.svc_trust_env_proxy is not None:     # .env 里显式覆盖
        return settings.svc_trust_env_proxy
    return not is_private_url(url)                   # 内网绕开，公网沿用
```

任何发往本机的独立脚本都要么 `trust_env=False`，要么先 `os.environ.pop("HTTP_PROXY"...)`。

---

## 五、preflight 脚本的设计要点

见 `scripts/preflight_external_service.py`。要点：

- **`--json` 给 CI / 启动器用**，人读的表格给运维用，两套输出别混。
- **按应用逐个探**，不要只测一个就说"通了"。Key 是每个应用一把，容易配错其中一个。
- **探针要挑「不会产生副作用、不烧 token」的请求**：
  - Chat / Chatflow / Agent 类：`GET /parameters`（一次鉴权，最省）
  - Workflow 类：没有 `/parameters`，改发一个**故意不合法**的请求体
    （如只带 `{"response_mode":"blocking"}` 缺 `user`）→ 400/422 说明「路由存在 + 鉴权通过」，
    且**不会真的跑一遍工作流**。
- **区分「没配 Key」和「配错 Key」**：前者在 mock 模式下是正常状态，标 `[--]` 不算失败；
  切 live 后必须算失败。
- **给结论配自查顺序**，别只说"失败"：
  - 全部 404 → 几乎肯定是 base_url 少版本前缀
  - 全部连不上 → 服务起没起 → 端口 → host（容器内互访别写 127.0.0.1）
  - 出现非预期状态码且当前走代理 → 提示关掉 trust_env
- **退出码分层**：`0` 全通过 / `1` 有问题 / `2` 用法或配置错误（例如还没切 live）。
- **一个 Key 都没配时不要说"可以切 live"**：脚本能证明的只是"现在没有东西可校验"。
- Key 打日志要**脱敏**：`f"{key[:8]}…{key[-4:]}"`。

---

## 六、不联网地测双模

### 6.1 用 transport 注入替掉网络

给检查函数留一个 `transport` 参数，测试时注入 `httpx.MockTransport`，
生产传 `None` 走真实网络。这样 preflight 的**每一条判定分支都能被单测覆盖**，
不依赖外部服务是否活着：

```python
def check_one(app: str, timeout: float,
              transport: Optional[httpx.BaseTransport] = None) -> dict:
    with httpx.Client(base_url=..., transport=transport, ...) as client: ...
```

配套用例要覆盖：200 → ok / 401 → key_invalid / 404 → path_wrong / 连接异常 → unreachable /
400+422 → workflow 探针通过 / 未配置 → missing。

### 6.2 mock 与 live 各跑一遍

- 默认测试集跑 `MODE=mock`，断言「不联网也能全绿」。
- 单独一个 `test_<svc>_live.py` 用**假 Key** 断言 live 下的**本地行为**（不真发请求）：
  缺 Key 时 `missing_keys` 齐不齐、`ready` 是否为 False、是否按预期降级。

### 6.3 配置类回归

`scripts/settings_env_regression_test.py` 给了一份模板：直接用仓库里真实的
`.env.example` 内容构造 `Settings`，把「运维实际会写的那种格式」全部覆盖一遍
（逗号分隔 / JSON / 空值 / 坏片段报错）。

---

## 七、收尾检查清单

- [ ] mock 模式下功能可用（真查库/真落库，不是占位符）
- [ ] `/health` 里有模式字段，前端状态灯看得到
- [ ] `/ready` 不把 mock 判成没就绪
- [ ] `scripts/check_<svc>.py` 能区分 5 种结论，`--json` 可用
- [ ] `missing_keys` 按全量应用名算，不是「字典非空」
- [ ] 复杂 env 字段标了 `NoDecode` + 宽松校验器 + 真实 `.env.example` 回归测试
- [ ] 内网目标绕开系统代理（`trust_env`）
- [ ] preflight 的每条判定分支都有 transport 注入的单测
- [ ] 启动器的 `--selfcheck` 里带上了这个 preflight

## 八、反向调用：外部 SaaS 回调你的后端

前面讲的都是「你调外部」。但真实业务里**外部也会反过来调你**——Dify 的工具/HTTP 节点
要读你的业务数据（查成绩、查工单、提交申请）。这条反向链路有个专属坑：

> **你为了防伪造上了 HMAC 签名，而 SaaS 平台根本算不出来。**

症状：后端 `/internal/tools/*` 强制 `X-Tool-Timestamp` + `X-Tool-Signature`
（`HMAC-SHA256(secret, timestamp + "." + raw_body)`，再校验时间窗 ±300s 防重放），
但 Dify 的「自定义工具」只支持 `none` / `api_key` / `bearer` 三种鉴权，
**不会对每个请求现算 HMAC**。结果就是「接口文档写得清清楚楚，Dify 侧就是配不上」。

三条出路：

| 方案 | 做法 | 代价 |
|---|---|---|
| A. 静态 Key 通道（**推荐**） | 后端为 SaaS 单独开一条 `Authorization: Bearer <TOOL_KEY>` 鉴权（**仅在缺 HMAC 头时才走**），原 HMAC 路径原样保留 | 后端 ~15 行 + 一条负向测试 |
| B. 平台侧算签名 | 用平台的 Code 节点算 HMAC，再让 HTTP 请求节点带上 | **脆**：HTTP 节点序列化出的 body 必须与签名时的**逐字节一致**，键序/空格差一点就 403 |
| C. Code 节点直接发请求 | 绕过平台的 HTTP 节点 | 多数平台默认禁 egress；且丢掉平台自带的鉴权/超时/重试 |

配套三条：

- **别因为通道受信就跳过业务鉴权**。HMAC / 静态 Key 只证明「来源可信」，
  工具自身仍要按 `min_role` 做角色校验（调用方在 body 里带 `role`）。
- **写工具必须二次确认**：第一次调用只回 `{needs_confirm: true, preview: "人话回显", token}`，
  带 `confirm=<token>` 再调一次才真正落库。
- **降级方向会反过来**，这点最容易漏：以前是「你连不上 SaaS → 降级到本地 mock」，
  现在是「SaaS 的工具节点连不上你 → 平台侧只走 fail-branch，对你就是一次空 answer」。
  原来那套 `degraded=True` 的可观测性在这个方向上是**完全看不见的**，要从平台侧另做监控。
- **平台侧配置要能自助发现**：留一个 `GET /internal/tools` 返回工具清单 + JSON Schema，
  省得每次改工具都要人去平台手抄参数。

### 8.1 把平台的「分类器 + 分支」编排接上反向工具时的七个坑

平台自带的编排画布（Dify 的 Chatflow「问题分类器 → 各分支 → 整理 → 回复」是最典型的形态）
看着比自己的 Python 路由漂亮，但真正接线时会连续踩这几个坑。**每一个的症状都是「静默降级」，
不报错、只是答案不对**，所以必须提前知道。

| # | 坑 | 症状 | 解法 |
|---|---|---|---|
| 1 | **起始变量没人填** | 分类器照跑，分支里的工具调用全 403 | 前端通常**故意不传** `inputs`（角色一律以 Token 为准）。平台应用声明的起始变量（`role` / 用户的业务 ID）**必须由你的后端按 Token 注入**，并且**覆盖**客户端传的同名值 —— 否则访客能伪造 `role=admin` |
| 2 | **分类器的 instruction 里写了输出格式** | 平台报 `Run failed: could not find json block in the output` | 分类器自己会拼一段 system prompt，要求模型回 `{"category_id":…,"category_name":…}`，解析器去文本里找 `{`。你在 `instruction` 里写「只输出 category_id」→ 模型真回个裸值 → 解析失败。**instruction 只写「怎么判断」，一个字都别提输出格式** |
| 3 | **模型没关思考模式** | 同上（思考内容把 JSON 挤掉）或答案里混进推理过程 | 所有节点显式 `thinking: false`（含分类器）；照抄旁边能跑的老节点的 `completion_params` 最稳 |
| 4 | **会话 ID 跨应用复用** | 换一类问题后回复变成一段**通用套话** | 平台的 `conversation_id` 是**按应用隔离**的。前端一般只存一个全局会话 ID，路由换到另一个应用后递过去，平台回「会话不存在」→ 被你的降级逻辑接走 → `degraded=true` 返回 mock 答案。修法见下面 8.2 |
| 5 | **工具的角色门槛比调用者高** | 某个分支的工具调用 403，答案里出现「权限不足」 | 早期为「顾问/管理层查库」写的只读工具，`min_role` 往往是 `employee`；而终端用户是更低的角色。**别去降老工具的门槛**（它可能 `student_id` 可选、能跨用户查）——另开一个「只能查自己」的同款工具（强制带本人 ID、`min_role` 对齐终端角色），并**写一条回归测试**锁住两者的分工 |
| 6 | **平台的 HTTP 节点打不通你的内网** | 节点超时 | 见第四节：容器访问宿主要用 `host.docker.internal` 对应的真实网关 IP（Docker Desktop 常是 `192.168.65.254`，`172.17.0.1` 不通）；平台的出网代理（如 `ssrf_proxy`）要放行 `localnet`；后端得真绑 `0.0.0.0` |
| 7 | **用 API 建图时把节点顶层 `type` 写成了块类型** | 画布上节点变成**150x21 的细白条、点不进去**；检查清单**误报**「必须添加直接回复节点」；但 API 跑起来完全正常 | 画布只注册了一个 ReactFlow 组件，key 是 `"custom"`，真正的种类在 `data.type`。顶层必须写 `"type": "custom"`（后端只读 `data.type`，所以已发布版本一直是好的）。详见技能 `dify-workflow-draft-node-repair` |

验收这七条的方式：**每个分支各问一句真实的业务问题**，然后盯两个字段 ——
答案里的数据是不是真的来自你的库（而不是套话），以及 `degraded` 是不是 `false`。
只看「有回复」会把这七条全放过。

### 8.2 会话 ID 跨应用复用：定向重试的写法（别当成普通错误）

先抓准平台到底回什么。Dify 1.10 实测两句话两种码：

```
404 {"code":"not_found","message":"Conversation Not Exists. ..."}
400 {"errors":{"conversation_id":"Existing conversation ID x is not a valid uuid."}}
```

**只有这两种**要把会话 ID 丢掉重开；其余 4xx/5xx/连接错误**照旧降级**。
写成「任何失败都重试一次」会让你在平台真的挂掉时白打一遍请求，还会掩盖故障。

```python
_HINTS = ("conversation not exists", "not a valid uuid")

def _conversation_missing(body) -> bool:                     # body 拿不到就回 None
    text = json.dumps(body, ensure_ascii=False).lower() if isinstance(body, dict) else str(body).lower()
    return any(h in text for h in _HINTS)

def _conversation_is_stale(resp) -> bool:
    return resp.status_code in (400, 404) and _conversation_missing(_json_or_none(resp))
```

阻塞路径最好写：拿到响应先判一次，命中就 `conversation_id=None` 递归重试（只会递归一层）。

**流式路径要绕一下**：`client.stream(...)` 得先把响应体 `await resp.aread()` 才读得到
判定用的 JSON，而且重开时要保证第一个连接被释放。用 `AsyncExitStack` 持有
`(client, resp)` 最省心：

```python
async def _open_chat_stream(self, app, payload, conversation_id):
    """返回 (stack, resp, 生效的会话 ID)；**调用方负责 stack.aclose()**。"""
    stack = AsyncExitStack()
    try:
        client = await stack.enter_async_context(self._client(self._key(app)))
        resp = await stack.enter_async_context(
            client.stream("POST", "/chat-messages", json=payload))
        if conversation_id and resp.status_code in (400, 404):
            await resp.aread()                      # ⚠️ 流式下不 aread 读不到 body
            if _conversation_missing(_json_or_none(resp)):
                await stack.aclose()                # ⚠️ 先放掉旧连接
                return await self._open_chat_stream(
                    app, {k: v for k, v in payload.items() if k != "conversation_id"}, None)
    except Exception:
        await stack.aclose()                        # ⚠️ 出错也要放，否则泄连接
        raise
    return stack, resp, conversation_id
```
调用方：`try: resp.raise_for_status() ... finally: await stack.aclose()`。

两种修法的取舍：

- **定向重试（推荐先做）**：不改对外契约（`conversation_id` 的形状多半写在文档里，
  验收期别动），线上不可见，顺带还能自愈「会话被删」「id 是垃圾值」。
  代价是换应用时多一次往返（罕见事件）。
- **按 (会话, 应用) 收口**：零浪费往返，但要引入服务端状态（DB 表或内存映射），
  内存映射在多 worker 下会各自为政 —— 好在这种缺失的降级方向是「开新会话」而不是报错。

回归测试用 `httpx.MockTransport` 模拟「第一次带会话 → 404，第二次不带 → 200」，
断言 `calls == ["旧会话", None]` 且 `degraded is False`；再补一条「500 不重试」的负向用例。

---

## 九、自带脚本

| 文件 | 用途 |
|---|---|
| `scripts/preflight_external_service.py` | 通用接线预检脚本（多应用逐个探 + 5 种结论 + `--json` + 退出码分层） |
| `scripts/settings_env_regression_test.py` | 「用真实 `.env.example` 构造 Settings」的回归测试模板 |
