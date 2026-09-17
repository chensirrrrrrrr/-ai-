"""`launcher.spawn_console` 的命令行拼装回归测试。

背景（真事故）：`Popen(["cmd", "/k", cmdline])` 传的是 **list**，Python 会用
`subprocess.list2cmdline` 把内层引号转义成 `\\"`；而 **cmd.exe 不认反斜杠转义**，
于是它去找一个字面名叫 `\\"C:\\...\\python.exe\\"` 的文件，报
「不是内部或外部命令，也不是可运行的程序或批处理文件」——
表现就是双击 `start.bat` 后弹出的控制台窗口一闪报错、服务起不来。

这里既断言拼装结果，也**真的把拼出来的命令行交给 cmd 跑一遍**，
免得以后有人又把 `Popen` 的参数换回 list。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import launcher  # noqa: E402

WIN = os.name == "nt"
PY = sys.executable
BACKEND_ARGS = ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8010"]
FRONTEND_ARGS = ["scripts/serve_frontend.py", "--host", "127.0.0.1", "--port", "8020",
                 "--api-base", "http://127.0.0.1:8010"]


# --------------------------------------------------------------------------- #
# 拼装结果
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title,args", [
    (launcher.BACKEND_TITLE, BACKEND_ARGS),
    (launcher.FRONTEND_TITLE, FRONTEND_ARGS),
])
def test_no_backslash_escaped_quotes(title, args):
    """回归标记：绝不能再出现 \\" —— 那是 cmd 解析不了的写法。"""
    assert '\\"' not in launcher.console_cmdline(title, PY, args)


@pytest.mark.parametrize("title,args", [
    (launcher.BACKEND_TITLE, BACKEND_ARGS),
    (launcher.FRONTEND_TITLE, FRONTEND_ARGS),
])
def test_shape(title, args):
    cmd = launcher.console_cmdline(title, PY, args)
    assert cmd.startswith(f'cmd /k "title {title} & "')
    assert cmd.endswith('"')
    assert f'"{PY}"' in cmd                      # 解释器路径永远带引号
    assert args[-1] in cmd                       # 参数一个不丢


@pytest.mark.parametrize("title,args", [
    (launcher.BACKEND_TITLE, BACKEND_ARGS),
    (launcher.FRONTEND_TITLE, FRONTEND_ARGS),
])
def test_title_failure_cannot_block_the_service(title, args):
    """`title` 只是装饰：用 `&`（无条件）而非 `&&`，它挂了服务也得照样起。"""
    cmd = launcher.console_cmdline(title, PY, args)
    assert " && " not in cmd


@pytest.mark.skipif(not WIN, reason="cmd.exe 只在 Windows 上有")
def test_service_still_starts_after_a_failed_prefix(tmp_path):
    """证明选 `&`（而非 `&&`）的语义：前一句失败，后面照样执行。

    用一条必然失败的假命令顶替 `title`，后面再接真实 python —— 若用了 `&&`，
    STUB-OK 不会出现。
    """
    stub = _stub(tmp_path)
    raw = launcher.console_cmdline("T", PY, [str(stub)])
    broken = raw.replace("title T &", "this-command-does-not-exist-xyz &")
    p = subprocess.run(broken.replace("cmd /k ", "cmd /c ", 1), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert "STUB-OK" in p.stdout


def test_executable_path_always_quoted_even_with_spaces():
    cmd = launcher.console_cmdline("T", r"C:\Program Files\Py\python.exe", [])
    assert '"C:\\Program Files\\Py\\python.exe"' in cmd


def test_arg_with_spaces_is_quoted():
    cmd = launcher.console_cmdline("T", PY, ["--api-base", "http://a b"])
    assert '"http://a b"' in cmd


def test_arg_with_cmd_metachar_is_quoted():
    """`&` 不引起来会被 cmd 当成命令分隔符。"""
    cmd = launcher.console_cmdline("T", PY, ["--msg", "a&b"])
    assert '"a&b"' in cmd


def test_plain_arg_stays_bare():
    cmd = launcher.console_cmdline("T", PY, ["--port", "8020"])
    assert "--port 8020" in cmd


# --------------------------------------------------------------------------- #
# 真跑一遍（cmd.exe 只在 Windows 上有）
# --------------------------------------------------------------------------- #
def _stub(tmp_path: Path) -> Path:
    p = tmp_path / "stub.py"
    p.write_text("import sys\nprint('STUB-OK', sys.argv[1:])\n", encoding="utf-8")
    return p


@pytest.mark.skipif(not WIN, reason="cmd.exe 只在 Windows 上有")
def test_cmd_actually_executes_the_built_cmdline(tmp_path):
    """核心断言：把拼好的命令行（/k 换 /c 以免挂住）交给 cmd，必须真跑起来。"""
    stub = _stub(tmp_path)
    raw = launcher.console_cmdline("Probe Title", PY, [str(stub), "--port", "1"])
    p = subprocess.run(raw.replace("cmd /k ", "cmd /c ", 1), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert p.returncode == 0, f"rc={p.returncode} err={p.stderr}"
    assert "STUB-OK" in p.stdout
    assert "--port" in p.stdout


@pytest.mark.skipif(not WIN, reason="cmd.exe 只在 Windows 上有")
def test_old_list_form_really_is_broken(tmp_path):
    """反证：老写法（传 list）确实坏 —— 说明上面的修法不是可有可无的。"""
    stub = _stub(tmp_path)
    cmdline = f'title T && "{PY}" {stub}'
    raw = subprocess.list2cmdline(["cmd", "/k", cmdline])
    assert '\\"' in raw, "list2cmdline 应该产生被转义的引号"
    p = subprocess.run(raw.replace("cmd /k ", "cmd /c ", 1), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert p.returncode != 0
    assert "STUB-OK" not in p.stdout
