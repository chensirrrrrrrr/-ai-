"""应用配置：全部通过环境变量 / .env 注入，代码里不出现任何密钥。

配置对象在进程启动时构建一次（lru_cache），因此测试需要在导入 app 之前
设置好环境变量。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Annotated, Any, Dict, List, Optional, Tuple

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# 本系统约定的 7 个 Dify 应用名。
# 与 `services/dify.py` 里的 APP_* 常量必须一一对应；
# `tests/test_config.py` 有一条断言锁住两边不漂移（改一边就红）。
DIFY_APP_NAMES: Tuple[str, ...] = (
    "router",                 # 意图路由（Workflow）
    "customer_service",       # 客服问答（Chatflow + 知识库）
    "student_helper",         # 学生事务（Agent + Tool）
    "enterprise_assistant",   # 员工取数 / 录入（Agent + Tool）
    "mental_care",            # 心理关怀（Chatflow）
    "screener",               # 材料研判（Workflow）
    "reporter",               # 经营报告（Workflow）
)

# 通知通道。`internal`（站内落库）是兜底，其余需要凭证才能真发。
# 放在模块级而不是 Settings 的类属性上：pydantic-settings 会把带注解的类属性
# 当成可配置字段，于是 .env 里写 NOTIFY_CHANNELS 就会尝试 JSON 解析并可能起不来。
NOTIFY_CHANNELS: Tuple[str, ...] = ("internal", "wecom", "sms", "webhook")


# 内网/本机地址：这类目标**不该走系统代理**（见 Settings.dify_use_proxy）
_PRIVATE_HOST_RE = re.compile(
    r"^https?://(?:"
    r"localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\]|"
    r"10\.\d+\.\d+\.\d+|"
    r"192\.168\.\d+\.\d+|"
    r"172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|"
    r"host\.docker\.internal"
    r")(?::\d+)?",
    re.IGNORECASE,
)


def is_private_url(url: str) -> bool:
    """URL 是否指向本机 / 内网（这类目标不该走系统代理）。"""
    return bool(_PRIVATE_HOST_RE.match((url or "").strip()))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 基础 ----------
    app_name: str = "留学机构 AI 智能助手系统"
    app_version: str = "1.0.0"
    api_v1_prefix: str = "/api/v1"
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = 8000

    # ---------- 跨域 ----------
    # 前后端分离部署时，前端域名必须在这里登记（debug=True 时放开为全放通）。
    #
    # 注意 Annotated[..., NoDecode]：pydantic-settings 对 List/Dict 默认要求
    # env 值是 JSON，会在**数据源层**抢先 json.loads，导致 .env 里写
    # CORS_ORIGINS=a,b 直接抛 SettingsError 让进程起不来。加 NoDecode 关掉
    # 那层解码，改由下面的校验器处理「逗号分隔 or JSON」两种写法。
    cors_origins: Annotated[List[str], NoDecode] = [
        "http://127.0.0.1:8020",
        "http://localhost:8020",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:8010",
        "http://localhost:8010",
    ]

    # ---------- 安全 ----------
    # 生产必须替换；长度 >= 32 字节，否则 HS256 会告警（RFC 7518）
    secret_key: str = "dev-only-secret-change-me-0123456789abcdef"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 120
    tool_shared_secret: str = "dev-only-tool-secret-change-me-0123456789"

    # Dify 反向调用 `/internal/tools/*` 用的**静态 Key**（`Authorization: Bearer <它>`）。
    #
    # 为什么还需要它，而不是只用 HMAC：Dify 的「自定义工具 / HTTP 请求节点」只支持
    # none / api_key / bearer 三种鉴权，**没法对每个请求现算 HMAC**（签名内容依赖
    # 原始 body 与 timestamp）。所以给 Dify 单独开一条静态 Bearer 通道，
    # 原 HMAC 路径一字未动，两条路互不影响。
    #
    # ⚠️ 静态 Key 没有防重放能力，等价于「共享密码」。因此：
    #   1. 默认**留空 = 通道关闭**，不给没配置的环境留后门；
    #   2. 生产必须在网关把 `/internal/tools*` 限制为仅 Dify 容器网段可访问
    #      （与 HMAC 通道同一条运维要求）；
    #   3. 业务侧的角色校验不受影响 —— 通道只证明「来源可信」，
    #      工具自身仍按 min_role 卡权限。
    dify_tool_key: str = ""

    # ---------- 数据库 ----------
    # 开发用 SQLite（文件即库）；生产切 MySQL：
    #   mysql+pymysql://user:pwd@host:3306/ai_assistant?charset=utf8mb4
    database_url: str = "sqlite:///./data/ai_assistant.db"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ---------- Dify ----------
    # dify_app_keys 同样标 NoDecode：Dict 字段默认也只吃 JSON，而运维在 .env 里
    # 更容易写成 `router=app-x,customer_service=app-y`。两种写法都支持，
    # 免得又踩一次「格式不对导致进程起不来」。
    dify_mode: str = "mock"          # mock | live
    dify_base_url: str = "http://127.0.0.1/v1"
    dify_app_keys: Annotated[Dict[str, str], NoDecode] = {}
    dify_timeout: float = 30.0
    dify_max_retries: int = 2
    dify_user_prefix: str = "uas"
    # None = 自动判断（内网地址绕开代理，公网地址沿用代理）；也可显式 true/false
    dify_trust_env_proxy: Optional[bool] = None

    # ---------- 语音录入 ----------
    asr_provider: str = "mock"       # mock | dify
    asr_max_bytes: int = 10 * 1024 * 1024

    # ---------- 业务开关 ----------
    nl2sql_max_rows: int = 50
    nl2sql_whitelist_enabled: bool = True

    # ---------- 材料上传与解析（M1 客户研判） ----------
    # 上传文件落盘目录。相对路径按**进程工作目录**解析（与 DATABASE_URL 同口径，
    # 即项目根）。生产接对象存储时只要把 raw_file_url 换成外链，接口契约不变。
    upload_dir: str = "data/uploads"
    upload_max_bytes: int = 8 * 1024 * 1024      # 单份材料上限
    parse_max_chars: int = 20000                 # 解析文本截断阈值（喂给研判的正文）
    screening_batch_max: int = 20                # 单次批量研判条数上限

    # ---------- 主动待办推送 / 预警触达 ----------
    # 默认 **关闭** 后台调度：pytest / 冒烟 / E2E 都在同一份数据上跑，
    # 后台线程会持续写 todo_push 把断言搞脏。要跑就设 ENABLE_SCHEDULER=true，
    # 或者直接调 `services.scheduler.run_once()`（同一条逻辑，手动触发）。
    enable_scheduler: bool = False
    scheduler_interval_seconds: int = 900        # 15 分钟扫一轮
    todo_push_window_minutes: int = 60           # 同类待办对同一人的推送冷却窗口（频控）
    alert_overdue_hours: int = 24                # 超过这么久没处理就算「高优先级」待办

    # ---------- 通知与触达通道 ----------
    # 站内（internal）永远可用，是兜底通道；
    # wecom / sms / webhook 是**真实外发**通道，需要凭证：没配就降级为站内，
    # 并把「实际生效的通道」写进 notification.delivered_via（不静默降级）。
    #
    # ⚠️ 需求 SRS 4.4.4 要求「高危预警必须在秒级触达责任人」、AC-07 要求
    #    「触达时延 ≤ 5 秒」。本项目当前**没有企业微信应用凭证、也没有短信服务商账号**，
    #    所以这两条只能验证到「站内可达 + 通道可插拔」，真实外发时延要等甲方提供
    #    凭证后配好 NOTIFY_WECOM_WEBHOOK / NOTIFY_SMS_PROVIDER 再验。
    notify_default_channel: str = "internal"
    notify_wecom_webhook: str = ""       # 企业微信机器人 Webhook 地址
    notify_webhook_url: str = ""         # 通用回调地址（内部工单/IM 系统对接）
    notify_sms_provider: str = ""        # 短信服务商标识（预留，适配器待接）
    notify_http_timeout: float = 5.0
    notify_title_max: int = 128          # 标题截断长度，防止外发通道拒收

    # ---------- 学业考务提醒（REQ-M4-04） ----------
    deadline_remind_default_hours: int = 48   # 新增节点默认「提前多久提醒」
    deadline_scan_batch: int = 200            # 单轮扫描上限

    # ---------- 增值转化（REQ-M4-07） ----------
    promotion_cooldown_days: int = 14    # 同一学生 N 天内最多推 1 次（频控）
    promotion_max_per_run: int = 20      # 单轮最多推多少条

    # ---------- 写操作二次确认（SRS 4.3.4） ----------
    pending_action_ttl_seconds: int = 300     # 确认令牌有效期，过期需重新发起

    # ---------- 限流（进程内滑动窗口，见 services/ratelimit 的说明） ----------
    rate_limit_enabled: bool = True
    rate_limit_auth_per_min: int = 30        # 登录接口：每 IP 每分钟（防爆破）
    rate_limit_chat_per_min: int = 120       # 对话接口：每账号每分钟（防刷 LLM）

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    # ------------------------------------------------------------------ #
    # 通知通道
    # ------------------------------------------------------------------ #
    def notify_channel_ready(self, channel: str) -> bool:
        """某个通道现在能不能真的发出去。

        `internal` 恒为 True（落库即达，前端「我的通知」能看到）。
        其余通道**必须配了凭证才算就绪** —— 不配就是没就绪，不做「假装发了」。
        """
        name = (channel or "").strip().lower()
        if name == "internal":
            return True
        if name == "wecom":
            return bool(self.notify_wecom_webhook.strip())
        if name == "webhook":
            return bool(self.notify_webhook_url.strip())
        if name == "sms":
            return bool(self.notify_sms_provider.strip())
        return False

    @property
    def notify_ready_channels(self) -> List[str]:
        """当前真正可用的通道清单（就绪探针 / 界面提示用）。"""
        return [c for c in NOTIFY_CHANNELS if self.notify_channel_ready(c)]

    @property
    def is_dify_live(self) -> bool:
        return self.dify_mode.lower() == "live"

    @property
    def is_mock(self) -> bool:
        """离线模式：对话由本地 mock Agent 编排，不碰 Dify。"""
        return self.dify_mode.lower() != "live"

    def dify_key(self, app_name: str) -> str:
        """按应用名取 Dify API Key。"""
        return (self.dify_app_keys.get(app_name) or "").strip()

    @property
    def dify_missing_keys(self) -> List[str]:
        """live 模式下还没配 Key 的应用名。

        mock 模式返回空列表（本来就不需要 Key）。
        「配了一半」是最坑的状态：部分应用能真调、部分偷偷降级，
        所以这里按**全量 7 个**算，而不是「字典非空就算齐」。
        """
        if not self.is_dify_live:
            return []
        return [name for name in DIFY_APP_NAMES if not self.dify_key(name)]

    @property
    def dify_live_ready(self) -> bool:
        """live 模式且 7 个应用的 Key 都齐了才算「真的接上了」。"""
        return self.is_dify_live and not self.dify_missing_keys

    @property
    def dify_ready(self) -> bool:
        """服务能否正常应答（就绪探针口径）。

        mock 模式**天生就是就绪的** —— 它有完整的本地编排层（真查库 / 真落库），
        不是「降级状态」。只有 live 模式没配齐 Key 才算没就绪。
        """
        return (not self.is_dify_live) or self.dify_live_ready

    @property
    def dify_target_is_local(self) -> bool:
        """DIFY_BASE_URL 指向本机 / 内网。"""
        return is_private_url(self.dify_base_url)

    def resolve_proxy_trust(self, url: str) -> bool:
        """给定目标 URL，判断 httpx 的 `trust_env` 该取什么值。

        为什么需要它：httpx 默认 `trust_env=True`，会把发往 127.0.0.1 的请求
        也交给系统代理处理。典型症状是返回 502 或直接连不上（本机就跑着
        `HTTP_PROXY=http://127.0.0.1:53299` 且 `NO_PROXY` 为空，实测踩到），
        然后被降级逻辑吞掉、变成 mock 回答 —— 排查成本极高。

        规则：本机/内网目标绕开代理；公网目标（Dify Cloud 之类）照常沿用。
        需要覆盖时在 .env 里写 DIFY_TRUST_ENV_PROXY=true / false。
        """
        if self.dify_trust_env_proxy is not None:
            return self.dify_trust_env_proxy
        return not is_private_url(url)

    @property
    def dify_use_proxy(self) -> bool:
        """针对 `dify_base_url` 的代理判断（脚本 / 探针用）。"""
        return self.resolve_proxy_trust(self.dify_base_url)

    # ------------------------------------------------------------------ #
    # .env 里复杂字段的宽松写法
    #
    # cors_origins / dify_app_keys 都标了 NoDecode，所以这里拿到的是**原始字符串**：
    #   CORS_ORIGINS=http://a:1,http://b:2   → ['http://a:1', 'http://b:2']
    #   CORS_ORIGINS=["http://a:1"]          → ['http://a:1']
    #   CORS_ORIGINS=                        → []
    #
    #   DIFY_APP_KEYS={"router":"app-x"}                  → {'router': 'app-x'}
    #   DIFY_APP_KEYS=router=app-x,reporter=app-y         → {'router': 'app-x', ...}
    #   DIFY_APP_KEYS=                                    → {}
    # ------------------------------------------------------------------ #
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        return [item.strip() for item in text.split(",") if item.strip()]

    @field_validator("dify_app_keys", mode="before")
    @classmethod
    def _parse_dify_app_keys(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return {}
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"DIFY_APP_KEYS 看着像 JSON 但解析失败：{exc}；"
                    '要么改写成合法的 {"router":"app-xxx"}，'
                    "要么用 k=v 形式：router=app-xxx,customer_service=app-yyy"
                ) from exc
            if not isinstance(parsed, dict):
                raise ValueError("DIFY_APP_KEYS 必须是「应用名 -> Key」的映射")
            return parsed

        # k=v 形式：坏片段直接报错，不能静默丢掉（那样运维会以为配上了）
        pairs: Dict[str, str] = {}
        for seg in text.split(","):
            seg = seg.strip()
            if not seg:
                continue
            if "=" not in seg:
                raise ValueError(
                    f"DIFY_APP_KEYS 片段 {seg!r} 不是 k=v 形式；"
                    "正确示例：router=app-xxx,customer_service=app-yyy"
                )
            name, _, key = seg.partition("=")
            pairs[name.strip()] = key.strip()
        return pairs


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
