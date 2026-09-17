"""一键启动 / 停止 留学机构 AI 智能助手系统。

一个脚本管完「环境检查 → 依赖检查 → 建库灌种子 → 选端口 → 起前后端 → 探活 → 开浏览器」。

用法（一般由 start.bat 调用，也可以直接用 python 跑）：

    python scripts/launcher.py                 # 启动（默认后端 8010 / 前端 8020）
    python scripts/launcher.py --stop          # 停止
    python scripts/launcher.py --status        # 看状态
    python scripts/launcher.py --selfcheck     # 起服 → 体检 → 收服（单进程，适合 CI）
    python scripts/launcher.py --selfcheck --e2e   # 体检 + 真实浏览器端到端（需 Edge）
    python scripts/launcher.py --headless      # 不开新窗口，日志写 .run/logs/
    python scripts/launcher.py --smoke         # 启动后顺带跑一遍冒烟测试
    python scripts/launcher.py --reseed        # 重建演示数据
    python scripts/launcher.py --no-browser --backend-port 9000 --frontend-port 9001

设计取舍：
- 默认前后端各起一个**独立控制台窗口**，日志实时可见，窗口关掉即停服；
  同时把 PID 记到 .run/pids.json，--stop 可以连子进程一起收掉。
- 无桌面 / 无权限开窗口的环境用 --headless：进程完全脱离，输出重定向到
  .run/logs/*.log，失败时可以直接翻日志。
- 端口被占用时：如果占用者就是本项目（探针认得出来），直接复用不重启；
  否则端口 +1 继续试，最多试 20 个。
- 选定的后端端口会通过 `serve_frontend.py --api-base` 注入前端，
  所以换端口也不用改前端代码。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT = Path(__file__).resolve().parent.parent
RUN_DIR = PROJECT / ".run"
LOG_DIR = RUN_DIR / "logs"
PID_FILE = RUN_DIR / "pids.json"
DB_FILE = PROJECT / "data" / "ai_assistant.db"

DEFAULT_BACKEND_PORT = 8010
DEFAULT_FRONTEND_PORT = 8020
HOST = "127.0.0.1"

# 后端**监听**地址（注意：只影响 uvicorn 的 --host，不影响日志里给用户看的地址）。
#
# 默认只监听回环，安全。但 Docker 里的 Dify 要用「HTTP 请求节点」反向调用
# `/internal/tools/*` 时，容器访问不到宿主的 127.0.0.1 —— 必须改成 0.0.0.0
# （容器侧写 `host.docker.internal`，在 Docker Desktop 上解析为 192.168.65.254）。
#
# ⚠️ 改成 0.0.0.0 意味着同网段任何机器都能访问本服务。仅限本机联调时用，
#    调完记得 stop。生产是 nginx 反代 + 网关网段白名单，不走这条。
BIND_HOST = HOST
PORT_SCAN_LIMIT = 20
HEALTH_TIMEOUT = 90          # 秒；首次启动要建表，放宽一点
FRONTEND_TIMEOUT = 30
E2E_TIMEOUT = 300            # 秒；浏览器端到端要开 Edge 逐页跑，给足时间

BACKEND_TITLE = "AI Assistant - Backend"
FRONTEND_TITLE = "AI Assistant - Frontend"

# 起 uvicorn / 静态服务必须能用到的包
REQUIRED_MODULES = ["fastapi", "uvicorn", "sqlalchemy", "pydantic", "pydantic_settings",
                    "jwt", "httpx", "multipart"]

# Windows 派生标志
DETACHED_PROCESS = 0x00000008
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000


# --------------------------------------------------------------------------- #
# 输出小工具（不引第三方库，保证裸环境能跑）
# --------------------------------------------------------------------------- #
def say(text: str = "") -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:                      # 老控制台兜底
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def rule(char: str = "=") -> None:
    say(char * 66)


def ok(text: str) -> None:
    say(f"  [OK]   {text}")


def warn(text: str) -> None:
    say(f"  [WARN] {text}")


def bad(text: str) -> None:
    say(f"  [FAIL] {text}")


def step(text: str) -> None:
    say(f"\n>>> {text}")


# --------------------------------------------------------------------------- #
# 环境
# --------------------------------------------------------------------------- #
def find_python() -> Optional[str]:
    """挑一个能用的解释器：项目 .venv → 本机隔离环境 → PATH。"""
    candidates = [
        PROJECT / ".venv" / "Scripts" / "python.exe",           # Windows 项目级
        PROJECT / ".venv" / "bin" / "python",                   # *nix 项目级
    ]
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    if home:
        candidates.append(Path(home) / ".workbuddy" / "binaries" / "python"
                          / "envs" / "default" / "Scripts" / "python.exe")
        candidates.append(Path(home) / ".workbuddy" / "binaries" / "python"
                          / "envs" / "default" / "bin" / "python")

    for c in candidates:
        if c.exists():
            return str(c)

    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found:
            return found
    return None


def find_node() -> Optional[str]:
    """挑一个 node：本机隔离环境 → PATH。浏览器 E2E 要用它跑 puppeteer-core。"""
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    candidates = []
    if home:
        versions = Path(home) / ".workbuddy" / "binaries" / "node" / "versions"
        if versions.is_dir():
            # 版本目录名形如 22.22.2-3，倒序挑最新的
            for d in sorted(versions.iterdir(), reverse=True):
                candidates.append(d / "node.exe")
    candidates.append(Path("C:/Program Files/nodejs/node.exe"))
    for c in candidates:
        if Path(c).exists():
            return str(c)
    return shutil.which("node")


def modules_missing(py: str) -> list[str]:
    code = (
        "import importlib.util as u, json\n"
        f"mods = {REQUIRED_MODULES!r}\n"
        "print(json.dumps([m for m in mods if u.find_spec(m) is None]))\n"
    )
    try:
        proc = subprocess.run([py, "-c", code], capture_output=True, text=True,
                              timeout=60, encoding="utf-8", errors="replace")
    except Exception:
        return list(REQUIRED_MODULES)
    if proc.returncode != 0:
        return list(REQUIRED_MODULES)
    try:
        return json.loads((proc.stdout or "").strip().splitlines()[-1])
    except Exception:
        return list(REQUIRED_MODULES)


def pip_install(py: str, req: Path) -> bool:
    index = os.environ.get("PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple")
    cmd = [py, "-m", "pip", "install", "-r", str(req), "-i", index]
    say(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=str(PROJECT)).returncode == 0


def ensure_dependencies(py: str) -> bool:
    step("检查 Python 依赖")
    missing = modules_missing(py)
    if not missing:
        ok("依赖齐全")
        return True

    warn(f"缺少 {len(missing)} 个包：{', '.join(missing)}")
    req = PROJECT / "requirements.txt"
    if not req.exists():
        bad(f"找不到 {req}")
        return False
    if not pip_install(py, req):
        bad("依赖安装失败（检查网络或 PIP_INDEX_URL）")
        return False

    still = modules_missing(py)
    if still:
        bad(f"安装后仍缺：{', '.join(still)}")
        return False
    ok("依赖安装完成")
    return True


def ensure_env_file() -> None:
    step("检查配置文件")
    env = PROJECT / ".env"
    example = PROJECT / ".env.example"
    if env.exists():
        ok(".env 已存在")
        return
    if example.exists():
        shutil.copyfile(example, env)
        ok(".env 已从 .env.example 生成（默认 SQLite + Dify mock）")
    else:
        warn("既没有 .env 也没有 .env.example，将全部走代码内默认值")


def ensure_database(py: str, reseed: bool) -> bool:
    step("检查数据库")
    if reseed or not DB_FILE.exists():
        reason = "指定 --reseed" if reseed else "数据库文件不存在"
        say(f"  执行种子脚本（{reason}）")
        cmd = [py, str(PROJECT / "scripts" / "seed.py")]
        if reseed:
            cmd.append("--reset")
        proc = subprocess.run(cmd, cwd=str(PROJECT), encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            bad("种子脚本执行失败")
            return False
        ok("建表 + 演示数据就绪")
    else:
        size_kb = DB_FILE.stat().st_size // 1024
        # ⚠️ 「库已存在」不等于「结构是最新的」：模型加了新表/新列之后，
        # 不清一次结构就会一路 `no such column`。这里做**只加不改不删**的同步，
        # 一行业务数据都不动 —— 比让人 `--reseed` 清库要安全得多。
        proc = subprocess.run([py, str(PROJECT / "scripts" / "sync_schema.py")],
                              cwd=str(PROJECT), encoding="utf-8", errors="replace",
                              capture_output=True)
        output = (proc.stdout or "").strip()
        changed = any(line.startswith("[sync] 缺") or line.startswith("[sync] 已建表")
                      for line in output.splitlines())
        if proc.returncode != 0 and proc.returncode != 1:
            warn("结构同步失败，继续启动（若报 no such column 请手动跑 scripts/sync_schema.py）")
            for line in (output or proc.stderr or "").splitlines()[-5:]:
                say(f"    {line}")
        elif changed:
            added = [ln.split("：", 1)[-1] for ln in output.splitlines()
                     if ln.startswith("[sync] 缺") or ln.startswith("[sync] 缺表")]
            ok(f"数据库已存在（{DB_FILE.name}，{size_kb} KB）；已同步结构："
               + "、".join(added[:6]) + ("…" if len(added) > 6 else ""))
        else:
            ok(f"数据库已存在（{DB_FILE.name}，{size_kb} KB）；结构已是最新")
    return True


# --------------------------------------------------------------------------- #
# 端口
# --------------------------------------------------------------------------- #
def pid_on_port(port: int) -> list[int]:
    """用 netstat 查监听该端口的 PID（不依赖 psutil）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=20).stdout or ""
    except Exception:
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{port}"):
                try:
                    pids.append(int(parts[4]))
                except ValueError:
                    pass
    return pids


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            # 按**实际要监听的地址**试绑：绑 0.0.0.0 时若只试 127.0.0.1，
            # 会漏判「别的网卡接口上已被占用」的情况。
            s.bind((BIND_HOST, port))
        except OSError:
            return False
    return True


def http_get(url: str, timeout: float = 2.0, token: str = "") -> Optional[str]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


def http_post_json(url: str, payload: dict, timeout: float = 5.0) -> Optional[str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


def backend_alive(port: int) -> bool:
    """探针认得出来才算「我们的后端在跑」，避免把别的服务误当自己人。"""
    body = http_get(f"http://{HOST}:{port}/api/v1/health")
    if not body:
        return False
    try:
        data = json.loads(body)
    except Exception:
        return False
    return data.get("code") == 0 and "app" in (data.get("data") or {})


def frontend_alive(port: int) -> bool:
    body = http_get(f"http://{HOST}:{port}/index.html")
    return bool(body) and "留学机构" in body


def pick_port(preferred: int, kind: str) -> tuple[int, bool]:
    """返回 (端口, 是否已在运行)。"""
    probe = backend_alive if kind == "backend" else frontend_alive
    if probe(preferred):
        return preferred, True
    if port_is_free(preferred):
        return preferred, False

    for offset in range(1, PORT_SCAN_LIMIT + 1):
        cand = preferred + offset
        if probe(cand):
            return cand, True
        if port_is_free(cand):
            warn(f"{preferred} 被占用，改用 {cand}")
            return cand, False
    raise RuntimeError(f"{preferred} 起的 {PORT_SCAN_LIMIT} 个端口都被占用了")


# --------------------------------------------------------------------------- #
# 进程
# --------------------------------------------------------------------------- #
def service_commands(py: str, bp: int, fp: int) -> tuple[list[str], list[str]]:
    """返回 (后端命令, 前端命令)。前端会把后端地址注入页面，换端口也不用改代码。

    后端按 `BIND_HOST` 监听（默认回环；要让容器里的 Dify 调进来需 `--host 0.0.0.0`），
    但**前端注入给浏览器用的地址始终是 `HOST`** —— 否则页面上会出现
    `http://0.0.0.0:8010` 这种点不开的地址。
    """
    backend = [py, "-m", "uvicorn", "app.main:app", "--host", BIND_HOST, "--port", str(bp)]
    frontend = [py, str(PROJECT / "scripts" / "serve_frontend.py"),
                "--host", HOST, "--port", str(fp),
                "--api-base", f"http://{HOST}:{bp}"]
    return backend, frontend


def _cmd_quote_arg(arg: str) -> str:
    """按 cmd.exe（不是 C runtime）的规则给单个参数加引号。

    含空格或 cmd 元字符的参数必须包双引号，否则会被 `&` `^` `(` 之类切开。
    """
    if arg == "":
        return '""'
    if any(ch in arg for ch in ' \t"&()[]{}^=;!\'+,`~'):
        return f'"{arg}"'
    return arg


def console_cmdline(title: str, py: str, args: list[str]) -> str:
    """拼出交给 `cmd /k` 的**完整原始命令行**（含 cmd 本身）。

    ⚠️ 这里返回的是「要原样交给 CreateProcess 的一整行」，调用方必须把它当
    **字符串**传进 Popen，绝不能传 list —— 传 list 时 `subprocess.list2cmdline`
    会把内层引号转义成 `\\"`，而 **cmd.exe 不认反斜杠转义**，于是它去找一个字面
    名叫 `\\"C:\\...\\python.exe\\"` 的文件，报「不是内部或外部命令」。
    这正是「双击 start.bat 后控制台窗口一闪报错」的根因。

    引号分层（实测有效）：
      cmd /k "title X & "C:\\path\\python.exe" arg1 arg2"
      └ cmd 的规则：引号数 > 2 → 掐掉最外层首尾两个引号 → 得到可正常解析的
        `title X & "C:\\path\\python.exe" arg1 arg2`

    用 `&` 而不是 `&&`：`title` 只是改窗口标题的装饰动作，万一它失败
    （比如在完全没有 console 的环境里），`&&` 会让后面的服务**根本不启动**。
    `&` 是无条件继续，装饰失败不影响正事。
    """
    inner = " ".join([f'"{py}"', *(_cmd_quote_arg(a) for a in args)])
    return f'cmd /k "title {title} & {inner}"'


def spawn_console(title: str, py: str, args: list[str]) -> Optional[int]:
    """在新控制台窗口里跑命令；窗口关掉即停服。"""
    if os.name != "nt":
        return subprocess.Popen([py, *args], cwd=str(PROJECT)).pid

    raw = console_cmdline(title, py, args)
    proc = subprocess.Popen(raw, cwd=str(PROJECT),      # 字符串=原样交给 CreateProcess
                            creationflags=CREATE_NEW_CONSOLE)
    return proc.pid


def spawn_headless(py: str, args: list[str], log_name: str) -> tuple[Optional[int], Path]:
    """完全脱离父进程，输出落到 .run/logs/<log_name>.log。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{log_name}.log"
    handle = open(log_path, "wb")
    flags = 0
    if os.name == "nt":
        flags = DETACHED_PROCESS | CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen([py, *args], cwd=str(PROJECT),
                                stdout=handle, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags)
    except Exception:
        handle.close()
        return None, log_path
    handle.close()                       # 子进程已持有自己的句柄
    return proc.pid, log_path


def kill_tree(pid: int) -> bool:
    try:
        proc = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=25)
        return proc.returncode == 0
    except Exception:
        return False


def terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def write_pid_file(data: dict) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_pid_file() -> dict:
    if not PID_FILE.exists():
        return {}
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def wait_for(name: str, probe, timeout: int = HEALTH_TIMEOUT) -> bool:
    say(f"  等待 {name} 就绪")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe():
            say("")                                  # 换行收尾
            return True
        say(".", )
        time.sleep(0.8)
    say("")
    return False


def tail_log(path: Path, lines: int = 25) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    rows = text.strip().splitlines()[-lines:]
    return "\n".join("      " + r for r in rows)


def show_logs(logs: dict[str, Path]) -> None:
    for name, path in logs.items():
        body = tail_log(path)
        if body:
            say(f"\n  ---- {name} 日志尾部（{path}） ----")
            say(body)


# --------------------------------------------------------------------------- #
# 子命令：stop / status
# --------------------------------------------------------------------------- #
def stop_by_port(port: int, kind: str) -> list[int]:
    """按端口兜底停服：**只杀探针认得的自家服务**，避免误伤占用同端口的别人。

    为什么需要这层：PID 记录可能失效（控制台窗口被关过、进程被系统回收过，
    或者上一次启动方式是 --headless 之外的形态），此时 `--stop` 会报
    「可能已经退出」，但服务其实还在跑，端口被一直占着。
    """
    probe = backend_alive if kind == "backend" else frontend_alive
    if not probe(port):
        return []
    killed: list[int] = []
    for pid in pid_on_port(port):
        if kill_tree(pid):
            killed.append(pid)
    return killed


def cmd_stop() -> int:
    step("停止服务")
    saved = read_pid_file()
    targets: list[tuple[str, int]] = []
    for key in ("backend", "frontend"):
        pid = saved.get(key)
        if isinstance(pid, int):
            targets.append((key, pid))

    if not targets:
        warn("没有 PID 记录，改为按端口找进程")

    for key, pid in targets:
        if kill_tree(pid):
            ok(f"已停止 {key}（pid={pid}）")
        else:
            warn(f"{key}（pid={pid}）的 PID 已失效，稍后按端口兜底清理")

    # 兜底：无论 PID 记录是否有效，都按端口再清一遍（探针确认是自己人才动手）
    ports = {
        "backend": int(saved.get("backend_port", DEFAULT_BACKEND_PORT)),
        "frontend": int(saved.get("frontend_port", DEFAULT_FRONTEND_PORT)),
    }
    swept = 0
    for key, port in ports.items():
        for pid in stop_by_port(port, key):
            ok(f"按端口清理了残留的 {key}（端口 {port}，pid={pid}）")
            swept += 1

    if not targets and not swept:
        ok("没有找到在跑的服务")

    if PID_FILE.exists():
        PID_FILE.unlink()
    return 0


def cmd_status(bp: int, fp: int) -> int:
    step("服务状态")
    saved = read_pid_file()
    bp = int(saved.get("backend_port", bp))
    fp = int(saved.get("frontend_port", fp))

    back = backend_alive(bp)
    front = frontend_alive(fp)

    if back:
        body = http_get(f"http://{HOST}:{bp}/api/v1/health") or "{}"
        try:
            d = json.loads(body).get("data", {})
        except Exception:
            d = {}
        ok(f"后端 http://{HOST}:{bp}  app={d.get('app')} db={d.get('dialect')} "
           f"dify={d.get('dify_mode')}")
    else:
        bad(f"后端未运行（{HOST}:{bp}）")

    if front:
        ok(f"前端 http://{HOST}:{fp}")
    else:
        bad(f"前端未运行（{HOST}:{fp}）")

    return 0 if (back and front) else 1


# --------------------------------------------------------------------------- #
# 公共前置：解释器 / 依赖 / 配置 / 库 / 端口
# --------------------------------------------------------------------------- #
def dify_preflight(py: str) -> None:
    """live 模式下检查 7 个应用的 Key 是否配齐。

    用真实配置解析（`app.config.settings`）而不是自己读 .env 文本，
    免得和 config.py 里那套「JSON / k=v 两种写法」的逻辑各写一份、迟早漂移。
    """
    code = (
        "import json\n"
        "from app.config import settings\n"
        "print(json.dumps({'mode': settings.dify_mode,"
        " 'ready': settings.dify_live_ready,"
        " 'missing': settings.dify_missing_keys,"
        " 'base': settings.dify_base_url}, ensure_ascii=False))\n"
    )
    try:
        proc = subprocess.run([py, "-c", code], capture_output=True, text=True,
                              cwd=str(PROJECT), timeout=60,
                              encoding="utf-8", errors="replace")
        info = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except Exception:                                # 配置本身有问题时留给起服阶段报
        return

    step("Dify 接线预检")
    if not str(info.get("mode", "")).lower().startswith("live"):
        ok("mock 模式，不连 Dify（对话由本地编排层应答）")
        return

    if info.get("ready"):
        ok(f"live 模式，7 个应用 Key 齐全（base={info.get('base')}）")
        warn("连通性请跑 python scripts/check_dify.py 核对（本步只看配置齐全）")
        return

    missing = info.get("missing") or []
    bad(f"live 模式但有 {len(missing)} 个应用没配 Key：{'、'.join(missing)}")
    say("      这些应用的请求会**静默降级成 mock 回答**，从答复里看不出来。")
    say("      先跑 `python scripts/check_dify.py` 核对，或把 .env 改回 DIFY_MODE=mock。")


def prepare(args) -> tuple[Optional[str], Optional[tuple[int, int, bool, bool]]]:
    """返回 (python, (bp, fp, back_running, front_running))；出错时第二项为 None。"""
    rule()
    say("  留学机构 AI 智能助手系统 · 一键启动")
    say(f"  项目目录：{PROJECT}")
    rule()

    step("定位 Python 解释器")
    py = args.python or find_python()
    if not py:
        bad("找不到 Python。装一个 3.11+ 后重试，或用 --python 指定路径。")
        return None, None
    version = subprocess.run([py, "-c", "import sys;print(sys.version.split()[0])"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout.strip()
    ok(f"{py}  (Python {version})")

    if not ensure_dependencies(py):
        return py, None
    ensure_env_file()
    if not ensure_database(py, args.reseed):
        return py, None
    dify_preflight(py)

    step("选择端口")
    try:
        bp, back_running = pick_port(args.backend_port, "backend")
        fp, front_running = pick_port(args.frontend_port, "frontend")
    except RuntimeError as exc:
        bad(str(exc))
        return py, None
    ok(f"后端 {HOST}:{bp}{'（复用已在运行的实例）' if back_running else ''}")
    ok(f"前端 {HOST}:{fp}{'（复用已在运行的实例）' if front_running else ''}")
    return py, (bp, fp, back_running, front_running)


def print_banner(bp: int, fp: int) -> None:
    back_url = f"http://{HOST}:{bp}"
    front_url = f"http://{HOST}:{fp}"
    say("")
    rule()
    say("  启动完成")
    rule()
    say(f"  前端界面   {front_url}")
    say(f"  接口文档   {back_url}/docs")
    say(f"  健康检查   {back_url}/api/v1/health")
    say("")
    say("  演示账号（密码 = 用户名 + 123）")
    say("    admin / manager / advisor / teacher / student")
    say("    例：admin 的密码是 admin123")
    say("")
    say("  停止服务   start.bat stop      或关掉那两个服务窗口")
    say("  查看状态   start.bat status")
    rule()


# --------------------------------------------------------------------------- #
# 子命令：start
# --------------------------------------------------------------------------- #
def cmd_start(args) -> int:
    py, picked = prepare(args)
    if py is None or picked is None:
        return 2
    bp, fp, back_running, front_running = picked

    backend_cmd, frontend_cmd = service_commands(py, bp, fp)
    step("启动服务")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    saved = read_pid_file()
    logs: dict[str, Path] = {}

    if back_running:
        ok("后端已在运行，跳过启动")
    elif args.headless:
        pid, log_path = spawn_headless(py, backend_cmd[1:], "backend")
        logs["后端"] = log_path
        if pid:
            saved["backend"] = pid
            ok(f"后端已后台启动（pid={pid}）日志 {log_path}")
        else:
            bad("后端启动失败")
            return 6
    else:
        pid = spawn_console(BACKEND_TITLE, py, backend_cmd[1:])
        saved["backend"] = pid
        ok(f"后端窗口已打开（pid={pid}）")

    if front_running:
        ok("前端已在运行，跳过启动")
    elif args.headless:
        pid, log_path = spawn_headless(py, frontend_cmd[1:], "frontend")
        logs["前端"] = log_path
        if pid:
            saved["frontend"] = pid
            ok(f"前端已后台启动（pid={pid}）日志 {log_path}")
        else:
            bad("前端启动失败")
            return 6
    else:
        pid = spawn_console(FRONTEND_TITLE, py, frontend_cmd[1:])
        saved["frontend"] = pid
        ok(f"前端窗口已打开（pid={pid}）")

    saved.update({"backend_port": bp, "frontend_port": fp,
                  "started_at": datetime.now().isoformat(timespec="seconds"),
                  "python": py,
                  "mode": "headless" if args.headless else "console"})
    write_pid_file(saved)

    step("等待服务就绪")
    back_ready = wait_for("后端", lambda: backend_alive(bp), timeout=HEALTH_TIMEOUT)
    front_ready = wait_for("前端", lambda: frontend_alive(fp), timeout=FRONTEND_TIMEOUT)

    if not back_ready or not front_ready:
        bad("服务未能就绪。")
        if args.headless:
            show_logs(logs)
        else:
            say("      去看那两个新开的服务窗口里的报错，"
                "或改用 --headless 让日志落到 .run/logs/。")
        say("      也可以跑 python scripts/launcher.py --status 复检。")
        return 6

    if args.smoke:
        step("冒烟测试")
        subprocess.run([py, str(PROJECT / "scripts" / "smoke_test.py"),
                        "--base", f"http://{HOST}:{bp}"],
                       cwd=str(PROJECT), encoding="utf-8", errors="replace")

    print_banner(bp, fp)

    if not args.no_browser:
        try:
            webbrowser.open(f"http://{HOST}:{fp}")
            say("  已尝试打开浏览器。")
        except Exception:
            warn("自动打开浏览器失败，手动访问上面的前端地址即可。")

    return 0


# --------------------------------------------------------------------------- #
# 子命令：selfcheck（起服 → 体检 → 收服，全程单进程）
# --------------------------------------------------------------------------- #
def run_api_checks(bp: int, fp: int) -> list[tuple[str, bool, str]]:
    """打几条真实请求，确认前后端不只是「端口通了」而是真的能干活。"""
    out: list[tuple[str, bool, str]] = []

    def check(name: str, body: Optional[str], want: str) -> None:
        good = bool(body) and want in body
        detail = f"{len(body)} 字节" if body else "无响应"
        out.append((name, good, detail))

    check("GET  /api/v1/health",
          http_get(f"http://{HOST}:{bp}/api/v1/health"), '"status"')
    check("GET  /api/v1/ready",
          http_get(f"http://{HOST}:{bp}/api/v1/ready"), '"ready"')

    token_body = http_post_json(f"http://{HOST}:{bp}/api/v1/auth/token",
                                {"username": "admin", "password": "admin123"})
    check("POST /api/v1/auth/token", token_body, "access_token")

    # 用拿到的 token 访问受保护接口，确认鉴权链路真的通
    token = ""
    if token_body:
        try:
            token = (json.loads(token_body).get("data") or {}).get("access_token", "")
        except Exception:
            token = ""
    if token:
        me = http_get(f"http://{HOST}:{bp}/api/v1/auth/me", token=token)
        out.append(("GET  /api/v1/auth/me 带 token",
                    bool(me) and "admin" in me, f"{len(me or '')} 字节"))
        anon = http_get(f"http://{HOST}:{bp}/api/v1/auth/me")
        out.append(("GET  /api/v1/auth/me 无 token 应 401",
                    not anon, "401 已拦截" if not anon else "未拦截"))

    check("GET  /docs", http_get(f"http://{HOST}:{bp}/docs"), "swagger")
    check("GET  前端 /index.html",
          http_get(f"http://{HOST}:{fp}/index.html"), "留学机构")
    check("GET  前端 js/main.js",
          http_get(f"http://{HOST}:{fp}/js/main.js"), "import")
    check("GET  前端 styles.css",
          http_get(f"http://{HOST}:{fp}/styles.css"), ":root")
    return out


def cmd_selfcheck(args) -> int:
    if args.backend_port == DEFAULT_BACKEND_PORT and not port_is_free(DEFAULT_BACKEND_PORT):
        warn(f"{DEFAULT_BACKEND_PORT} 已被占用，改用 {DEFAULT_BACKEND_PORT + 1} 起体检")
        args.backend_port += 1
    if args.frontend_port == DEFAULT_FRONTEND_PORT and not port_is_free(DEFAULT_FRONTEND_PORT):
        warn(f"{DEFAULT_FRONTEND_PORT} 已被占用，改用 {DEFAULT_FRONTEND_PORT + 1} 起体检")
        args.frontend_port += 1

    py, picked = prepare(args)
    if py is None or picked is None:
        return 2
    bp, fp, back_running, front_running = picked

    if back_running or front_running:
        bad("自检需要独占端口，但检测到本项目已在运行。先 start.bat stop 再试。")
        return 5

    backend_cmd, frontend_cmd = service_commands(py, bp, fp)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    b_log = open(LOG_DIR / "selfcheck-backend.log", "wb")
    f_log = open(LOG_DIR / "selfcheck-frontend.log", "wb")

    step("拉起服务（体检模式，进程随本脚本退出）")
    procs: list[subprocess.Popen] = []
    try:
        procs.append(subprocess.Popen(backend_cmd, cwd=str(PROJECT),
                                      stdout=b_log, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL,
                                      creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0))
        procs.append(subprocess.Popen(frontend_cmd, cwd=str(PROJECT),
                                      stdout=f_log, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL,
                                      creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0))
        ok(f"后端 pid={procs[0].pid}，前端 pid={procs[1].pid}")

        results: list[tuple[str, bool, str]] = []

        step("探活")
        back_ready = wait_for("后端", lambda: backend_alive(bp), timeout=HEALTH_TIMEOUT)
        front_ready = wait_for("前端", lambda: frontend_alive(fp), timeout=FRONTEND_TIMEOUT)
        results.append(("后端健康检查", back_ready, f"http://{HOST}:{bp}/api/v1/health"))
        results.append(("前端首页可访问", front_ready, f"http://{HOST}:{fp}/index.html"))

        # 运行时配置注入
        injected = http_get(f"http://{HOST}:{fp}/runtime-config.js") or ""
        results.append(("前端注入后端地址", f":{bp}" in injected,
                        injected.strip().splitlines()[-1] if injected else "(拿不到)"))

        # HTTP 接口
        if back_ready:
            step("接口检查")
            results.extend(run_api_checks(bp, fp))
        else:
            results.append(("接口检查", False, "后端没起来，跳过"))

        # 冒烟测试
        step("冒烟测试（scripts/smoke_test.py）")
        smoke = subprocess.run(
            [py, str(PROJECT / "scripts" / "smoke_test.py"), "--base", f"http://{HOST}:{bp}"],
            cwd=str(PROJECT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
        smoke_out = (smoke.stdout or "") + (smoke.stderr or "")
        smoke_lines = [ln for ln in smoke_out.splitlines() if ln.strip()]
        passed = smoke.returncode == 0
        results.append(("冒烟测试整体", passed,
                        smoke_lines[-1][:90] if smoke_lines else "无输出"))

        # 浏览器端到端（可选：--e2e）。必须在服务还活着的时候、在这个进程里跑。
        if getattr(args, "e2e", False):
            if front_ready and back_ready:
                step("浏览器端到端（scripts/frontend_e2e.mjs）")
                results.append(run_frontend_e2e(bp, fp))
            else:
                results.append(("浏览器端到端", False, "服务没起来，跳过"))
    finally:
        step("收服")
        for p in procs:
            terminate(p)
        b_log.close()
        f_log.close()
        ok("已停止体检用的进程")

    step("体检结果")
    width = max(len(n) for n, _, _ in results)
    all_ok = True
    for name, good, detail in results:
        mark = "[OK]  " if good else "[FAIL]"
        all_ok = all_ok and good
        say(f"  {mark} {name.ljust(width)}  {detail}")

    if not all_ok:
        show_logs({"后端": LOG_DIR / "selfcheck-backend.log",
                   "前端": LOG_DIR / "selfcheck-frontend.log"})

    say("")
    rule()
    say("  体检结论：" + ("全部通过 ✓" if all_ok else "有项目未通过 ✗"))
    rule()
    return 0 if all_ok else 1


# --------------------------------------------------------------------------- #
def run_frontend_e2e(bp: int, fp: int) -> tuple[str, bool, str]:
    """跑真实浏览器端到端（puppeteer-core + 本机 Edge），返回 `(名称, 是否通过, 说明)`。

    ⚠️ 为什么必须挂在 `--selfcheck` 这条链路上，而不是单独写个「先起服再跑 E2E」的脚本：
       服务是 `cmd_selfcheck` 拉起来的**子进程**，它的生命周期绑在这个进程上。
       一旦改成「后台起服 → Shell 退出 → 再跑 E2E」，服务会随 Shell 一起被收走，
       后面的 E2E 只会拿到 `ERR_CONNECTION_REFUSED`（本机实测踩过这个坑）。
       所以能跑浏览器的前提就是「同一个进程里起服、跑、收服」。
    """
    node = find_node()
    if not node:
        return ("浏览器端到端", False, "找不到 node（装 node 或让它进 PATH）")
    script = PROJECT / "scripts" / "frontend_e2e.mjs"
    if not script.exists():
        return ("浏览器端到端", False, "缺少 scripts/frontend_e2e.mjs")

    env = dict(os.environ)
    env["FRONT_URL"] = f"http://{HOST}:{fp}/"
    env["BACK_URL"] = f"http://{HOST}:{bp}"
    # 本机/回环一律绕开系统代理，否则会被 HTTP_PROXY 接走
    env.setdefault("NO_PROXY", "127.0.0.1,localhost")
    env.setdefault("no_proxy", "127.0.0.1,localhost")
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    if home:
        ws = Path(home) / ".workbuddy" / "binaries" / "node" / "workspace"
        if ws.is_dir():
            env.setdefault("WB_NODE_WORKSPACE", str(ws))

    try:
        proc = subprocess.run([node, str(script)], cwd=str(PROJECT), env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=E2E_TIMEOUT)
    except subprocess.TimeoutExpired:
        return ("浏览器端到端", False, f"超时（>{E2E_TIMEOUT}s）")
    except Exception as exc:                                   # noqa: BLE001
        return ("浏览器端到端", False, f"{type(exc).__name__}: {exc}")

    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    detail = lines[-1][:90] if lines else "无输出"
    return ("浏览器端到端", proc.returncode == 0, detail)


def cmd_dify_check(args) -> int:
    """转发到 scripts/check_dify.py：看看 Dify 到底接上没有。"""
    py = args.python or find_python()
    if not py:
        bad("找不到 Python。装一个 3.11+ 后重试，或用 --python 指定路径。")
        return 2
    if not ensure_dependencies(py):
        return 2
    return subprocess.run([py, str(PROJECT / "scripts" / "check_dify.py")],
                          cwd=str(PROJECT)).returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="留学机构 AI 智能助手系统 —— 一键启动/停止",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--stop", action="store_true", help="停止后端与前端")
    parser.add_argument("--status", action="store_true", help="只检查状态")
    parser.add_argument("--selfcheck", action="store_true",
                        help="起服→体检→收服（单进程，适合 CI / 无桌面环境）")
    parser.add_argument("--e2e", action="store_true",
                        help="配 --selfcheck 用：体检完再跑一遍真实浏览器端到端（需本机 Edge）")
    parser.add_argument("--dify-check", action="store_true",
                        help="逐个探活 Dify 应用（切 live 前先跑这个）")
    parser.add_argument("--headless", action="store_true",
                        help="不开新控制台窗口，日志写到 .run/logs/")
    parser.add_argument("--smoke", action="store_true", help="启动后跑一遍冒烟测试")
    parser.add_argument("--reseed", action="store_true", help="重置并重建演示数据")
    parser.add_argument("--no-browser", action="store_true", help="不要自动开浏览器")
    parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT)
    parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT)
    parser.add_argument("--host", default=None,
                        help="后端监听地址（默认 127.0.0.1）。需要让 Docker 里的 Dify "
                             "反向调用 /internal/tools/* 时传 0.0.0.0；也可用环境变量 "
                             "LAUNCHER_HOST。⚠️ 0.0.0.0 会对同网段开放，仅限联调。")
    parser.add_argument("--python", default=None, help="指定 python 解释器路径")
    args = parser.parse_args()

    global BIND_HOST
    BIND_HOST = (args.host or os.environ.get("LAUNCHER_HOST") or HOST).strip() or HOST
    if BIND_HOST not in ("127.0.0.1", "localhost"):
        warn(f"后端将监听 {BIND_HOST} —— 同网段机器均可访问本服务，仅限本机联调使用")

    try:
        sys.stdout.reconfigure(errors="replace")     # type: ignore[union-attr]
    except Exception:
        pass

    os.chdir(PROJECT)                                # 保证 ./data 相对路径正确

    if args.stop:
        return cmd_stop()
    if args.status:
        return cmd_status(args.backend_port, args.frontend_port)
    if args.dify_check:
        return cmd_dify_check(args)
    if args.selfcheck:
        return cmd_selfcheck(args)
    return cmd_start(args)


if __name__ == "__main__":
    raise SystemExit(main())
