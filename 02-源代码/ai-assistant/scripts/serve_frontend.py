"""前端静态服务（前后端分离部署里的「前端侧」）。

前端零构建、零依赖，用任意静态服务器托管即可。这个脚本只是为了把
Python 自带的 http.server 包一层：补 .js / .mjs 的 MIME、关掉缓存，
方便本地联调时改完刷新就生效。

另外提供一个小机关：把 `--api-base` 的值以 `/runtime-config.js` 动态吐给
浏览器，前端启动时先读它再回落 localStorage / 默认值。这样 launcher 换了
后端端口也不用改前端代码，真正「一键」。

用法：
    python scripts/serve_frontend.py                      # http://127.0.0.1:8020
    python scripts/serve_frontend.py --port 5173
    python scripts/serve_frontend.py --api-base http://127.0.0.1:8011

注意：页面用了 ES Module，必须通过 HTTP 访问；
直接双击 index.html（file://）会被浏览器的模块同源策略拦住。
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import mimetypes
import os
import socketserver
import sys
from pathlib import Path

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/json", ".json")

ROOT = Path(__file__).resolve().parent.parent / "frontend"
RUNTIME_CONFIG_PATH = "/runtime-config.js"


class Handler(http.server.SimpleHTTPRequestHandler):
    """关缓存 + 静默日志（只打一行简化信息）+ 动态运行时配置。"""

    # 由 main() 注入；空串表示「不覆盖」，前端用自带默认值。
    api_base: str = ""

    def do_GET(self) -> None:                      # noqa: N802
        if self.path.split("?", 1)[0] == RUNTIME_CONFIG_PATH:
            self._serve_runtime_config()
            return
        super().do_GET()

    def do_HEAD(self) -> None:                     # noqa: N802
        if self.path.split("?", 1)[0] == RUNTIME_CONFIG_PATH:
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            return
        super().do_HEAD()

    def _serve_runtime_config(self) -> None:
        body = (
            "/* 由 scripts/serve_frontend.py 动态生成，勿手改 */\n"
            f"window.__UAS_API_BASE__ = {json.dumps(self.api_base)};\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:      # noqa: A003
        sys.stderr.write("  %s\n" % (fmt % args))


class ReuseServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description="前端静态服务器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--dir", default=str(ROOT))
    parser.add_argument("--api-base", default="",
                        help="注入给前端的后端地址，例如 http://127.0.0.1:8010")
    args = parser.parse_args()

    directory = Path(args.dir).resolve()
    if not (directory / "index.html").exists():
        print(f"[x] 目录里没有 index.html：{directory}")
        return 1

    Handler.api_base = (args.api_base or "").rstrip("/")
    handler = functools.partial(Handler, directory=str(directory))
    with ReuseServer((args.host, args.port), handler) as httpd:
        print("=" * 62)
        print("  留学机构 AI 智能助手 · 前端")
        print("=" * 62)
        print(f"  目录      : {directory}")
        print(f"  访问地址  : http://{args.host}:{args.port}/")
        if Handler.api_base:
            print(f"  后端 API  : {Handler.api_base}（已自动注入前端）")
        else:
            print("  后端 API  : 未注入，前端用登录页「接口地址」或默认 8010")
        print("  停止      : Ctrl+C")
        print("=" * 62)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    raise SystemExit(main())
