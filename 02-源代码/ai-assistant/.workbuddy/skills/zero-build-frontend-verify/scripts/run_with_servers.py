"""起后端 + 前端 → 跑一个 node 脚本（E2E / 截图）→ 收服，全部在**一次调用内**完成。

为什么必须这样：受控/沙箱环境会把本次工具调用派生的整棵进程树在调用结束时回收，
「调用 A 起服 → 调用 B 验证」的第二次一定连不上。这个脚本就是那个「单次调用编排器」。

用法
----
    python run_with_servers.py scripts/frontend_e2e.mjs
    python run_with_servers.py _lab/shot.mjs --frontend-port 8021

可覆盖的环境变量（都有默认值，默认值对齐本机 ai-assistant 项目）
--------------------------------------------------------------
    PROJECT_DIR        项目根（默认：本脚本上溯两级）
    PY                 跑前后端的 python.exe（默认 sys.executable）
    NODE               跑 node 脚本的 node.exe
    WB_NODE_WORKSPACE  含 node_modules（puppeteer-core）的目录，ESM 不认 NODE_PATH
    BACKEND_CMD        后端命令行模板，默认 "{py} -m uvicorn app.main:app --host {host} --port {port}"
    FRONTEND_CMD       前端命令行模板，默认 "{py} scripts/serve_frontend.py --host {host} --port {port} --api-base http://{host}:{bport}"
    BACKEND_PROBE      后端探活 "路径|关键字"，默认 "/api/v1/health|\"code\""
    FRONTEND_PROBE     前端探活 "路径|关键字"，默认 "/index.html|留学机构"
    HEALTH_TIMEOUT     后端就绪超时秒数，默认 40

退出码：0 = node 脚本正常结束；2 = 端口被占；3 = 服务未就绪。
⚠️ node 脚本自己是否「全部断言通过」要看它的 stdout / JSON 报告，别只看退出码。
"""
from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOST = os.environ.get("SERVE_HOST", "127.0.0.1")
CREATE_NO_WINDOW = 0x08000000


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def probe(port: int, path: str, marker: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}{path}", timeout=1.5) as r:
            return marker in r.read(65536).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return False


def wait(port: int, path: str, marker: str, timeout: float, label: str) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if probe(port, path, marker):
            print(f"  [ok] {label} 就绪（{time.time() - t0:.1f}s）")
            return True
        time.sleep(0.4)
    print(f"  [x] {label} 超时未就绪（{timeout}s）")
    return False


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def main(argv: list[str]) -> int:
    positional = [a for a in argv if not a.startswith("--")]
    if not positional:
        print(__doc__)
        return 64
    node_script = positional[0]

    # 允许 --frontend-port / --backend-port / --node 覆盖
    def opt(flag: str, env: str, default: str) -> str:
        if flag in argv:
            return argv[argv.index(flag) + 1]
        return _env(env, default)

    bp = int(opt("--backend-port", "BACKEND_PORT", "8010"))
    fp = int(opt("--frontend-port", "FRONTEND_PORT", "8020"))
    py = opt("--python", "PY", sys.executable)
    node = opt("--node", "NODE", "node")

    project = Path(_env("PROJECT_DIR", str(Path(__file__).resolve().parents[2]))).resolve()
    if not (project / node_script).exists() and not Path(node_script).exists():
        print(f"[x] 找不到 node 脚本：{node_script}（cwd={project}）")
        return 66

    for p in (bp, fp):
        if not port_free(p):
            print(f"[x] 端口 {p} 被占用，先停掉再跑")
            return 2

    b_tpl = _env("BACKEND_CMD",
                 "{py} -m uvicorn app.main:app --host {host} --port {port}")
    f_tpl = _env("FRONTEND_CMD",
                 "{py} scripts/serve_frontend.py --host {host} --port {port} "
                 "--api-base http://{host}:{bport}")
    b_cmd = shlex.split(b_tpl.format(py=py, host=HOST, port=bp, bport=bp))
    f_cmd = shlex.split(f_tpl.format(py=py, host=HOST, port=fp, bport=bp))

    b_probe = _env("BACKEND_PROBE", '/api/v1/health|"code"').split("|")
    f_probe = _env("FRONTEND_PROBE", "/index.html|留学机构").split("|")
    timeout = float(_env("HEALTH_TIMEOUT", "40"))

    logdir = project / ".run" / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    b_log = open(logdir / "verify-backend.log", "wb")
    f_log = open(logdir / "verify-frontend.log", "wb")

    # 本机常有全局 HTTP_PROXY，会把 127.0.0.1 的请求也丢给代理（实测 502）
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"

    procs: list[subprocess.Popen] = []
    try:
        procs.append(subprocess.Popen(b_cmd, cwd=str(project), env=env, stdout=b_log,
                                      stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      creationflags=CREATE_NO_WINDOW))
        procs.append(subprocess.Popen(f_cmd, cwd=str(project), env=env, stdout=f_log,
                                      stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      creationflags=CREATE_NO_WINDOW))
        print(f"后端 pid={procs[0].pid} 前端 pid={procs[1].pid}")
        ok_b = wait(bp, b_probe[0], b_probe[1] if len(b_probe) > 1 else "", timeout, "后端")
        ok_f = wait(fp, f_probe[0], f_probe[1] if len(f_probe) > 1 else "", 20, "前端")
        if not (ok_b and ok_f):
            print("--- backend log 尾部 ---")
            print((logdir / "verify-backend.log").read_text(encoding="utf-8", errors="replace")[-2500:])
            print("--- frontend log 尾部 ---")
            print((logdir / "verify-frontend.log").read_text(encoding="utf-8", errors="replace")[-1500:])
            return 3

        ne = dict(os.environ)
        ne["WB_NODE_WORKSPACE"] = _env(
            "WB_NODE_WORKSPACE",
            str(Path.home() / ".workbuddy" / "binaries" / "node" / "workspace"))
        ne["FRONT_URL"] = f"http://{HOST}:{fp}/"
        ne["BACK_URL"] = f"http://{HOST}:{bp}"
        r = subprocess.run([node, node_script], cwd=str(project), env=ne,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        print("=== node stdout ===")
        print(r.stdout)
        if r.stderr.strip():
            print("=== node stderr ===")
            print(r.stderr[-3000:])
        print(f"node exit={r.returncode}")
        return r.returncode
    finally:
        # Windows 上 taskkill /T 才能把 uvicorn / Edge 的子进程一起收掉
        for p in procs:
            if p.poll() is None:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True)
        b_log.close()
        f_log.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
