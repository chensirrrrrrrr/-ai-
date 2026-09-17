# -*- coding: utf-8 -*-
"""配置类回归测试模板：**用仓库里真实的 .env.example 构造 Settings**。

为什么必须这么写
----------------
`pydantic-settings` 对 List / Dict 字段默认要求 env 值是 JSON，且在**数据源层**
就先 `json.loads`（早于字段校验器，所以 `field_validator(mode="before")` 救不了）。
于是 .env 里写 `CORS_ORIGINS=a,b` 会让**进程直接起不来**。

最阴的是：这个坑**只在存在 .env 时才炸**。测试若只跑代码内默认值，会全绿。

所以正确姿势是：把真实 .env.example 的每一行塞进环境变量，再造 Settings。
新增复杂字段（List/Dict/嵌套模型）后，必须在这里补一条用例。

用法
----
拷到项目 `tests/` 下，把 `from app.config import Settings` 换成你的配置类，
把 `ENV_EXAMPLE` 指向你的 `.env.example`。
"""
from __future__ import annotations

import pathlib
import re

import pytest

# ---- 改成你的项目 ----
from app.config import Settings                      # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
# ----------------------

# 造 Settings 前必须清掉的环境变量前缀（避免本机真实 env 干扰）
CLEAR_PREFIXES = ("CORS_", "DIFY_", "ASR_", "DB_", "DATABASE_", "SECRET_", "JWT_",
                  "NL2SQL_", "TOOL_", "DEBUG", "HOST", "PORT")


def parse_env_example(path: pathlib.Path) -> dict[str, str]:
    """极简 .env 解析：够用即可（key=value，# 注释，去引号）。"""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


@pytest.fixture()
def clean_env(monkeypatch):
    """清掉所有可能干扰的环境变量，并禁用 .env 文件读取。"""
    import os
    for name in list(os.environ):
        if name.upper().startswith(CLEAR_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture()
def env_from_example(clean_env, tmp_path, monkeypatch):
    """把 .env.example 的每一行变成真实环境变量，并返回这个字典。"""
    values = parse_env_example(ENV_EXAMPLE)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    # 关键：把 env_file 指到一个不存在的路径，确保断言的是「环境变量」而不是磁盘 .env
    monkeypatch.chdir(tmp_path)
    return values


def test_env_example_exists_and_parses():
    assert ENV_EXAMPLE.exists(), f"缺少 {ENV_EXAMPLE}"
    values = parse_env_example(ENV_EXAMPLE)
    assert values, ".env.example 解析出 0 个键，解析逻辑或文件有问题"


def test_settings_accepts_real_env_example(env_from_example):
    """核心用例：真实 .env.example 必须能成功构造 Settings。

    这条一旦红，说明有人在 .env 里写了复杂字段（List/Dict）却没标 NoDecode，
    运维照着 .env.example 抄就会起不来服务。
    """
    settings = Settings()                       # 不抛异常即通过
    assert settings.app_name


@pytest.mark.parametrize("raw, expected", [
    ("http://a:1,http://b:2", ["http://a:1", "http://b:2"]),      # 逗号分隔（直觉写法）
    ('["http://a:1"]', ["http://a:1"]),                           # JSON 数组
    ("", []),                                                     # 空值
    ("  http://a:1 , http://b:2  ", ["http://a:1", "http://b:2"]),  # 带空格
])
def test_list_field_lenient(clean_env, raw, expected):
    """List 型字段要同时吃「逗号分隔」和「JSON」，别只支持一种。"""
    clean_env.setenv("CORS_ORIGINS", raw)
    assert Settings().cors_origins == expected


@pytest.mark.parametrize("raw, expected", [
    ('{"router":"app-x"}', {"router": "app-x"}),                        # JSON
    ("router=app-x,reporter=app-y", {"router": "app-x", "reporter": "app-y"}),  # k=v
    ("", {}),
])
def test_dict_field_lenient(clean_env, raw, expected):
    """Dict 型字段同理，两种写法都要支持。"""
    clean_env.setenv("DIFY_APP_KEYS", raw)
    assert Settings().dify_app_keys == expected


def test_dict_field_bad_segment_raises(clean_env):
    """k=v 形式里的坏片段**必须报错**，不能静默丢弃 —— 丢了运维会以为配上了。"""
    clean_env.setenv("DIFY_APP_KEYS", "router=app-x,oops")
    with pytest.raises(Exception) as exc:      # noqa: PT011 - pydantic 包了一层
        Settings()
    assert "k=v" in str(exc.value) or "DIFY_APP_KEYS" in str(exc.value)


def test_app_names_constant_matches_service_module():
    """配置里的应用名常量与业务代码里的常量必须一致（改一边就红）。"""
    from app import config as cfg
    from app.services import dify                      # noqa: F401
    src = (ROOT / "app" / "services" / "dify.py").read_text(encoding="utf-8")
    for name in cfg.DIFY_APP_NAMES:
        assert re.search(rf'"{name}"', src), f"services/dify.py 里找不到应用名 {name}"
