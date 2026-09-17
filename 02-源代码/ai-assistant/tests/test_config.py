"""配置解析回归测试。

背景（真实踩过的坑）：
pydantic-settings 对 List / Dict 这类「复杂字段」默认要求 env 值是 JSON，
而且是在**数据源层**先 `json.loads`，早于字段校验器。于是 .env 里照直觉写

    CORS_ORIGINS=http://127.0.0.1:8020,http://localhost:5173

会让 Settings() 直接抛 SettingsError，整个进程起不来（uvicorn 一启动就崩）。
之前测试全绿是因为仓库里没有 .env —— 测试只走代码内默认值，把这条路径漏了。

修法：给 cors_origins 标 Annotated[..., NoDecode]，关掉数据源层解码，
改由字段校验器同时接受「逗号分隔」和「JSON 数组」。

这组测试锁住三件事：
1. .env.example 本身必须能被解析（launcher 会拿它生成 .env，它炸了项目就跑不起来）；
2. .env.example 里不许出现拼错的键名（extra="ignore" 会把错字静默吞掉）；
3. cors_origins 的三种写法都要能用。
"""
from __future__ import annotations

import pathlib

import pytest

from app.config import DIFY_APP_NAMES, Settings

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"


def _active_env_keys(path: pathlib.Path) -> list[str]:
    """取出 .env 文件里真正生效的键（跳过空行与注释行）。"""
    keys: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.append(line.split("=", 1)[0].strip())
    return keys


@pytest.fixture(autouse=True)
def _clear_relevant_env(monkeypatch):
    """避免开发机真实环境变量盖过测试里构造的值。"""
    for name in ("CORS_ORIGINS", "DIFY_APP_KEYS", "DIFY_MODE"):
        monkeypatch.delenv(name, raising=False)


def test_env_example_exists() -> None:
    assert ENV_EXAMPLE.exists(), "launcher 要靠 .env.example 生成 .env"


def test_env_example_is_parseable() -> None:
    """核心回归：拿真实的 .env.example 去构造 Settings，不能抛异常。"""
    settings = Settings(_env_file=str(ENV_EXAMPLE))

    assert isinstance(settings.cors_origins, list)
    assert settings.cors_origins, ".env.example 里配了 CORS_ORIGINS，应被解析出来"
    assert all(origin.startswith("http") for origin in settings.cors_origins), \
        f"CORS_ORIGINS 解析结果可疑：{settings.cors_origins}"


def test_env_example_keys_are_known_fields() -> None:
    """键名打错会被 extra='ignore' 静默吞掉，这里主动抓出来。"""
    known = {name.upper() for name in Settings.model_fields}
    unknown = [k for k in _active_env_keys(ENV_EXAMPLE) if k.upper() not in known]
    assert not unknown, f".env.example 里出现未知键名（可能拼错）：{unknown}"


def test_env_file_with_csv_and_json(tmp_path: pathlib.Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "CORS_ORIGINS=http://127.0.0.1:8020,http://localhost:5173\n"
        'DIFY_APP_KEYS={"router":"app-x","screener":"app-y"}\n',
        encoding="utf-8",
    )

    settings = Settings(_env_file=str(env))

    assert settings.cors_origins == [
        "http://127.0.0.1:8020",
        "http://localhost:5173",
    ]
    assert settings.dify_app_keys == {"router": "app-x", "screener": "app-y"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://a:1, http://b:2 ,http://c:3", ["http://a:1", "http://b:2", "http://c:3"]),
        ('["http://x:1","http://y:2"]', ["http://x:1", "http://y:2"]),
        ("http://only:1", ["http://only:1"]),
        ("   ", []),
        ("", []),
        (",,", []),
    ],
)
def test_cors_origins_accepts_multiple_writings(raw: str, expected: list[str]) -> None:
    assert Settings(cors_origins=raw).cors_origins == expected


def test_cors_origins_passes_through_list() -> None:
    given = ["http://a:1", "http://b:2"]
    assert Settings(cors_origins=given).cors_origins == given


def test_cors_origins_default_is_usable_without_env() -> None:
    """没有 .env 时也要有合理的默认白名单。"""
    settings = Settings(_env_file=None)
    assert "http://127.0.0.1:8020" in settings.cors_origins
    assert settings.is_sqlite is True


# --------------------------------------------------------------------------- #
# DIFY_APP_KEYS：同样别再让格式问题把进程搞崩
# --------------------------------------------------------------------------- #
def test_dify_app_keys_accepts_kv_form() -> None:
    """k=v 形式是运维最容易写对的，必须支持（Dict 默认只吃 JSON）。"""
    settings = Settings(dify_app_keys="router=app-x, customer_service=app-y")
    assert settings.dify_app_keys == {"router": "app-x", "customer_service": "app-y"}


def test_dify_app_keys_accepts_json_form() -> None:
    settings = Settings(dify_app_keys='{"router":"app-x","reporter":"app-y"}')
    assert settings.dify_app_keys == {"router": "app-x", "reporter": "app-y"}


def test_dify_app_keys_empty_is_empty_dict() -> None:
    assert Settings(dify_app_keys="").dify_app_keys == {}
    assert Settings(dify_app_keys="  ").dify_app_keys == {}


def test_dify_app_keys_bad_segment_raises() -> None:
    """坏片段必须报错：静默丢掉会让运维以为配上了，实际在跑 mock。"""
    with pytest.raises(Exception) as exc:
        Settings(dify_app_keys="router=app-x,customer_serviceapp-y")
    assert "k=v" in str(exc.value)


def test_dify_app_keys_broken_json_raises() -> None:
    with pytest.raises(Exception):
        Settings(dify_app_keys='{"router":"app-x"')


def test_dify_app_names_match_services_constants() -> None:
    """两份清单不能漂移：config.DIFY_APP_NAMES 与 services/dify.py 的 APP_*。"""
    from app.services import dify as dify_module

    declared = {
        dify_module.APP_ROUTER,
        dify_module.APP_CUSTOMER_SERVICE,
        dify_module.APP_STUDENT_HELPER,
        dify_module.APP_ENTERPRISE,
        dify_module.APP_MENTAL_CARE,
        dify_module.APP_SCREENER,
        dify_module.APP_REPORTER,
    }
    assert declared == set(DIFY_APP_NAMES)


def test_dify_missing_keys_reports_every_gap() -> None:
    """配一半也算没配齐 —— 「部分应用静默降级」是最坑的状态。"""
    settings = Settings(dify_mode="live", dify_app_keys="router=app-x")
    assert "router" not in settings.dify_missing_keys
    assert "customer_service" in settings.dify_missing_keys
    assert len(settings.dify_missing_keys) == len(DIFY_APP_NAMES) - 1
    assert settings.dify_live_ready is False


def test_dify_missing_keys_is_empty_in_mock() -> None:
    """mock 模式本来就不需要 Key，不该报缺。"""
    settings = Settings(dify_mode="mock")
    assert settings.dify_missing_keys == []
    assert settings.dify_live_ready is False


def test_dify_live_ready_when_all_keys_present() -> None:
    raw = ",".join(f"{name}=app-{i}" for i, name in enumerate(DIFY_APP_NAMES))
    settings = Settings(dify_mode="live", dify_app_keys=raw)
    assert settings.dify_missing_keys == []
    assert settings.dify_live_ready is True
    assert settings.dify_key("router") == "app-0"
    assert settings.dify_key("nonexistent") == ""


@pytest.mark.parametrize(
    ("mode", "keys", "live_ready", "ready"),
    [
        # mock 是完整可用的形态（有本地编排层），就绪探针必须为 True
        ("mock", "", False, True),
        ("live", "", False, False),
        ("live", "router=app-x", False, False),
        ("live", "ALL", True, True),
    ],
)
def test_dify_readiness_semantics(mode: str, keys: str, live_ready: bool, ready: bool) -> None:
    """就绪探针的口径：只有「live 却没配齐 Key」才算没就绪。"""
    raw = ",".join(f"{n}=app-{i}" for i, n in enumerate(DIFY_APP_NAMES)) if keys == "ALL" else keys
    settings = Settings(dify_mode=mode, dify_app_keys=raw)
    assert settings.dify_live_ready is live_ready
    assert settings.dify_ready is ready


# --------------------------------------------------------------------------- #
# 系统代理：本机/内网 Dify 必须绕开 HTTP_PROXY
#
# 实测坑：本机 HTTP_PROXY=http://127.0.0.1:53299 且 NO_PROXY 为空，
# httpx 默认 trust_env=True，会把发往 127.0.0.1 的请求也交给代理 →
# 返回 502 → 被降级逻辑吞掉变成 mock 回答，从界面上完全看不出来。
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("base_url", "is_local", "use_proxy"),
    [
        ("http://127.0.0.1/v1", True, False),
        ("http://127.0.0.1:5001/v1", True, False),
        ("http://localhost:3001/v1", True, False),
        ("http://host.docker.internal/v1", True, False),
        ("http://0.0.0.0:5001/v1", True, False),
        ("http://10.0.0.5/v1", True, False),
        ("http://192.168.1.20/v1", True, False),
        ("http://172.16.3.4/v1", True, False),
        ("http://172.31.255.1/v1", True, False),
        ("http://172.32.3.4/v1", False, True),    # 172.32 已不在私网段
        ("http://8.8.8.8/v1", False, True),
        ("https://api.dify.ai/v1", False, True),
        ("https://dify.example.com/v1", False, True),
    ],
)
def test_dify_proxy_auto_detection(base_url: str, is_local: bool, use_proxy: bool) -> None:
    settings = Settings(_env_file=None, dify_base_url=base_url)
    assert settings.dify_target_is_local is is_local
    assert settings.dify_use_proxy is use_proxy


def test_dify_proxy_can_be_overridden() -> None:
    """自动判断不合意时可以显式覆盖。"""
    forced_on = Settings(_env_file=None, dify_base_url="http://127.0.0.1/v1",
                         dify_trust_env_proxy=True)
    assert forced_on.dify_use_proxy is True

    forced_off = Settings(_env_file=None, dify_base_url="https://api.dify.ai/v1",
                          dify_trust_env_proxy=False)
    assert forced_off.dify_use_proxy is False

