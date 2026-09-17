"""默认端口契约：各脚本的默认端口必须以 `scripts/launcher.py` 为**唯一权威源**。

背景（真事故）：`smoke_test.py` 把 `--base` 默认值硬编码成
`http://127.0.0.1:8000`，而 `launcher.py` 的 `DEFAULT_BACKEND_PORT` 是 **8010**。
后果：服务由 launcher 起在 8010 时，手工跑 `smoke_test.py` 不加 `--base`
会**全部打空**，149 条用例集体失败 —— 现象看起来像「服务坏了」，
实际只是默认值漂移。

这类「同一个事实在两处各写一遍」的缺陷，靠代码评审记不住，得靠测试钉住。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts import launcher, smoke_test

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# smoke_test.py 的 --base 默认值
# --------------------------------------------------------------------------- #
def test_smoke_base_falls_back_to_launcher_default(tmp_path, monkeypatch):
    """没有运行态记录时，默认值应等于 launcher 的默认后端端口。"""
    monkeypatch.setattr(smoke_test, "PID_FILE", tmp_path / "pids.json")
    assert smoke_test.default_base() == \
        f"http://{launcher.HOST}:{launcher.DEFAULT_BACKEND_PORT}"


def test_smoke_base_follows_running_port(tmp_path, monkeypatch):
    """有运行态记录时，应跟随 launcher 实际启动的端口（含自定义端口）。"""
    pid_file = tmp_path / "pids.json"
    pid_file.write_text(json.dumps({"backend_port": 9011}), encoding="utf-8")
    monkeypatch.setattr(smoke_test, "PID_FILE", pid_file)
    assert smoke_test.default_base() == f"http://{launcher.HOST}:9011"


def test_smoke_base_survives_broken_pid_file(tmp_path, monkeypatch):
    """pids.json 损坏或字段缺失时不能抛异常，要安静兜底。"""
    pid_file = tmp_path / "pids.json"
    pid_file.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(smoke_test, "PID_FILE", pid_file)
    assert smoke_test.default_base() == \
        f"http://{launcher.HOST}:{launcher.DEFAULT_BACKEND_PORT}"


def test_smoke_base_survives_missing_port_key(tmp_path, monkeypatch):
    """pids.json 合法但没有 backend_port 字段，同样要兜底而不是 KeyError。"""
    pid_file = tmp_path / "pids.json"
    pid_file.write_text(json.dumps({"frontend": 616}), encoding="utf-8")
    monkeypatch.setattr(smoke_test, "PID_FILE", pid_file)
    assert smoke_test.default_base() == \
        f"http://{launcher.HOST}:{launcher.DEFAULT_BACKEND_PORT}"


def test_smoke_has_no_hardcoded_default_port():
    """回归标记：源码里不应再出现硬编码端口的 --base 默认值。"""
    src = (ROOT / "scripts" / "smoke_test.py").read_text(encoding="utf-8")
    assert '"--base", default="http://127.0.0.1:8000"' not in src
    assert "default=default_base()" in src


# --------------------------------------------------------------------------- #
# 其他脚本的默认端口也要与 launcher 对齐
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rel,pattern,const", [
    ("scripts/serve_frontend.py", r"default=(\d+)", "DEFAULT_FRONTEND_PORT"),
    # FRONT 的 URL 结尾带斜杠（8020/），BACK 不带 —— 用 /? 兼容两种写法
    ("scripts/frontend_e2e.mjs", r"FRONT_URL \|\| 'http://127\.0\.0\.1:(\d+)/?'",
     "DEFAULT_FRONTEND_PORT"),
    ("scripts/frontend_e2e.mjs", r"BACK_URL \|\| 'http://127\.0\.0\.1:(\d+)/?'",
     "DEFAULT_BACKEND_PORT"),
])
def test_defaults_match_launcher(rel, pattern, const):
    """前端静态服务与浏览器 E2E 的默认端口，须与 launcher 常量一致。"""
    src = (ROOT / rel).read_text(encoding="utf-8")
    ports = re.findall(pattern, src)
    assert ports, f"{rel} 里没扫到端口默认值，检查正则或文件结构是否变了"
    assert str(getattr(launcher, const)) in ports, \
        f"{rel} 的默认端口 {ports} 与 launcher.{const} 不一致"
